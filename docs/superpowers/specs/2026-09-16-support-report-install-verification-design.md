# Privacy-safe support report and install verification cards

## Goal

Give users two small, understandable safety aids without adding automatic
uploads or new confirmation prompts:

1. a privacy-minimized support document whose complete contents are visible
   before it is saved; and
2. model-install source/check cards that distinguish planned checks from
   checks actually completed.

## Privacy-safe support report

`omm report` builds a new local allow-list document. The default fields are:

- schema version and creation time;
- OMM version and installation source;
- the scrubbed top-level command name;
- the exception class only; and
- doctor overall status plus PASS/WARN/FAIL counts.

The default report never includes usernames, home/project paths, tokens,
environment variables, search text, command arguments, generated text, or
model identifiers. Optional `os`, `policies`, and check-name groups are added
only when explicitly selected with repeated `--include` options. Check details
are never copied, because they can contain paths and model names.

The complete JSON is always printed before any write. `--save PATH` requires
an interactive confirmation, or `--yes` for a non-interactive caller. Existing
files are not replaced without `--force`, and symlink destinations are
refused. The command contains no upload, GitHub, browser, issue, or messaging
path.

## Install source and verification cards

After model resolution but before download, a normal interactive `omm install`
shows:

- provider and repository;
- concrete GGUF filename and expected byte size when known;
- expected OMM hub destination; and
- planned HTTPS, response/provider-size, and SHA-256 checks.

Provider metadata is resolved once and reused by the installer. A previously
downloaded file is described as a verified OMM cache only when its source and
recorded digest still match its actual bytes.

After artifact verification, OMM separately reports the checks actually
performed. A known provider size is enforced against the completed file in
addition to the downloader's response-length checks. SHA-256 wording states
only byte-for-byte agreement with the provider/pinned digest and explicitly
does not claim the file is non-malicious.

The card is presentation, not an additional consent gate. `--quiet`, piped or
non-interactive calls, JSON contracts, and internal contribution installs do
not receive the extra table output. Existing HTTPS downgrade protection,
redirect limits, digest verification, disk checks, atomic download behavior,
and engine/model ownership rules remain unchanged.

## Non-goals

- No problem report is uploaded or submitted automatically.
- No model provider, engine, authentication, or global network policy is added.
- No merge, release, deployment, or user-install mutation is part of this work.

## Verification

Tests use temporary OMM homes, fake pending errors containing token/path/model
strings, fake provider metadata and downloads, and isolated wheel installs.
Report verification checks both allowed fields and forbidden-value absence.
Install verification checks card wording, cache identity, provider-size
mismatch refusal, and unchanged script/non-interactive behavior.
