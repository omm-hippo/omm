# Dependency PR repair, 2026-10-23

PR #509 retains the 16 Dependabot updates. Its build pin assertions now use
build 1.6.1 and hatchling 1.32.4. PR #498 contains only the same certifi changes
already included in #509 and was closed as a duplicate, not merged.

## Unit-verified

A separate virtual environment installed this PR's frozen binary pins together
with the dev, release and train requirements. `uv pip check` verified all 77
installed packages are compatible. With an isolated HOME/OMM_HOME and umask 022,
the complete suite passed: **4200 passed, 37 skipped in 115.03s**, exit 0.
An earlier 300-second run timed out and was not counted as passing.

## Cross-repository dependency

The Homebrew gate reads the live Tap's resource graph. Its 12 runtime resources
still use earlier versions. The companion patch is
https://github.com/omm-hippo/homebrew-omm/pull/27 . That exact candidate Formula
passed the existing OMM graph/URL/SHA-256 check without weakening it. The Tap's
39 unittest cases passed. Its source package remains the published OMM 0.3.105;
revision 1 distinguishes the dependency rebuild. The future pypdf addition is
reported separately by the existing release-lag policy.

The live-Tap gate cannot pass until a matching Tap update is merged. Neither
repository's PR has been merged by this task. Native Homebrew installation of
the candidate Formula is **Not verified** locally; remote CI is separate evidence.
OMM production-host requests: **0**.
