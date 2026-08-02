"""
cooling.py - Predictive cooling state machine for Kinver Hub.

Implements the 3-stage virtual-temperature protocol that communicates
with the external Cooler Control daemon via raw integer writes to
``/tmp/cpu_temp.txt`` and ``/tmp/gpu_temp.txt``.

The three stages (documented in CoolingPreset constants.py):

1. **PREFILL BURST** (75000): Written BEFORE forwarding the inference
   payload.  Followed by a mandatory 1-second buffer (``asyncio.sleep(1.0)``)
   to allow physical fans to accelerate before the AVX-512 / VRAM matrix
   burst.

2. **GENERATION HOLD** (50000): Written upon receiving the FIRST token
   from the model.  This provides enough airflow to cool localized
   physical core hotspots during single-token decoding while keeping
   the system acoustically quiet.

3. **BASELINE IDLE** (30000): Written when the stream terminates or
   sends [DONE].  Returns the system to silent operation.  Also written
   at proxy startup.

The cooling state machine only writes to the IPC file(s) that are
actively in use for the current hardware path (CPU, GPU, or Hybrid).
Unused hardware paths are left untouched.

Design principle: Moderate virtual temperatures establish airflow
momentum WITHOUT triggering maximum RPMs.  Max RPMs are reserved for
actual physical thermal emergencies managed natively by the cooler daemon.

Usage::

    from cooling import CoolingStateMachine
    cooler = CoolingStateMachine()
    await cooler.prefill_burst("gpu")
    await cooler.generation_hold("gpu")
    await cooler.baseline_idle()

Maintainers: James Stansfield
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from constants import (
    CPU_TEMP_PATH,
    GPU_TEMP_PATH,
    CoolingPreset,
    get_logger,
)

logger = get_logger("proxy.cooling")

# ---------------------------------------------------------------------------
# The mandatory delay (seconds) between PREFILL write and payload dispatch.
# Per the spec: "the proxy MUST execute an await asyncio.sleep(1.0) delay,
# holding the network request to allow fans to physically accelerate."
# ---------------------------------------------------------------------------
PREFILL_BUFFER_SECONDS: float = 1.0


# ---------------------------------------------------------------------------
# CoolingStateMachine
# ---------------------------------------------------------------------------

class CoolingStateMachine:
    """
    Manages the 3-stage predictive cooling lifecycle.

    This class is intentionally stateless — it writes the current stage
    to the IPC files on demand.  The "state" is tracked externally by
    the proxy's request lifecycle (before inference → first chunk → done).

    Parameters
    ----------
    cpu_path : Path
        Filesystem path for the CPU virtual-temperature IPC file.
    gpu_path : Path
        Filesystem path for the GPU virtual-temperature IPC file.
    """

    def __init__(
        self,
        cpu_path: Path = CPU_TEMP_PATH,
        gpu_path: Path = GPU_TEMP_PATH,
    ) -> None:
        self._cpu_path: Path = cpu_path
        self._gpu_path: Path = gpu_path

    # ------------------------------------------------------------------
    # Internal: write a temperature value to the appropriate file(s)
    # ------------------------------------------------------------------

    def _read_temp(self, path: Path) -> int:
        """
        Read the current integer value from an IPC file.

        Returns 0 if the file doesn't exist or is unreadable — a
        missing file is treated as "no cooling active" (0).
        """
        try:
            return int(path.read_text().strip())
        except (OSError, ValueError):
            return 0

    def _write_temp(
        self,
        path: Path,
        value: int,
        label: str,
        allow_downgrade: bool = True,
    ) -> None:
        """
        Write an integer temperature value to an IPC file.

        Failures (e.g. permission denied, filesystem full) are logged
        but not raised — cooling is advisory, not critical-path.

        The *allow_downgrade* flag controls read-before-write safety:

        - ``allow_downgrade=False``: If the file currently holds a
          **higher** temperature (set by another concurrent stream),
          skip the write to avoid silencing a more demanding process.
        - ``allow_downgrade=True``: Write unconditionally (used for
          BASELINE cleanup after all streams have finished).
        """
        if not allow_downgrade:
            current = self._read_temp(path)
            if current > value:
                # Another process has set a higher temp — do NOT downgrade
                logger.debug(
                    "Cooling → SKIP %s write %d (current=%d is higher, "
                    "another stream is active)",
                    label, value, current,
                )
                return

        try:
            path.write_text(str(value))
        except OSError:
            logger.exception(
                "Failed to write %d to %s (%s cooling IPC)",
                value, path, label,
            )

    # ------------------------------------------------------------------
    # Stage 0: Baseline (called on startup and after stream completion)
    # ------------------------------------------------------------------

    def baseline_idle(self) -> None:
        """
        Write BASELINE (30000) to both CPU and GPU IPC files.

        Called at proxy startup and after every inference stream terminates.
        Returns the fans to near-silent idle.
        """
        logger.debug("Cooling → BASELINE (30000) on CPU + GPU")
        # BASELINE is always allowed to write — this is the final cleanup
        # after all streams have terminated (no other process should be
        # holding a higher temp at this point).
        self._write_temp(self._cpu_path, CoolingPreset.BASELINE, "CPU", allow_downgrade=True)
        self._write_temp(self._gpu_path, CoolingPreset.BASELINE, "GPU", allow_downgrade=True)

    # ------------------------------------------------------------------
    # Stage 1: Prefill Burst (called before forwarding inference payload)
    # ------------------------------------------------------------------

    async def prefill_burst(self, target_hardware: str = "gpu") -> None:
        """
        Write PREFILL (75000) to the relevant hardware IPC file(s) and
        then sleep for the mandatory 1-second buffer to allow fans to
        physically accelerate.

        Parameters
        ----------
        target_hardware : str
            One of ``"cpu"``, ``"gpu"``, or ``"hybrid"``.  Determines which
            IPC file(s) receive the PREFILL value.  Unused paths are left
            untouched.

            - ``"cpu"``: Only ``/tmp/cpu_temp.txt`` is written.
            - ``"gpu"``: Only ``/tmp/gpu_temp.txt`` is written.
            - ``"hybrid"``: Both files are written (CPU-offloaded reasoning).
        """
        logger.info("Cooling → PREFILL BURST (75000) on %s", target_hardware)

        # PREFILL (75000) is the ceiling — it always writes because no
        # process ever requests a higher virtual temperature.
        if target_hardware in ("cpu", "hybrid"):
            self._write_temp(self._cpu_path, CoolingPreset.PREFILL, "CPU", allow_downgrade=False)

        if target_hardware in ("gpu", "hybrid"):
            self._write_temp(self._gpu_path, CoolingPreset.PREFILL, "GPU", allow_downgrade=False)

        # --- THE 1-SECOND BUFFER ---
        # CRITICAL: This delay holds the network request so that the
        # physical fans have time to accelerate before the compute burst
        # (AVX-512 on CPU, matrix ops on GPU) actually begins.
        await asyncio.sleep(PREFILL_BUFFER_SECONDS)

    # ------------------------------------------------------------------
    # Stage 2: Generation Hold (called on first token from the model)
    # ------------------------------------------------------------------

    def generation_hold(self, target_hardware: str = "gpu") -> None:
        """
        Write GENERATION (50000) to the relevant hardware IPC file(s).

        This is called upon receiving the FIRST token/chunk from the
        model, stepping down from the PREFILL peak to a sustained level
        that cools localized physical core hotspots during single-token
        decoding while keeping acoustics moderate.

        Parameters
        ----------
        target_hardware : str
            One of ``"cpu"``, ``"gpu"``, or ``"hybrid"``.
        """
        logger.debug("Cooling → GENERATION HOLD (50000) on %s", target_hardware)

        # GENERATION (50000) uses allow_downgrade=False: if another
        # concurrent stream is still in PREFILL (75000), we must not
        # drop the IPC file down to 50000 and slow the fans.
        if target_hardware in ("cpu", "hybrid"):
            self._write_temp(self._cpu_path, CoolingPreset.GENERATION, "CPU", allow_downgrade=False)

        if target_hardware in ("gpu", "hybrid"):
            self._write_temp(self._gpu_path, CoolingPreset.GENERATION, "GPU", allow_downgrade=False)

    # ------------------------------------------------------------------
    # Public API: determine which hardware path is active
    # ------------------------------------------------------------------

    @staticmethod
    def hardware_path_for_model(model_key: str) -> str:
        """
        Determine the cooling hardware path for a given model key.

        - CPU-only models (frontdesk): ``"cpu"``
        - Hybrid models (architect, coder, creative, professional, scholar):
          ``"hybrid"`` — these models use partial CPU offloading alongside
          GPU compute and benefit from full-system cooling on both channels.
        - GPU-only models (chatter): ``"gpu"`` 
        - Cloud: no local cooling needed (returns ``"none"``)

        Parameters
        ----------
        model_key : str
            The key from ``LLAMA_ENDPOINTS`` (e.g. ``"professional"``, ``"frontdesk"``).

        Returns
        -------
        str
            ``"cpu"``, ``"gpu"``, or ``"none"``.
        """
        cpu_models = {"frontdesk"}
        hybrid_models = {"architect", "coder", "creative", "professional", "scholar"}
        if model_key in cpu_models:
            return "cpu"
        if model_key in hybrid_models:
            return "hybrid"
        if model_key == "cloud":
            return "none"
        return "gpu"