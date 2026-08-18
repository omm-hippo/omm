"""Small, bounded GGUF metadata reader.

The reader intentionally stops before tensor data.  It accepts either a local
file or an in-memory prefix fetched with HTTP Range, which lets contribute make
profile-aware memory decisions before downloading a multi-gigabyte model.
"""

from __future__ import annotations

import io
import struct
from pathlib import Path
from typing import BinaryIO

_UINT8, _INT8, _UINT16, _INT16, _UINT32, _INT32 = 0, 1, 2, 3, 4, 5
_FLOAT32, _BOOL, _STRING, _ARRAY, _UINT64, _INT64, _FLOAT64 = 6, 7, 8, 9, 10, 11, 12

_SCALAR_FORMATS = {
    _UINT8: "<B",
    _INT8: "<b",
    _UINT16: "<H",
    _INT16: "<h",
    _UINT32: "<I",
    _INT32: "<i",
    _FLOAT32: "<f",
    _BOOL: "<B",
    _UINT64: "<Q",
    _INT64: "<q",
    _FLOAT64: "<d",
}

_MAX_KV_COUNT = 1_000_000
_MAX_STRING_BYTES = 64 * 1024**2
_MAX_ARRAY_ITEMS = 100_000_000


def _read_exact(f: BinaryIO, size: int) -> bytes:
    value = f.read(size)
    if len(value) != size:
        raise struct.error(f"GGUF metadata ended early (wanted {size} bytes, got {len(value)})")
    return value


def _unpack(f: BinaryIO, fmt: str):
    return struct.unpack(fmt, _read_exact(f, struct.calcsize(fmt)))[0]


def _read_string(f: BinaryIO) -> str:
    length = _unpack(f, "<Q")
    if length > _MAX_STRING_BYTES:
        raise ValueError(f"GGUF string is unreasonably large ({length} bytes)")
    return _read_exact(f, length).decode("utf-8", errors="replace")


def _read_scalar(f: BinaryIO, value_type: int) -> object:
    if value_type == _STRING:
        return _read_string(f)
    fmt = _SCALAR_FORMATS.get(value_type)
    if fmt is None:
        raise ValueError(f"unsupported GGUF metadata value type: {value_type}")
    value = _unpack(f, fmt)
    return bool(value) if value_type == _BOOL else value


def _skip_value(f: BinaryIO, value_type: int) -> None:
    """Read past a value without materializing it (arrays included)."""
    if value_type == _ARRAY:
        elem_type = _unpack(f, "<I")
        length = _unpack(f, "<Q")
        if length > _MAX_ARRAY_ITEMS:
            raise ValueError(f"GGUF metadata array is unreasonably large ({length} items)")
        if elem_type in _SCALAR_FORMATS:
            _read_exact(f, struct.calcsize(_SCALAR_FORMATS[elem_type]) * length)
            return
        for _ in range(length):
            _skip_value(f, elem_type)
        return
    _read_scalar(f, value_type)


def read_gguf_metadata_stream(f: BinaryIO, wanted_keys: set[str]) -> dict[str, object]:
    """Return typed scalar metadata for ``wanted_keys`` from a GGUF stream.

    Arrays are deliberately not returned because the memory estimator only
    needs scalar architecture dimensions.  A short HTTP prefix raises
    ``struct.error`` so its caller can retry with a larger Range.
    """
    found: dict[str, object] = {}
    if _read_exact(f, 4) != b"GGUF":
        return found
    version = _unpack(f, "<I")
    if version not in (2, 3):
        raise ValueError(f"unsupported GGUF version: {version}")
    _read_exact(f, 8)  # tensor_count
    kv_count = _unpack(f, "<Q")
    if kv_count > _MAX_KV_COUNT:
        raise ValueError(f"GGUF metadata count is unreasonably large ({kv_count})")
    for _ in range(kv_count):
        key = _read_string(f)
        value_type = _unpack(f, "<I")
        if key in wanted_keys and value_type != _ARRAY:
            found[key] = _read_scalar(f, value_type)
        else:
            _skip_value(f, value_type)
        if len(found) == len(wanted_keys):
            break
    return found


def read_gguf_metadata_bytes(data: bytes, wanted_keys: set[str]) -> dict[str, object]:
    return read_gguf_metadata_stream(io.BytesIO(data), wanted_keys)


def read_gguf_metadata(path: Path, wanted_keys: set[str]) -> dict[str, object]:
    with path.open("rb") as f:
        return read_gguf_metadata_stream(f, wanted_keys)
