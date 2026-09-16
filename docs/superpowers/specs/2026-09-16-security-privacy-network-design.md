# OMM security, privacy, and network controls

## Goal

Give ordinary users understandable controls for local-server exposure,
credentials, outbound networking, support reports, and model-install trust
without weakening OMM's existing checksum, signature, redirect, loopback,
consent, or ownership boundaries.

## Engine exposure

`omm engine security [ENGINE]` inspects Ollama and LM Studio only. It maps
live TCP listeners to `local_only`, `external_allowed`, or `unknown` and
shows the addresses that produced the verdict. A port alone is not an engine
identity: the listener process must match the expected engine executable.

`--fix-local-only` is deliberately narrower than inspection. It may restart
an Ollama server only when OMM previously started it and a fresh PID, process
start-time, executable, and command-line check still proves that identity.
Before a restart, the CLI states that active local clients disconnect and
the server is restarted on loopback. Interactive use requires confirmation;
`--yes` is the explicit non-interactive consent. LM Studio and servers not
owned by OMM are never restarted or reconfigured.

Every Ollama daemon OMM starts receives an explicit loopback `OLLAMA_HOST`.
The ownership receipt lives under `OMM_HOME`, contains no secret, and is
removed when OMM stops that daemon.

## Credentials

`omm auth login/status/logout [PROVIDER]` supports `huggingface` and
`lmstudio`. `HF_TOKEN` and `LM_API_TOKEN` remain the highest-priority source.
Otherwise OMM uses only the native operating-system store: macOS Keychain via
`security` with stdin-only secret entry, Windows Credential Manager via its
native API, or Linux Secret Service via `secret-tool` with stdin-only secret
entry. No alternate-file backend exists. If a supported backend is absent,
the CLI does not persist or solicit the token and points users to the existing
per-session environment variable flow.

Tokens are entered by hidden prompt or stdin, never by a command-line value.
They are never written to `config.json`, `.env`, logs, exceptions, or support
reports. Hugging Face requests attach the token only to Hugging Face hosts;
manual redirects recompute headers so credentials cannot cross hosts. LM
Studio tokens are used only for the already loopback-restricted local API.
The login copy asks for read-only model access and explains that gated-model
approval is a separate Hugging Face action.

## Network modes

`network_mode` is one of `online`, `models-only`, and `offline`.

- `online` preserves today's behavior and every existing upload consent.
- `models-only` permits model provider search/listing, signed recommendation
  catalog refreshes, model metadata, and model downloads. It suppresses
  telemetry, crash-report and usage-stat uploads plus OMM update checks.
- `offline` permits loopback runtime traffic only. External HTTP/DNS, Git
  fetches, package-manager mutations, and update checks are refused.

`--offline` is a one-process override and works before or after the command
name. It never rewrites the saved mode. External HTTP enforcement happens
below the command layer so a missed call site still fails closed. Model-only
exceptions are scoped to known providers/catalog hosts and the downloader's
explicit model-transfer context. Git and package-manager entry points also
perform explicit checks.

Offline install may reuse only a file whose bytes still match an OMM registry
digest for the same source. Recommendation/search commands name the verified
cache or static fallback they used and explain how to switch modes. These
settings do not control communications initiated independently by Ollama or
LM Studio.

## Privacy-safe support report

`omm report` builds a new local allow-list document. Defaults are schema/time,
OMM version, install source, a scrubbed command name, exception class, and
doctor status counts. It never includes usernames, home or project paths,
tokens, environment variables, search text, command arguments, generated
text, or model identifiers. Optional OS, policy, network-mode, and check-name
groups are included only when selected with `--include`.

The complete JSON is always printed before any save. Saving requires an
explicit path plus interactive confirmation, or `--yes` for a script. The
command has no upload, GitHub, browser, issue, or messaging path.

## Install source card

Plain interactive `omm install` shows provider, repository, file, size,
format, destination, and the planned HTTPS, size, and SHA-256 checks before
the transfer. It does not add a confirmation. Quiet/unattended internal
flows stay quiet. Provider metadata is resolved once and reused by the
installer.

After installation, OMM prints actual HTTPS, byte-count, and digest results
separately. A digest result says only that bytes matched the provider or
pinned digest; it never claims the file is non-malicious. Known provider
sizes are enforced in addition to the downloader's response-length checks.

## Compatibility and non-goals

- Existing upload consent remains authoritative in `online` mode.
- Existing SSH update signatures, catalog signatures, HTTPS downgrade and
  redirect limits, SHA-256 verification, loopback runtime validation, and
  engine/model ownership records stay in force.
- No production endpoint is needed for tests. Tests use temporary OMM homes,
  fake tokens/backends, loopback HTTP, and documentation-only reserved IPs.
- The feature does not change Ollama or LM Studio settings owned by another
  app or user, control those apps' independent outbound traffic, upload a
  support report, merge a PR, release, or deploy.
