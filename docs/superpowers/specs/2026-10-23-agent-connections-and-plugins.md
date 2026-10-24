# Agent connections and reviewed runtime extensions

Related issues: #502, #431. The user approved implementation after reviewing the
issue inventory. Existing #406/#421/#423/#424/#425/#427/#428 behavior is validated
separately; it is not counted as newly implemented here.

`omm setting agent local claude|codex|pi --model NAME` checks the installed
harness and the loopback Ollama model's tool/context capabilities, then changes
only the selected harness's routing fields. `cloud` restores the original
configuration; `status` reports whether OMM has an active connection record.
No harness is launched by these commands. No cloud account is modified.

Preserve exact original file bytes when unchanged since connection. Preserve
unrelated intervening changes with field-level three-way restoration. Refuse
conflicting routing edits rather than guessing. Store a private write-ahead
journal before multi-file changes so interrupted changes remain recoverable.
Reject malformed files and symbolic links. Never print original credentials.
Use syntax-preserving TOML editing for Codex, JSON for Claude and pi. Respect
their documented configuration-directory environment overrides.

Only local Ollama is supported in this phase. Gateway tokens, cluster setup and
RAG/MCP registration depend on #503/#504. Capability metadata is a preflight,
not proof of successful tool calls. Project/managed settings and shell overrides
can supersede user settings and must be reported as verification limits.

The plugin contract is a small explicit in-process runtime registry over the
existing RuntimeAdapter protocol. It validates version/capabilities and reserves
built-in names. Callers must explicitly approve trusted Python factories before
registration. There is no package installer, entry-point discovery, import-path
loader, shell execution or web installation API. External Python has the user's
process permissions, not a sandbox. Providers and document parsers are recorded
as future contracts, not fictitiously supported extension types.

Tests cover real temporary-file persistence, exact restoration, edited settings,
conflicts, interrupted writes, local HTTP preflight, CLI routing, builtin
retention and an example adapter through the compatibility lifecycle. Real
harness/model file-edit workflows and unavailable operating systems remain
separate verification requirements.
