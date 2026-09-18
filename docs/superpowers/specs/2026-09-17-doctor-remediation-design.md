# Doctor remediation and disk-space wording design

## Problem

`omm doctor` currently reports `PASS`, `WARN`, and `FAIL` details, but most
findings leave the user to infer the next action. A few checks embed advice in
their detail text, which is inconsistent and not machine-readable.

The first-run hardware table also divides free bytes by `1024**3` while
labelling the result `GB`. That is a GiB value, so it differs from macOS and
`diskutil`, which display decimal GB. The value is free space on the APFS
container holding `OMM_HOME`, not the size of the `.omm` directory.

## Goals

- Keep `omm doctor` read-only while showing safe next steps after its summary.
- Attach remediation to the specific warning or failure that caused it.
- Preserve commands as argument arrays in JSON and quote them only for display.
- Deduplicate identical root-cause actions, such as an unavailable Ollama
  server causing both server and tag checks to warn.
- Show decimal GB in onboarding and explain that the value is immediately
  writable space on the volume containing `OMM_HOME`.

## Non-goals

- No `--fix` mode and no automatic mutation, deletion, reinstall, daemon start,
  or registry repair.
- No promise that APFS purgeable space is immediately writable.
- No attempt to hide existing diagnostic paths; privacy-safe bug-report output
  remains governed by its own allowlist.

## Data contract

An actionable `DoctorCheck` may carry a `DoctorRemediation` with a human
message and an optional tuple of command arguments. PASS checks cannot carry a
remediation. JSON omits the field when there is no action and otherwise emits:

```json
{
  "remediation": {
    "message": "Re-link this model into Ollama, then run omm doctor again.",
    "command": ["omm", "link", "model.gguf", "--engine", "ollama"]
  }
}
```

The terminal keeps the existing result table and overall status. It then
prints a `How to fix` section containing only unique WARN/FAIL actions, followed
by a reminder that no changes were made.

## Safety rules

- Registry corruption receives backup/inspect/restore guidance, never a delete
  command.
- Optional Ollama absence says it can be ignored when Ollama is not used.
- Platform-dependent repair advice stays descriptive unless one command is
  valid for the detected installation source.
- Model filenames are stored as command arguments and shell-quoted only at the
  final rendering boundary.

## Disk-space semantics

`_free_gb()` uses `bytes / 1000**3`, matching the GB label and macOS
`diskutil`. The table shows `omm home` as the path alone and a separate
`Disk available` row as immediately writable space on that volume. APFS may
show additional purgeable capacity elsewhere; OMM keeps the conservative
immediately writable value for installation decisions.
