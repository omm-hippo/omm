"""Which memory group a machine belongs to, for the arena leaderboard.

Sub-project D (issue #410) ranks models per group of similar machines, so a
vote row carries a coarse group: Apple Silicon by its unified memory, a PC by
its GPU's VRAM (where the model actually runs), and a PC with no usable GPU by
its RAM.

Buckets are named after **marketed sizes** so a person finds their machine
without arithmetic. The OS reports a little less than the marketed size (a
16 GB Mac reads ~15.8 GiB, an 8 GB card ~7.98), so a reading is rounded to the
nearest whole GiB first and then matched against closed ranges. These are
deliberately not `usage.py`'s buckets: those use half-open `<` edges on the
raw reading, which put a real 16 GB machine in `8-16` and a 24 GB card next to
80 GB cards.

The labels are a wire contract - mirrored by `VOTE_MEMORY_BUCKETS` in
cf-worker/src/validate.ts and the `votes` block in database.rules.json, and
stored in every uploaded row - so changing one means a redeploy and mixed
data. tests/test_vote_wire_contract.py keeps the three in sync.

Pure standard library at import time: scripts/arena_score.py imports this
module too (`classify` imports omm.hardware lazily).
"""

from __future__ import annotations

import math
import platform
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omm.hardware import HardwareInfo

KINDS = ("unified", "vram", "cpu")

#: (upper bound in whole GiB, label) per kind; a reading above the last bound
#: takes the overflow label.
_BOUNDS: dict[str, tuple[tuple[int, str], ...]] = {
    "unified": ((8, "8"), (16, "16"), (24, "18-24"), (36, "32-36"), (64, "48-64"), (128, "96-128")),
    "vram": ((6, "0-6"), (8, "8"), (12, "10-12"), (16, "16"), (24, "20-24")),
    "cpu": ((8, "0-8"), (16, "16"), (32, "32")),
}
_OVERFLOW = {"unified": "192+", "vram": "32+", "cpu": "64+"}

BUCKET_LABELS: dict[str, tuple[str, ...]] = {
    kind: tuple(label for _bound, label in bounds) + (_OVERFLOW[kind],)
    for kind, bounds in _BOUNDS.items()
}

#: Where "no GPU detected" can be believed. On Linux hardware.py only reads
#: NVML, so an AMD GPU machine reports none - better no group than "CPU only".
_TRUSTED_NO_GPU_SYSTEMS = frozenset({"Darwin", "Windows"})

_FAMILY_LABEL = {
    "unified": "Mac · unified memory",
    "vram": "PC · GPU VRAM",
    "cpu": "CPU only · RAM",
}


def _usable(reading: object) -> float | None:
    if isinstance(reading, bool) or not isinstance(reading, (int, float)):
        return None
    value = float(reading)
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def bucket_for(kind: str, reading_gib: float) -> str | None:
    """The bucket label for an OS-reported size in GiB, or None when the
    reading is unusable or the kind unknown."""
    bounds = _BOUNDS.get(kind)
    value = _usable(reading_gib)
    if bounds is None or value is None:
        return None
    # floor(x + 0.5), not round(): round() is banker's rounding.
    rounded = math.floor(value + 0.5)
    for bound, label in bounds:
        if rounded <= bound:
            return label
    return _OVERFLOW[kind]


def classify(hw: HardwareInfo, *, system: str | None = None) -> tuple[str, str] | None:
    """(memory_kind, memory_bucket) for this machine, or None when no group
    can be stated honestly. A None row still counts in the overall ranking."""
    system = platform.system() if system is None else system
    if hw.unified_memory:
        bucket = bucket_for("unified", hw.ram_total_gb)
        return ("unified", bucket) if bucket else None
    if _usable(hw.vram_total_gb) is not None:
        bucket = bucket_for("vram", hw.vram_total_gb)
        return ("vram", bucket) if bucket else None
    if system not in _TRUSTED_NO_GPU_SYSTEMS:
        return None
    if hw.gpu_name:
        # A GPU was detected but its size was not: either an integrated /
        # shared-memory adapter (the model runs from RAM - CPU only is
        # right) or a discrete card hardware.py could not size (a GPU
        # machine - no group beats a wrong one).
        from omm.hardware import is_integrated_gpu

        if not is_integrated_gpu(hw.gpu_name):
            return None
    bucket = bucket_for("cpu", hw.ram_total_gb)
    return ("cpu", bucket) if bucket else None


def is_valid(kind: object, bucket: object) -> bool:
    return isinstance(kind, str) and bucket in BUCKET_LABELS.get(kind, ())


def display_label(kind: str, bucket: str) -> str:
    """Human label, e.g. "Mac · unified memory 16GB". An unknown pair (a
    table change this client predates) falls back to its raw key."""
    if not is_valid(kind, bucket):
        return f"{kind}:{bucket}"
    if bucket.startswith("0-"):
        size = f"≤{bucket[2:]}GB"
    elif bucket.endswith("+"):
        size = f"{bucket[:-1]}GB+"
    else:
        size = f"{bucket.replace('-', '–')}GB"
    return f"{_FAMILY_LABEL[kind]} {size}"
