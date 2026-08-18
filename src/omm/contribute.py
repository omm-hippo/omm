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
  Phase C - once Phase A, Phase B, and `refetch` are all exhausted, the
  caller may supply `fetch_siblings` to probe other GGUF quantizations of
  the two boundary repos (the last candidate Phase A popped, and the
  first Phase B drew from its unviable side) - below first, then above -
  narrowing in on this machine's actual fit/unfit ceiling instead of
  stopping cold at the fixed candidate pool's edge.

Three separate sets keep a candidate out of selection: `history_refs`
(done with, permanently or for this session), `excluded_refs` (held back by
a condition that lapses on its own, e.g. benchmark_history's post-download
failure cooldown), and the internal deferred set (`defer` /
`release_deferred`, for live memory pressure within a session).

`mark_seen` re-ranks the remaining pools immediately (via `_rebuild`), so
a local calibration update from the benchmark that was just marked seen
takes effect on the very next `next_candidate()` call rather than only on
the next session or the next `refetch`.
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
    pool: list[tuple[dict, float]],
    history_refs: set[str],
    cursor: int,
    deferred_refs: set[str] | None = None,
) -> tuple[dict | None, int]:
    """Scan `pool` starting at `cursor`, wrapping at most once, for a
    candidate not in `history_refs`."""
    n = len(pool)
    if n == 0:
        return None, cursor
    for step in range(n):
        idx = (cursor + step) % n
        candidate, _ = pool[idx]
        if not matches_history(candidate, history_refs) and ref(candidate) not in (deferred_refs or ()):
            return candidate, idx + 1
    return None, cursor


class ContributionQueue:
    def __init__(
        self,
        artifact: dict,
        hw: HardwareInfo,
        history_refs: set[str],
        excluded_refs: set[str] | None = None,
    ) -> None:
        """`history_refs` are candidates this machine is done with (already
        benchmarked, or covered by this session); `excluded_refs` are ones
        held back for a reason that will lapse on its own - today, the
        post-download failure cooldown in benchmark_history. Keeping them
        apart matters: `history_refs` is what the caller reports as
        "candidates covered", and a candidate nobody tried this run has not
        been covered by it."""
        self.artifact = artifact
        self.hw = hw
        self.history_refs = set(history_refs)
        self.excluded_refs = set(excluded_refs or ())
        self._deferred_refs: set[str] = set()
        self._boundary_below: dict | None = None
        self._boundary_above: dict | None = None
        self._phase_c_below_queue: list[dict] = []
        self._phase_c_above_queue: list[dict] = []
        self._phase_c_below_fetched = False
        self._phase_c_above_fetched = False
        self._rebuild()

    def _rebuild(self) -> None:
        ranked = predictor.rank_candidates(self.artifact, self.hw)
        viable = [(c, s) for c, s in ranked if s > 0]
        unviable = [(c, s) for c, s in ranked if s <= 0]
        self._phase_a_queue = [
            c
            for c, s in _prefer_huggingface(viable)
            if not matches_history(c, self.history_refs) and ref(c) not in self._blocked_refs
        ]
        self._below_pool = _prefer_huggingface(list(reversed(viable)))
        self._above_pool = _prefer_huggingface(unviable)
        self._below_cursor = 0
        self._above_cursor = 0
        self._next_side_is_below = True

    def mark_seen(self, seen_ref: str) -> None:
        self._deferred_refs.discard(seen_ref)
        self.excluded_refs.discard(seen_ref)
        self.history_refs.add(seen_ref)
        self._rebuild()

    @property
    def _blocked_refs(self) -> set[str]:
        """Every ref selection must currently step over, whatever the
        reason. `defer`/`release_deferred` only ever move refs in and out of
        `_deferred_refs`, so an excluded ref cannot be released by the
        loop's deferred-retry bookkeeping."""
        return self._deferred_refs | self.excluded_refs

    @property
    def deferred_refs(self) -> frozenset[str]:
        return frozenset(self._deferred_refs)

    def defer(self, deferred_ref: str) -> None:
        """Temporarily remove a memory-constrained candidate from selection."""
        if deferred_ref not in self.history_refs:
            self._deferred_refs.add(deferred_ref)
            self._rebuild()

    def release_deferred(self, deferred_ref: str) -> None:
        if deferred_ref in self._deferred_refs:
            self._deferred_refs.remove(deferred_ref)
            self._rebuild()

    def next_candidate(
        self,
        refetch: Callable[[], tuple[dict, bool]] | None = None,
        fetch_siblings: Callable[[dict], list[dict]] | None = None,
    ) -> dict | None:
        while self._phase_a_queue:
            candidate = self._phase_a_queue.pop(0)
            if (
                not matches_history(candidate, self.history_refs)
                and ref(candidate) not in self._blocked_refs
            ):
                self._boundary_below = candidate
                return candidate

        for _ in range(2):  # try both sides at most once before giving up
            if self._next_side_is_below:
                candidate, self._below_cursor = _next_unseen(
                    self._below_pool,
                    self.history_refs,
                    self._below_cursor,
                    self._blocked_refs,
                )
                if candidate is not None:
                    self._boundary_below = candidate
            else:
                candidate, self._above_cursor = _next_unseen(
                    self._above_pool,
                    self.history_refs,
                    self._above_cursor,
                    self._blocked_refs,
                )
                if candidate is not None and self._boundary_above is None:
                    self._boundary_above = candidate
            self._next_side_is_below = not self._next_side_is_below
            if candidate is not None:
                return candidate

        if refetch is not None:
            new_artifact, changed = refetch()
            if changed:
                self.artifact = new_artifact
                self._rebuild()
                return self.next_candidate(refetch, fetch_siblings)

        return self._next_phase_c_candidate(fetch_siblings)

    def _next_phase_c_candidate(
        self, fetch_siblings: Callable[[dict], list[dict]] | None
    ) -> dict | None:
        if fetch_siblings is None:
            return None
        for boundary_attr, queue_attr, fetched_attr in (
            ("_boundary_below", "_phase_c_below_queue", "_phase_c_below_fetched"),
            ("_boundary_above", "_phase_c_above_queue", "_phase_c_above_fetched"),
        ):
            if not getattr(self, fetched_attr):
                setattr(self, fetched_attr, True)
                boundary = getattr(self, boundary_attr)
                if boundary is not None:
                    siblings = fetch_siblings(boundary)
                    setattr(
                        self,
                        queue_attr,
                        [
                            c
                            for c in siblings
                            if not matches_history(c, self.history_refs)
                            and ref(c) not in self._blocked_refs
                        ],
                    )
            queue = getattr(self, queue_attr)
            while queue:
                candidate = queue.pop(0)
                if (
                    not matches_history(candidate, self.history_refs)
                    and ref(candidate) not in self._blocked_refs
                ):
                    return candidate
        return None
