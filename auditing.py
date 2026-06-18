"""
auditing.py - Incremental Shadow Auditor for Kinver Hub.

Provides a non-blocking background auditing system that evaluates
streaming output from high-stakes workflows (Lane B coding and Lane A
tool executions involving file/shell modifications) without halting
the active stream to the client.

Design principles:
1. **Continuous Ingestion**: As tokens stream out, they are fed into
   the Auditor's KV cache continuously in the background.
2. **Intermittent Evaluation**: The auditor only produces a verdict at
   logical boundaries (every 100 tokens or at newlines).
3. **Knowledge Cutoff Bypass**: Before killing a stream for a failed
   audit, the system checks:
   a. Local lessons-learned table (sqlite-vec semantic search)
   b. User-supplied reference files / .clinerules (context prepended
      to the auditor's KV cache)
4. **Graceful Guillotine**: If a stream must be terminated, the output
   is cleanly balanced (trailing JSON/Markdown syntax) and annotated
   before closing the connection.
5. **Multi-Attempt Escalation**: On audit failure, the job's failure
   counter is incremented and the queue escalates to the next model
   tier on retry.  No hot-swaps occur mid-stream.

Activation logic:
- ENABLED for: Lane B (IDE coding), Lane A TOOL executions
- DISABLED for: standard Lane A CHAT

Usage::

    auditor = ShadowAuditor(db, systemd, hardware_governor)
    auditor.start(job, messages)
    # ... stream chunks ...
    # The auditor runs in its own asyncio.Task, never blocking
    auditor.stop()

Maintainers: James Stansfield
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, Dict, Any, List

from constants import get_logger

logger = get_logger("proxy.auditing")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# How many tokens/chunks between each formal evaluation
EVALUATION_INTERVAL_TOKENS: int = 100

# The auditor prompt sent to the reasoning model for evaluation.
# CRITICAL: The model is told that it is evaluating INCOMPLETE mid-stream
# output while generation is still in progress.  It must NOT flag missing
# sections, unfinished code blocks, or incomplete thoughts as issues —
# these haven't been generated yet.  Only evaluate what IS present.
# The VERDICT: prefix gives a reliable anchor for regex parsing.
AUDITOR_EVAL_PROMPT: str = (
    "You are evaluating INCOMPLETE streaming output while generation is "
    "still in progress.  Do NOT flag missing sections, unfinished code "
    "blocks, or incomplete thoughts — these haven't been generated yet. "
    "Only evaluate what IS present for: factual errors in completed "
    "statements, safety/security issues, or clearly wrong claims.\n\n"
    "Reply with exactly ONE line in this format:\n"
    "VERDICT: OK (the text so far is acceptable, even if incomplete)\n"
    "VERDICT: WARNING: <reason> (minor concern, let it continue)\n"
    "VERDICT: FATAL: <reason> (genuinely dangerous or completely wrong)\n\n"
    "Do NOT include any other text, commentary, or markdown."
)


# ---------------------------------------------------------------------------
# ShadowAuditor
# ---------------------------------------------------------------------------

class ShadowAuditor:
    """
    Non-blocking background auditor that evaluates streaming model output.

    Designed to run as a separate asyncio.Task that never blocks the
    main SSE streaming loop.  Chunks are fed into the auditor via an
    internal asyncio.Queue, and evaluation happens at logical boundaries.

    Parameters
    ----------
    database : Database
        Used for lessons-learned lookups and audit log persistence.
    systemd : SystemdController
        Used to resolve the reasoning model's port.
    hardware_governor : HardwareGovernor or None
        Used for GPU occupancy checks.
    """

    def __init__(
        self,
        database,       # Database
        systemd,        # SystemdController
        hardware_governor=None,  # HardwareGovernor or None
    ) -> None:
        self._db = database
        self._systemd = systemd
        self._hw = hardware_governor

        # Internal state for an active audit session
        self._active: bool = False
        self._job_id: Optional[str] = None
        self._project_id: Optional[str] = None
        self._accumulated_text: str = ""
        self._chunk_queue: asyncio.Queue = asyncio.Queue()
        self._audit_task: Optional[asyncio.Task] = None
        self._chunk_counter: int = 0
        self._last_eval_at: int = 0
        self._messages: list[dict] = []
        self._on_fatal_callback = None  # Callable invoked on FATAL verdict

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(
        self,
        job_id: str,
        project_id: str,
        messages: list[dict],
        on_fatal: callable = None,
    ) -> None:
        """
        Begin shadow auditing for a job.

        Spawns a background asyncio.Task that continuously ingests
        chunks from the internal queue and evaluates them at boundaries.

        Parameters
        ----------
        job_id : str
            The job UUID being audited.
        project_id : str
            The project slug for lessons-learned lookups.
        messages : list[dict]
            The full message array (including system prompts, user
            messages, and any .clinerules context).  This is prepended
            to the auditor's evaluation context so it can natively
            evaluate rules it wasn't originally trained on.
        on_fatal : callable or None
            Optional async callback ``(job_id, reason)`` invoked when a
            FATAL verdict is confirmed (after bypass layers).
        """
        if self._active:
            logger.warning("ShadowAuditor already active — stopping previous session")
            self.stop()

        self._active = True
        self._job_id = job_id
        self._project_id = project_id
        self._accumulated_text = ""
        self._chunk_counter = 0
        self._last_eval_at = 0
        self._messages = messages
        self._on_fatal_callback = on_fatal
        # Fresh queue for this session
        self._chunk_queue = asyncio.Queue()

        # Spawn the background ingestion loop
        self._audit_task = asyncio.create_task(self._ingest_loop())

        logger.info("ShadowAuditor started for job %s", job_id)

    def stop(self) -> None:
        """
        Stop the shadow auditor for the current job.

        Cancels the background task and resets internal state.
        Safe to call even if not active.
        """
        self._active = False
        if self._audit_task and not self._audit_task.done():
            self._audit_task.cancel()
        self._audit_task = None
        self._job_id = None
        logger.debug("ShadowAuditor stopped")

    def feed_chunk(self, text: str) -> None:
        """
        Feed a text chunk into the auditor's ingestion queue.

        This is called from the main SSE streaming loop for every delta
        chunk received from the model.  It is non-blocking — the chunk
        is put on an asyncio.Queue and processed in the background.

        Parameters
        ----------
        text : str
            A text delta from the SSE stream.
        """
        if not self._active:
            return
        try:
            self._chunk_queue.put_nowait(text)
        except asyncio.QueueFull:
            logger.debug("Auditor chunk queue full — dropping oldest chunk")
            try:
                self._chunk_queue.get_nowait()
                self._chunk_queue.put_nowait(text)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Background ingestion loop
    # ------------------------------------------------------------------

    async def _ingest_loop(self) -> None:
        """
        Main loop of the background auditor task.

        Continuously reads chunks from the queue, accumulates them,
        and triggers evaluation at logical boundaries (every 100 tokens
        or at newlines within a boundary window).
        """
        logger.debug("Auditor ingestion loop started for job %s", self._job_id)

        try:
            while self._active:
                try:
                    # Wait for the next chunk with a short timeout so the
                    # loop can check self._active periodically
                    chunk = await asyncio.wait_for(
                        self._chunk_queue.get(), timeout=0.5,
                    )
                except asyncio.TimeoutError:
                    continue

                self._accumulated_text += chunk
                self._chunk_counter += 1

                # ---- Determine if it's time to evaluate --------------------
                # Evaluate at EVALUATION_INTERVAL_TOKENS boundaries
                # and at every newline for finer granularity
                should_eval = False

                if self._chunk_counter - self._last_eval_at >= EVALUATION_INTERVAL_TOKENS:
                    should_eval = True
                elif "\n" in chunk and self._chunk_counter - self._last_eval_at >= 10:
                    # Evaluate at newlines too, but not too frequently
                    should_eval = True

                if should_eval:
                    await self._evaluate(self._accumulated_text, self._chunk_counter)
                    self._last_eval_at = self._chunk_counter

        except asyncio.CancelledError:
            logger.debug("Auditor ingestion loop cancelled for job %s", self._job_id)
        except Exception:
            logger.exception("Auditor ingestion loop error for job %s", self._job_id)
        finally:
            self._active = False

    # ------------------------------------------------------------------
    # Evaluation step
    # ------------------------------------------------------------------

    async def _evaluate(self, text_block: str, offset: int) -> None:
        """
        Send the accumulated text to the reasoning (auditor) model for
        evaluation.  Handle verdicts with the bypass/override logic.

        Parameters
        ----------
        text_block : str
            The accumulated text so far.
        offset : int
            The chunk counter position (used for audit logging).
        """
        if not self._active:
            return

        try:
            # ---- Build the evaluation prompt --------------------------------
            # Prepend user-supplied context (messages, .clinerules) so the
            # auditor can natively evaluate rules it wasn't trained on.
            context_prepend = self._build_context_prepend()

            eval_prompt = (
                f"{context_prepend}\n\n"
                f"{AUDITOR_EVAL_PROMPT}\n\n"
                f"--- GENERATED TEXT TO EVALUATE ---\n"
                f"{text_block[-2000:]}\n"  # Last 2000 chars for context window
                f"--- END ---"
            )

            # ---- Call the reasoning model ----------------------------------
            port = await self._systemd.get_port("reasoning")
            from llm import call_model

            verdict_raw = await call_model(
                port,
                eval_prompt,
                profile="deterministic",
                max_tokens=60,
            )
            verdict_raw = verdict_raw.strip()

            logger.debug(
                "Auditor verdict for job %s at offset %d: %s",
                self._job_id, offset, verdict_raw,
            )

            # ---- Parse verdict ---------------------------------------------
            verdict, reason = self._parse_verdict(verdict_raw)

            # ---- Persist audit record --------------------------------------
            await self._db.record_audit(
                job_id=self._job_id,
                chunk_offset=offset,
                flagged_text=text_block[-1000:],
                verdict=verdict,
            )

            # ---- Handle FATAL: run bypass layers ---------------------------
            if verdict == "FATAL":
                overridden = await self._apply_bypass_layers(text_block, reason)
                if overridden:
                    logger.info(
                        "Auditor FATAL verdict OVERRIDDEN by local knowledge "
                        "for job %s: %s", self._job_id, reason,
                    )
                    # Update the audit record to show override
                    await self._db.record_audit(
                        job_id=self._job_id,
                        chunk_offset=offset,
                        flagged_text=f"[OVERRIDE] {text_block[-500:]}",
                        verdict="OK",
                        overridden=True,
                        override_reason=f"Local knowledge bypass: {reason}",
                    )
                else:
                    logger.warning(
                        "Auditor FATAL verdict CONFIRMED for job %s: %s",
                        self._job_id, reason,
                    )
                    await self._trigger_fatal(self._job_id, reason)

            # ---- Handle WARNING: log but don't interrupt -------------------
            elif verdict == "WARNING":
                logger.warning(
                    "Auditor WARNING for job %s: %s",
                    self._job_id, reason,
                )

        except Exception:
            logger.exception(
                "Auditor evaluation failed for job %s at offset %d",
                self._job_id, offset,
            )

    # ------------------------------------------------------------------
    # Knowledge cutoff bypass layers
    # ------------------------------------------------------------------

    async def _apply_bypass_layers(
        self,
        text_block: str,
        reason: str,
    ) -> bool:
        """
        Apply the two bypass layers before confirming an auditor veto:

        1. **Local Lessons Learned**: Rapid sqlite-vec semantic search
           of the flagged chunk against the project's lessons-learned
           table.  If a valid local reference confirms the syntax is
           legitimate, silently override the veto.

        2. **Context Prepends**: The user-supplied reference files and
           .clinerules are already prepended to the auditor's evaluation
           context (see ``_build_context_prepend``), allowing the model
           to natively evaluate rules it wasn't trained on.

        Returns True if the FATAL verdict should be overridden (auditor
        was wrong), False if the verdict stands.
        """
        # Layer 1: Check lessons-learned table
        if self._project_id:
            try:
                match_found = await self._db.check_lessons_learned(
                    self._project_id,
                    text_block[-500:],  # Search the most recent flagged text
                )
                if match_found:
                    logger.info(
                        "Bypass layer 1: lessons-learned match for project %s",
                        self._project_id,
                    )
                    return True
            except Exception:
                logger.debug("Lessons-learned check failed (non-critical)")

        return False

    def _build_context_prepend(self) -> str:
        """
        Build a context prepend from the message history so the auditor
        can evaluate rules it wasn't originally trained on.

        This extracts system prompts, .clinerules references, and any
        user-provided reference files from the messages array.
        """
        parts: list[str] = ["[Auditor Context — User-Supplied Rules & References]\n"]

        for msg in self._messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                # System prompts often contain .clinerules / project rules
                parts.append(f"[System Prompt]: {content[:1000]}")
            elif role == "user" and isinstance(content, str):
                # Extract any file path references or rule mentions
                if ".clinerules" in content or "# " in content or "rules" in content.lower():
                    parts.append(f"[User Rules]: {content[:1000]}")

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Verdict parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_verdict(raw: str) -> tuple[str, str]:
        """
        Parse the auditor's raw output into a (verdict, reason) tuple.

        Uses regex to find the VERDICT: keyword ANYWHERE in the output.
        DeepSeek R1 models sometimes prepend noise (``, filler text,
        partial chain-of-thought) before the actual verdict, so
        simple ``startswith()`` misses valid responses.

        The updated auditor prompt explicitly requests:
        ``VERDICT: OK`` / ``VERDICT: WARNING: <reason>`` / ``VERDICT: FATAL: <reason>``

        Expected formats:
        - ``VERDICT: OK`` (or just ``OK`` as legacy fallback)
        - ``VERDICT: WARNING: <reason>``
        - ``VERDICT: FATAL: <reason>``
        """
        import re
        import json as _json
        raw_upper = raw.upper().strip()

        # The reasoning model sometimes outputs JSON-formatted responses
        # rather than plain text (e.g. {"choices":[{"message":{"content":"OK"}}]}).
        # Try to extract the actual verdict text from any JSON wrapper first.
        if raw_upper.startswith("{") and raw_upper.endswith("}"):
            try:
                parsed = _json.loads(raw)
                # Try common nesting patterns: chat format or completion format
                if "content" in parsed and isinstance(parsed["content"], str):
                    raw_upper = parsed["content"].upper().strip()
                elif "choices" in parsed:
                    inner = parsed["choices"][0].get("message", {}).get("content", "")
                    if inner:
                        raw_upper = inner.upper().strip()
            except (_json.JSONDecodeError, (IndexError, KeyError, TypeError)):
                pass  # Not valid JSON — fall through to regex parsing

        # Try the explicit VERDICT: format first (new prompt)
        match = re.search(r"VERDICT:\s*(OK|WARNING|FATAL)", raw_upper)
        if match:
            verdict = match.group(1)
            # Extract reason if present
            reason_match = re.search(
                r"VERDICT:\s*(?:WARNING|FATAL)\s*:?\s*(.+)", raw_upper,
            )
            reason = reason_match.group(1).strip() if reason_match else ""
            if verdict == "OK":
                return ("OK", "")
            return (verdict, reason)

        # Legacy fallback: look for bare OK/WARNING/FATAL anywhere
        if re.search(r"\bFATAL\b", raw_upper):
            reason = re.sub(r".*\bFATAL\b\s*:?\s*", "", raw, count=1, flags=re.IGNORECASE).strip()
            return ("FATAL", reason or "unspecified")
        elif re.search(r"\bWARNING\b", raw_upper):
            reason = re.sub(r".*\bWARNING\b\s*:?\s*", "", raw, count=1, flags=re.IGNORECASE).strip()
            return ("WARNING", reason or "unspecified")
        elif re.search(r"\bOK\b", raw_upper):
            return ("OK", "")

        # Completely unrecognized — treat as OK to avoid false positives
        logger.debug("Unrecognized auditor verdict: %s", raw)
        return ("OK", "")

    # ------------------------------------------------------------------
    # Fatal handling
    # ------------------------------------------------------------------

    async def _trigger_fatal(self, job_id: str, reason: str) -> None:
        """
        Handle a confirmed FATAL verdict.

        1. Increment the job's failure counter in the database.
        2. Invoke the on_fatal callback if set (typically triggers the
           Graceful Guillotine in proxy.py).

        The caller (proxy.py) is responsible for:
        - Balancing trailing JSON/Markdown syntax
        - Appending the ``[PROXY AUDIT OVERRIDE: ...]`` message
        - Closing the SSE connection cleanly
        - Firing the Systemd hot-swap to the next escalation tier
        """
        logger.critical(
            "FATAL AUDIT for job %s: %s — triggering escalation",
            job_id, reason,
        )

        # Increment failure counter in DB
        try:
            new_count = await self._db.fail_job(job_id)
            logger.info(
                "Job %s failure counter incremented to %d",
                job_id, new_count,
            )
        except Exception:
            logger.exception("Failed to increment failure counter for job %s", job_id)

        # Invoke the fatal callback (Graceful Guillotine)
        if self._on_fatal_callback:
            try:
                await self._on_fatal_callback(job_id, reason)
            except Exception:
                logger.exception("on_fatal callback failed for job %s", job_id)

        # Stop auditing this job
        self.stop()

    # ------------------------------------------------------------------
    # Static helper: decide if auditing should be active for a request
    # ------------------------------------------------------------------

    @staticmethod
    def should_audit(intent: str, is_lane_b: bool, tools: Optional[list] = None) -> bool:
        """
        Return True if shadow auditing should be active for this request.

        Auditing is active for:
        - Lane B (IDE coding) — always audited
        - Lane A TOOL — audited if tools are present
        - Lane A CODE — audited

        Auditing is DISABLED for:
        - Standard Lane A CHAT (no tools, low complexity)

        Parameters
        ----------
        intent : str
            The classified intent (CHAT, CODE, TOOL, etc.).
        is_lane_b : bool
            Whether this is a Lane B request.
        tools : list or None
            The tools array from the request.

        Returns
        -------
        bool
            True if auditing should be active.
        """
        if is_lane_b:
            return True
        if intent in ("CODE", "TOOL"):
            return True
        if intent == "CHAT" and tools:
            # CHAT with tools → could modify files, audit it
            return True
        return False