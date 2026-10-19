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

## 로컬 문서에 질문하기

‘내 문서’에서 텍스트 PDF(100쪽 이하), UTF-8 TXT/Markdown을 파일당 6 MiB까지 등록합니다. 선택한 문서에서 단어가 일치하는 구간을 로컬 검색한 뒤, 연결된 Ollama/LM Studio 모델에 질문과 원문 구간을 보냅니다. 파일명·페이지·추출 텍스트 위치와 원문을 함께 확인하세요. 검색 점수와 출처 번호는 답변 정확도를 보장하지 않습니다. 의미 검색/임베딩·OCR·HWP·HTML·fine-tuning은 지원하지 않습니다.

문서와 검색 색인, 최근 30개 답변은 `OMM_HOME/documents/library.sqlite3`에 저장합니다. 원본 파일은 이동하거나 삭제하지 않습니다. 같은 이름은 명시적으로 교체해야 하며 등록 실패 시 기존 자료를 유지합니다. 교체·삭제하면 이전 답변 기록 전체를 지워 삭제한 원문 인용이 남지 않게 합니다. 최대 100개 문서·200만 자 검색 텍스트를 보관하고, 한 질문에 최대 20개 문서를 선택할 수 있습니다.

모델은 질문 때마다 명시적으로 선택하며 생성 모델을 바꿔도 문서 색인을 다시 만들지 않습니다. 기존 로딩이 있으면 그 작업을 보존하고 질문을 거절합니다. 메모리 여유를 확인한 뒤 이 질문이 로딩한 모델만 해제합니다. 근거가 없으면 모델에 질문을 보내지 않습니다. 문서의 명령문은 지시가 아닌 인용 자료로 취급합니다. 모델이 올바른 출처 번호를 반환하지 않으면 답변 대신 원문을 확인하라는 안내를 표시합니다.
