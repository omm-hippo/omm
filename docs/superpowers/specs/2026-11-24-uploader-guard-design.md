# Uploader guard

## Problem

`omm recommend` candidates come from HuggingFace/ModelScope sorted by
downloads, and `omm search` shows any repo with a `.gguf` file. The only
content filter is `FAKE_PROVENANCE_MARKERS` (repo-name spam). The signed
catalog and the provider SHA-256 check prove the file is the one the uploader
published - not that the uploader is trustworthy. Today roughly half of the
nightly candidates come from individual accounts nobody has vetted.

GGUF has no pickle-style code execution, so the residual risk is malformed
files aimed at runner parser bugs, hostile chat templates, and tampered
weights. None of that is detectable by scanning; who published the file is
the strongest signal OMM has.

## Design

One policy, `uploader_guard_policy` in config, set with
`omm setting uploader-guard --policy strict|warn|off` (also in the bare
`omm setting` menu, next to Memory guard).

| Policy | recommend / search / "did you mean" | `omm install <repo>` |
|---|---|---|
| `warn` (default) | everything shown; non-allowlisted rows flagged "unverified uploader" | one warning line, continues |
| `strict` | non-allowlisted rows hidden (with a count of what was hidden) | asks "Install anyway?"; `--yes` skips the prompt |
| `off` | pre-guard behavior, plus one muted "uploader guard is off" line | no check |

- The allowlist is code (`search.TRUSTED_UPLOADERS`), per provider, so a
  trusted ModelScope org name registered by a stranger on HuggingFace does
  not inherit trust. Changing it goes through PR review. Matching is
  case-insensitive on the `org` part of `org/repo`.
- Curated defaults (`hub.CURATED_INDEX`) are always trusted: maintainers
  picked them.
- A direct URL install (no repo id) is not checked - the user typed the URL.
- The nightly `published/candidates.json` is unchanged: the default is
  `warn`, so the client needs the full list; `strict` filters client-side.
- JSON output: recommend rows carry the reason in the existing `warning`
  field; search rows gain `trusted_uploader: bool`.

## Out of scope (follow-ups)

- `omm contribute` (benchmarks catalog candidates for telemetry), the local
  web UI, and cluster commands still rank the full candidate list.
- Pulling HuggingFace's own repo security-scan status.
- User-extensible allowlist (`--trust <org>`).
