# Local agent connections

Related issue: #502, local Ollama phase. Install Claude Code, Codex CLI or pi
separately, then install a local Ollama model with tool support and at least
32K context capacity. 64K+ is recommended. OMM does not install the harness or
download a model in these commands.

```sh
omm agent local claude --model YOUR_INSTALLED_MODEL
omm agent status claude
omm agent cloud claude
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
and keeps those preferences. If a routing field was manually changed, restoration
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
