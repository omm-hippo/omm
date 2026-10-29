# Local agent connections

## One session only: `omm code`

```sh
omm code codex                  # pick an installed Ollama model, start Codex on it
omm code claude --model TAG     # same for Claude Code, model given up front
omm code codex exec "fix the failing test"   # arguments after the harness go to it
```

`omm code` starts the harness in the current terminal with local-Ollama
routing passed on its command line (Codex `-c` overrides) or in its process
environment (Claude Code), so no settings file is written. The Codex and
Claude desktop apps, other terminals, and later sessions keep their normal
model; quitting the harness is the way back. The model picker starts at the
last model used, and off a terminal that model is reused without asking.
The same tool-calling and context checks as `agent local` run first.

For Codex it also sets `model_context_window` and
`model_auto_compact_token_limit` to the local model's context, so a
cloud-sized value in `config.toml` does not stop auto-compaction, and turns
off `features.multi_agent` and `web_search`. Ollama's Responses API (checked
with 0.30.10) rejects every request that lists a non-function tool, and Codex
0.165.0 sends sub-agent (`namespace`) and `web_search` tools by default.
`agent local codex` below does not change those two settings, so with Codex
0.165+ against Ollama it fails with repeated reconnect errors; prefer
`omm code codex`. pi is not supported by `omm code` yet.

Checked on 2026-10-29 with Codex CLI 0.165.0, Claude Code 2.1.310 and Ollama
0.30.10: both harnesses answered through local Ollama, and Codex with
`gpt-oss-20b` ran a shell tool call that wrote a file. `qwen3:4b-instruct`
answered but replied with a code block instead of calling the tool.

## Saved connection: `omm setting agent`

Related issue: #502, local Ollama phase. Install Claude Code, Codex CLI or pi
separately, then install a local Ollama model with tool support and at least
32K context capacity. 64K+ is recommended. OMM does not install the harness or
download a model in these commands.

```sh
omm setting agent local claude --model YOUR_INSTALLED_MODEL
omm setting agent status claude
omm setting agent cloud claude
```

Use `codex` or `pi` in place of `claude`. A custom Ollama port can be selected
with `--base-url http://127.0.0.1:PORT`; only loopback origins are accepted.
Ollama 0.14.0+ is required. Missing tool metadata or insufficient model context
stops before settings are changed. Cloud tags and explicit remote-model
metadata are refused. These metadata checks do not establish correct tool
calling, effective runtime context, or that every request remains local.

The user routing files are Claude's `~/.claude/settings.json`, Codex's
`~/.codex/config.toml`, and pi's `~/.pi/agent/{models,settings}.json`. OMM
respects `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, and `PI_CODING_AGENT_DIR`.
After connecting, start a new session with the harness's normal command.
Existing sessions are not interrupted or reconfigured. Project, organization,
environment and command-line settings may override these user settings. Check
the effective model/server in the harness before sending private material.

Original bytes and applied bytes are backed up in
`OMM_HOME/agent-connections/<harness>.json` (mode 0600 on POSIX). Backups may
contain original credentials; they remain local, are not logged/uploaded, and
must not be attached to bug reports. No existing account authentication file
is changed. `cloud` means restoration of the original routing, which might
itself have pointed at a different local server.

If nothing changed after connection, restoration reproduces the exact original
file bytes. If unrelated preferences changed, OMM restores only routing fields
and keeps those preferences. A model picked later in the harness (`model`, or
pi's `defaultModel`; the Codex app writes `model` when you switch models) is
kept as the newer choice while the provider and server fields are restored.
If any other routing field was manually changed, restoration
stops before writing any file. Preserve the backup and reconcile the routing
edit yourself; OMM provides no forced overwrite switch. Malformed files,
duplicate JSON keys and symlinked paths are rejected.

A write-ahead backup is saved before changing any harness file. After an
interrupted connection or restoration, rerun `cloud` with the same configuration
directory overrides. Already restored files are accepted and the backup is
removed only after every restore write succeeds. Run `cloud` before connecting
again; repeated local calls never replace the original backup.

The OMM uninstallers refuse `--purge`/`-Purge` while connection backups remain.
Restore each harness with `cloud` first so purging cannot discard the only
original settings copy. After restoration, the owned lock directory can be
removed by an explicit purge.

## Sources and verification boundary

Configuration references reviewed on 2026-10-23:

- [Ollama Claude Code integration](https://docs.ollama.com/integrations/claude-code)
- [Claude settings and precedence](https://code.claude.com/docs/en/settings)
- [Codex configuration reference](https://developers.openai.com/codex/config-reference/)
- [Ollama Codex integration](https://docs.ollama.com/integrations/codex)
- [pi model configuration](https://pi.dev/docs/latest/models)

Observed installed versions: Claude Code 2.1.306 and Codex CLI 0.150.0. Both
consumed OMM-generated routing and returned a synthetic loopback response
(Messages for Claude, streaming Responses for Codex); routing was restored
afterward. pi was not on PATH. Tests use isolated files and real local HTTP
fixture servers; that is contract verification, not a live-model agent file-edit test. Windows,
Linux, real local model tool workflows and restored account sessions require
separate verification. Cluster gateway tokens and MCP registration are deferred
to #503/#504.
