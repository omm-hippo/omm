# Compact engine listing across info/scan/install

## Problem

`linker.ENGINES` currently has 7 entries (Ollama, LM Studio, Jan, AnythingLLM,
Msty, text-generation-webui, KoboldCpp) and every place that iterates it
prints one row/line per engine regardless of whether that engine is even
installed on the user's machine:

- `omm info <model>` (`cli.py:1880-1887`) prints a table row per engine,
  showing `"not linked"` for the 5-6 engines a typical user has never
  installed.
- `omm scan` (`cli.py:290-296`) prints a "Local AI runners" table with one row
  per engine, `"not detected"` for whichever aren't installed.
- `_link_model()` (`cli.py:1302-1329`), called during `omm install`/`omm
  update`, prints a dim `"<Engine> not detected, skipping link."` line for
  every uninstalled engine while a download is in progress.

None of this is wrong information, but it's noise that scales linearly with
`len(linker.ENGINES)`. The list has already grown from a handful to 7 and is
expected to keep growing, so the "list everything" pattern degrades further
with every new engine added. The user should see what's actually relevant to
their machine (what's installed, what's linked) without wading through a
majority-empty list.

## Design

### 1. `omm info` — installed engines only

Add an `installed` lookup (same pattern `omm scan` already uses):

```python
installed = {spec.key: linker.is_engine_installed(spec.key) for spec in linker.ENGINES}
```

In the table-building loop, skip any `spec` where `installed[spec.key]` is
`False` — that engine gets no row at all. Engines that are installed keep
today's per-engine behavior unchanged (`"ollama run <tag>"` / `"linked (visible
in ...)"` / `"not linked"`).

After the table, if any engine is *not* installed, print one summary line:

```
+ 5 program(s) not installed — see the compatibility list: https://github.com/omm-hippo/omm/wiki/Compatible-Programs
```

Omit this line entirely if every known engine is installed (count is 0).

`--json` output is unchanged — it keeps emitting the full `linked` dict for
all `linker.ENGINES` entries, since that path is for scripts/automation, not
visual scanning, and filtering it would be a breaking change for consumers
that expect a stable key set.

### 2. `omm scan` — "Local AI runners" table, same treatment

Same filter: only print a row for engines where `is_engine_installed()` is
`True`. After the table, print the same style of one-line summary when count
of not-installed engines > 0, reusing the same wiki link. Omit when 0.

The "Local AI models" table (per-model `Engine(s)` column) is untouched — it
already only lists engines a given model is actually linked into, so it
doesn't grow with the total engine count.

### 3. `_link_model()` — drop the per-skip noise line during install/update

`cli.py:1319` currently does:

```python
if not linker.is_engine_installed(spec.key):
    console.print(f"[dim]{spec.label} not detected, skipping link.[/dim]")
    continue
```

Change to a silent `continue` — drop the `console.print`. The `linked` dict
this function returns is unaffected (still correctly `False` for that
engine), so every downstream consumer (the post-install summary at
`cli.py:1737-1742`, which already only prints engines that ended up linked)
keeps working exactly as today. Net effect: during a fresh install with only
Ollama present, the 6 "not detected, skipping link" lines that currently
scroll past mid-download disappear; nothing else changes.

The existing `LinkError` warning path (`cli.py:1326-1327`, engine *is*
installed but linking failed) is untouched — that's a real, actionable
problem and stays visible.

### 4. New wiki page: compatible programs list

New page at `https://github.com/omm-hippo/omm/wiki/Compatible-Programs`,
linked from the summary lines above. Content: a table of all
`linker.ENGINES` entries (label + short description + link to the program's
own homepage/download page) plus a one-line note that `omm link`/`omm scan`
will pick them up automatically once installed. This page is edited directly
on GitHub Wiki (out of band from the `omm` release process — it's
documentation, not code), so this design doesn't add any new
generation/sync mechanism for it. Out of scope for the implementation plan
beyond writing its initial content once.

## Error handling

No new failure modes are introduced. `linker.is_engine_installed()` is
already called today in both `info`-adjacent (`scan`) and `_link_model`
code paths and is assumed reliable (existing behavior, not touched). The
"0 missing -> omit the summary line" and "0 installed -> table renders with
zero engine rows, only the summary line shows" cases are the only two edge
states, and both degrade gracefully (an empty table section is not an error,
just a short one).

## Testing

- `omm info` table: an installed+linked engine, an installed+not-linked
  engine, and an uninstalled engine should each behave as described (row
  shown/row shown/no row), with the summary line appearing only when the
  uninstalled count is nonzero. Existing tests around `cli.py`'s `info`
  command need updating for the new row-filtering behavior.
- `omm scan`: same three-state check for the "Local AI runners" table.
- `_link_model`: assert no `"not detected, skipping link"` string appears in
  captured output for an uninstalled engine, while the returned `linked`
  dict still reports `False` for it (existing tests already assert the dict;
  add an output-content assertion).
- `--json` output of `omm info` continues to include all `linker.ENGINES`
  keys regardless of install state (regression guard for the "JSON stays
  full" decision above).
