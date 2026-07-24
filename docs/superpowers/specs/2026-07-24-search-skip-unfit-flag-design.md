# `omm search --skip-unfit` design

## Problem

`omm search` lists every matching candidate, including ones predicted not
to run on the user's hardware (shown in red with a
"(predicted not to run on this hardware)" suffix, `cli.py:2021`). There is
no way to hide these and see only viable results.

## Solution

Add a `--skip-unfit` boolean flag to `omm search`, mirroring the existing
`install --skip-unfit` flag (`cli.py:1325-1330`) in name and intent:
"if this hardware is predicted not to run it, don't show it."

Default: `False`. Existing behavior (list everything, mark unfit ones red)
is unchanged unless the flag is passed.

## Behavior

In the per-candidate loop in `search()` (`cli.py:1984-2021`):

- After computing `fits_hardware`, if `skip_unfit and not fits_hardware`:
  skip the candidate entirely — don't add it to `refs`/`seen_refs`, don't
  print it, don't add it to the JSON `rows` list.
- Family headers (`==> family`) print lazily — only right before the first
  surviving candidate in that family is printed — so a family with all
  candidates filtered out produces no empty header/blank-line block.
- Index numbers (`[N]`), the `refs` list, and
  `session_cache.record_results(refs)` are based only on candidates that
  are actually shown, so numbering stays contiguous and
  `omm install [N]` always matches what's on screen.
- Applies identically to `--json` output: filtered rows are simply absent
  from the JSON array (not marked and kept).

## Out of scope

- No change to `install --skip-unfit` (already exists, different command).
- No change to how `fits_hardware` itself is computed (predictor logic
  untouched).
- No pagination/limit changes — this only removes unfit entries, it
  doesn't cap the remaining count.

## Testing

Add/extend a test in the search test suite covering:
- `--skip-unfit` with a mix of fit/unfit candidates hides only unfit ones.
- Index numbers stay contiguous after filtering.
- A family with all-unfit candidates produces no header for that family.
- `--json --skip-unfit` omits unfit rows entirely.
