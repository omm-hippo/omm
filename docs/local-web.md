# Local model manager

Start the installed manager with `omm web --open`. The page and API run on the
same loopback origin. Starting without `--open` prints the URL and preserves
the current browser focus. Choose a model, confirm installation and a runner,
then inspect the installed file and link result under **My models**.

Select **실행 확인** after enabling the runner's local server. The manager asks
before loading a model and sends a fixed, short English response probe. This
proves local text generation, not instruction-following accuracy or task
quality. It records compatibility separately from installation, shows the
actual sample, and releases only its own load. Existing loaded models keep
the observed context and are not unloaded. A failed check preserves the file.
Web checks currently support Ollama and LM Studio's native API. Other runner
links can be managed but are not labeled generation-verified.

Memory headroom is checked before a new load. The manager does not reclaim
another application's model to make room. Runtime API errors and memory
pressure are shown as failures; a downloaded file alone is not a successful
response check. Finishing an in-flight response check can delay server shutdown
while its owned load is released.

## Click launchers

After OMM is installed, `packaging/web-launchers/Open-OMM.command` on macOS or
`Open-OMM.cmd` included beside a Windows portable `omm.exe` opens the manager. The macOS
launcher checks common installed CLI locations and explains missing installs.
`omm web --create-launchers DIRECTORY` creates personal launchers bound to the
current executable. Existing unrelated files are not overwritten.

These launchers start the local manager; they do not install Python or OMM on a
clean Mac. Login auto-start is not registered. Real Finder double-click and
Windows installation are separate acceptance checks from wrapper tests.

## Recommendation provenance and descriptions

The page uses its cached catalog or bundled rules until **추천 자료 업데이트**
explicitly fetches and verifies new catalog data. Whole-catalog totals, local
speed correction, and matching feature-configuration counts are separate.
Training support excludes synthetic bootstrap rows and deduplicates repeated
feature vectors. Exact checkpoint identity, independent devices and calibrated
confidence remain unknown when the source does not provide them. Older signed
catalogs remain readable and show missing support instead of invented numbers.
The same evidence fields are carried in CLI JSON and selection details.

The model-description view reuses omm.run's pinned bilingual catalog, version
2026-10-01.1 at commit 54f386d7471dfb3da233adeb9c3beed2fe4787ae. Publisher claims,
editorial cautions, source URLs, review dates and null OMM evaluations are kept.
Package matching uses exact repositories or separately reviewed base-model
relationships, never a name-family guess. Reviewed relationships are in
`src/omm/model-wiki-matches.json`; unknown uploads and different sizes/versions
remain unmatched. Text stays local and is available offline.

## Demonstration and human usability checks

Exercise installation -> runner link -> response -> owned-load cleanup ->
restart -> stored outcome -> removal in isolated storage first. Check desktop
and narrow screens, error paths, duplicate submissions and missing APIs.
Record the environment and whether each result came from a real service or a
fixture. For later human evaluation, record the task, assistance requested,
failed step and recovery result without claiming bot tests are human feedback.
No participant recruitment, personal data collection or performance score is
implied by this scenario.

## Wiki search and management screens

The default **모델 찾기** page reuses the existing bilingual wiki. Search covers
model names, repositories, descriptions, strengths and use-case labels; purpose
filters preserve the wiki's original categories. **설치 파일 보기** shows exact
repositories and reviewed base-model relationships from the local catalog.
An explicit online search makes at most nine bounded Hugging Face metadata
requests (one search and up to eight repositories), with no redirect following
or retry. Other uploads must declare the exact original repository in their
model card and retain that declaration in the detailed response. These are
publisher declarations, not OMM quality verification. Split shards are excluded
from the single-file installer. Unknown or over-budget memory fits cannot be
installed from this selector. Model-description versions and sources remain
visible, including absent OMM quality evaluations.

**연결 상태** lives in secondary navigation. App installation, local API response,
and actual model generation are distinct. External connectivity is checked only
by its button, with one HEAD request each to fixed Hugging Face and GitHub raw
destinations, no download/retries/redirect following. **진단** reuses the read-only
doctor checks and remediation guidance, without running repair commands.

**성능 비교** accepts one to four managed models linked to the chosen Ollama or
LM Studio runtime. It uses a fixed 4096-token context, 128 batch size, eight
questions from the existing arithmetic smoke pack, a final-number-only prompt
with at most 64 response tokens, and three 64-token speed probes. The separate
`omm-web-arithmetic-speed-v1` protocol must not be mixed with CLI benchmark or
leaderboard scores. Reports record pack/hash/version, model byte hashes,
observed settings, speed samples, timestamps and hardware in `evaluations`.
Generated text is not retained or uploaded. Missing speed, unsuccessful cleanup,
changed model bytes and cancellation do not produce a successful report. The
latest successful report survives refresh/restart and can be saved from the UI.

**실행 설정** in **내 모델** reuses the core profile API. Baseline and recommended
short response trials must pass and release owned loads before a profile is
saved; the previous revision is preserved on failure. Restoring uses the model's
current digest. Saving, restoration and comparison run as serialized durable
jobs. Existing loaded models are never unloaded to make room for these trials.

**파일·설정** shows storage capacity and managed file size. External-file search
only creates a preview; adoption requires confirmation of a selected preview
and revalidation of its size/hash. Original runner files may become links during
adoption. Cleanup accepts only previewed incomplete downloads; changed bytes,
timestamps and symlinks are rejected. CLI sharing policies and memory guard
settings are validated and saved while preserving unrelated fields. Merely
opening settings does not initialize or repair the configuration. Web comparisons
remain local regardless of the CLI benchmark-sharing policy.
