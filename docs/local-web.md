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
