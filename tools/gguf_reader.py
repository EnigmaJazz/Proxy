"""
tools/gguf_reader.py - Build-time GGUF metadata reader.

Uses the ``gguf`` Python package to read technical facts from GGUF files
referenced by ``config/local_models.yaml``.  Runtime never imports this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import numpy as np
from gguf import GGUFReader, GGUFValueType


class GGUFReadError(Exception):
    """Raised when a GGUF file cannot be read or is missing critical fields."""


@dataclass(frozen=True)
class GGUFMetadata:
    """Technical facts extracted from a GGUF header.

    Attributes
    ----------
    architecture:
        Value of ``general.architecture`` (e.g. ``"qwen2"``, ``"llama"``).
    context_length:
        Value of the architecture-specific ``<arch>.context_length`` key.
    file_type:
        Value of ``general.file_type`` (quantization enum integer).
    name:
        Value of ``general.name``; falls back to the filename if absent.
    """

    architecture: str
    context_length: int
    file_type: int
    name: str


def _decode_field(reader: GGUFReader, key: str) -> Optional[Union[str, int]]:
    """Return a scalar string or uint32 value for *key*, or ``None``."""
    field = reader.get_field(key)
    if field is None or not field.parts:
        return None
    value_type = field.types[0] if field.types else None
    if value_type == GGUFValueType.STRING:
        return field.parts[-1].data.tobytes().decode("utf-8")
    if value_type == GGUFValueType.UINT32:
        return int(np.array(field.parts[-1].data)[0])
    return None


def read_gguf_metadata(path: Path) -> GGUFMetadata:
    """Open *path*, parse the GGUF header, and return technical facts.

    Raises:
        GGUFReadError: if the file is missing, unreadable, truncated, or
            missing critical fields (``general.architecture`` or the
            architecture-specific ``*.context_length``).
    """
    if not path.exists():
        raise GGUFReadError(f"GGUF file not found: {path}")
    if not path.is_file():
        raise GGUFReadError(f"GGUF path is not a file: {path}")

    try:
        reader = GGUFReader(str(path), "r")
    except Exception as exc:
        raise GGUFReadError(f"Failed to open GGUF file {path}: {exc}") from exc

    architecture = _decode_field(reader, "general.architecture")
    if architecture is None:
        raise GGUFReadError(f"Missing general.architecture in {path}")
    if not isinstance(architecture, str):
        raise GGUFReadError(f"Invalid general.architecture type in {path}")

    context_length = _decode_field(reader, f"{architecture}.context_length")
    if context_length is None:
        raise GGUFReadError(
            f"Missing {architecture}.context_length in {path}"
        )
    if not isinstance(context_length, int):
        raise GGUFReadError(f"Invalid context_length type in {path}")

    file_type = _decode_field(reader, "general.file_type")
    if file_type is None:
        raise GGUFReadError(f"Missing general.file_type in {path}")
    if not isinstance(file_type, int):
        raise GGUFReadError(f"Invalid file_type type in {path}")

    name = _decode_field(reader, "general.name")
    if not isinstance(name, str):
        name = path.name

    return GGUFMetadata(
        architecture=architecture,
        context_length=context_length,
        file_type=file_type,
        name=name,
    )
