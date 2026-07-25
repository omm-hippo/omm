"""Pure model-selection logic for `omm contribute` (see cli.py's
`contribute()` command). No Typer/console/network dependency here so the
selection algorithm is directly unit-testable.

Selection order:
  Phase A - every hardware-viable candidate from `recommend`'s full ranked
  pool (not just its top-10), highest predicted speed first, skipping
  anything already in `history_refs`.
  Phase B - once Phase A is exhausted, alternate indefinitely between the
  weakest-still-viable candidates (closest to this hardware's ceiling from
  below) and the least-bad-unviable candidates (closest to the ceiling from
  above), skipping anything in `history_refs`. When both sides are fully
  seen, the caller may supply `refetch` to check for newly published
  candidates before giving up.
"""

from __future__ import annotations

from typing import Callable

from omm import predictor
from omm.hardware import HardwareInfo


def ref(candidate: dict) -> str:
    provider = candidate.get("provider") or "huggingface"
    return f"{provider}:{candidate['repo_id']}:{candidate['filename']}"


def matches_history(candidate: dict, history_refs: set[str]) -> bool:
    """True if `candidate` is already in `history_refs`, accepting both the
    current provider-prefixed ref format and the legacy bare
    "repo_id:filename" format that pre-dates provider support (always
    HuggingFace, since it was the only provider back then)."""
    if ref(candidate) in history_refs:
        return True
    provider = candidate.get("provider") or "huggingface"
    if provider != "huggingface":
        return False
    legacy_ref = f"{candidate['repo_id']}:{candidate['filename']}"
    return legacy_ref in history_refs


def _prefer_huggingface(pool: list[tuple[dict, float]]) -> list[tuple[dict, float]]:
    """Stable-sort so every HuggingFace candidate is tried before any other
    provider's, preserving the pool's existing relative order within each
    provider (score-descending, score-ascending, whatever the caller set
    up). ModelScope's download endpoint is far slower than HuggingFace's
    from most regions (confirmed via live curl - single-digit hundred KB/s),
    so `contribute`'s benchmark loop should exhaust HF candidates first
    regardless of predicted inference speed."""

    def priority(item: tuple[dict, float]) -> int:
        provider = item[0].get("provider") or "huggingface"
        return 0 if provider == "huggingface" else 1

    return sorted(pool, key=priority)


def _next_unseen(
    pool: list[tuple[dict, float]], history_refs: set[str], cursor: int
) -> tuple[dict | None, int]:
    """Scan `pool` starting at `cursor`, wrapping at most once, for a
    candidate not in `history_refs`."""
    n = len(pool)
    if n == 0:
        return None, cursor
    for step in range(n):
        idx = (cursor + step) % n
        candidate, _ = pool[idx]
        if not matches_history(candidate, history_refs):
            return candidate, idx + 1
    return None, cursor


class ContributionQueue:
    def __init__(self, artifact: dict, hw: HardwareInfo, history_refs: set[str]) -> None:
        self.artifact = artifact
        self.hw = hw
        self.history_refs = set(history_refs)
        self._rebuild()

    def _rebuild(self) -> None:
        ranked = predictor.rank_candidates(self.artifact, self.hw)
        viable = [(c, s) for c, s in ranked if s > 0]
        unviable = [(c, s) for c, s in ranked if s <= 0]
        self._phase_a_queue = [
            c for c, s in _prefer_huggingface(viable) if not matches_history(c, self.history_refs)
        ]
        self._below_pool = _prefer_huggingface(list(reversed(viable)))
        self._above_pool = _prefer_huggingface(unviable)
        self._below_cursor = 0
        self._above_cursor = 0
        self._next_side_is_below = True

    def mark_seen(self, seen_ref: str) -> None:
        self.history_refs.add(seen_ref)

    def next_candidate(
        self, refetch: Callable[[], tuple[dict, bool]] | None = None
    ) -> dict | None:
        while self._phase_a_queue:
            candidate = self._phase_a_queue.pop(0)
            if not matches_history(candidate, self.history_refs):
                return candidate

        for _ in range(2):  # try both sides at most once before giving up
            if self._next_side_is_below:
                candidate, self._below_cursor = _next_unseen(
                    self._below_pool, self.history_refs, self._below_cursor
                )
            else:
                candidate, self._above_cursor = _next_unseen(
                    self._above_pool, self.history_refs, self._above_cursor
                )
            self._next_side_is_below = not self._next_side_is_below
            if candidate is not None:
                return candidate

        if refetch is not None:
            new_artifact, changed = refetch()
            if changed:
                self.artifact = new_artifact
                self._rebuild()
                return self.next_candidate(refetch)

        return None
