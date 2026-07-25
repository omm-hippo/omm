# Security Policy

## Supported Versions

`omm` is distributed as a single rolling release (no long-term-support
branches). Only the latest published version on PyPI/pipx receives security
fixes.

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for a suspected security
vulnerability. Instead, email **seong381400@gmail.com** with:

- A description of the vulnerability and its potential impact
- Steps to reproduce (a minimal repro is ideal)
- The `omm` version and OS you tested on

You should receive an acknowledgement within a few days. We'll work with you
to confirm the issue, prepare a fix, and coordinate disclosure timing before
any public write-up.

## Scope

Areas of particular interest for reports:

- The model download/install pipeline (`omm install`), including artifact
  integrity verification and symlinking into LM Studio/Ollama/other engines
- Signed recommendation-model manifests (`omm setting catalog-trust`,
  Ed25519 verification)
- The optional `localfit-server` telemetry/benchmark collector, including
  `LOCALFIT_ADMIN_TOKEN` handling
- Local credential/config storage under the user's home directory
