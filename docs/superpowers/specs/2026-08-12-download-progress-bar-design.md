# Download Progress Bar Redesign

## Problem
`omm install` / `omm contribute` download progress uses rich's default look:
```
ornith-1.0-9b-Q4_K_M.gguf ━━━━━╺━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 0.7/5.6 GB 3.5 MB/s 0:23:31
```
User finds the bar generic and the trailing size/speed/ETA cluster visually flat.

## Decision
Single line, Homebrew-flavored: spinner, filename, a bar filled with plain `#`
characters (no brackets, no inline percentage), then size / speed / ETA.

```
⠋ ornith-1.0-9b-Q4_K_M.gguf ############  0.7/5.6 GB  3.5 MB/s  ETA 23:31
```

- Spinner: rich `SpinnerColumn` (dots style), replaces the current bare filename start.
- Bar: custom `ProgressColumn` rendering `"#" * filled + " " * empty`, no
  brackets/percent — matches Homebrew's `curl -#` bar. Width is not fixed:
  the column uses rich's ratio-based expansion (`ratio=1`, same mechanism the
  default `BarColumn` already relies on) so it fills whatever space is left
  in the terminal after filename/size/speed/ETA. Rich recomputes this on
  every render, so a mid-download terminal resize is picked up for free —
  no extra polling/debounce needed. Clamp to a sane `[10, 60]` char range so
  it doesn't collapse on a very narrow terminal or look absurd on an
  ultra-wide one.
- Trailing columns: existing `DownloadColumn` (size) + `TransferSpeedColumn` (speed)
  stay; `TimeRemainingColumn` is replaced with a column labeled `ETA <time>` instead
  of rich's default bare `0:23:31`.
- Everything stays on one `rich.progress.Progress` line — no multi-row layout.

## Scope
Single change point: `_progress()` in `src/omm/downloader.py` (used by both
install and contribute download paths — no call-site changes needed elsewhere).

## Out of scope
- Color/gradient bars, percentage display, per-thread parallel-download sub-bars.
- Changing what `contribute`/`install` print before/after the bar (headers, summaries).
