# Security Policy

## Supported Versions

omm follows a rolling-release model and does not maintain long-term-support
branches.

| Version | Security support |
| --- | --- |
| Latest published release | Supported |
| Older published releases | Not supported |
| Current `main` branch | Reports welcome; this is unreleased development code |

Official distribution channels can temporarily provide different versions.
Include `omm --version` and the installation method in a report, and compare
them with the latest [GitHub release](https://github.com/omm-hippo/omm/releases).

## Reporting a Vulnerability

Do **not** open a public GitHub issue, discussion, or pull request for a
suspected vulnerability. The repository does not currently enable GitHub's
private vulnerability-reporting form.

Email **omm.hippo@gmail.com** with the subject **[Security]** and include:

- A concise description and the potential impact.
- The affected omm version or commit and installation method.
- Operating system, architecture, Python or Node.js version, and affected
  local runner where relevant.
- Minimal reproduction steps or a proof of concept.
- Redacted logs or screenshots that help confirm the behavior.

Do not send access tokens, service-account credentials, unredacted private
paths, or third-party personal data. Do not test against systems, accounts,
services, or data you do not own or have permission to assess. Avoid
high-volume testing against public or quota-limited services.

The maintainers aim to acknowledge a report within a few days, confirm the
affected scope, prepare and verify a fix, and coordinate disclosure timing. If
you are unsure whether a report is security-sensitive, use the private email
path.

## Scope

Reports are especially useful for vulnerabilities involving:

- Model download, installation, update, import, linking, cleanup, and file
  integrity.
- Official installation scripts and Python, npm, Homebrew, or portable release
  supply chains.
- Trusted commits, signed recommendation catalogs, checksums, and signature
  validation.
- Archive extraction, path handling, permissions, and cross-volume copies.
- Local configuration, credentials, tokens, or sensitive filesystem state.
- Benchmark, telemetry, error-report, Firebase, `localfit-server`, or
  `cf-worker` authorization and privacy boundaries.
- Supported local AI runner integrations where omm creates or amplifies the
  security impact.

Issues that exist entirely in an upstream model or runner should normally be
reported to that upstream project. If omm's integration changes the impact or
trust boundary, include that detail in a private report to this project too.

## Disclosure and Fixes

Please allow reasonable time for investigation and a coordinated fix before
publishing technical details. Security fixes are supported only when the
patched release or destination artifact has actually been published and
verified; a pull request or successful CI run alone is not a released fix.

## Non-security Bugs

For ordinary bugs, installation problems, and compatibility requests without
a security impact, use the public
[GitHub issue templates](https://github.com/omm-hippo/omm/issues/new/choose).
