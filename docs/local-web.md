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

**파일·설정** shows storage capacity, managed file size and the cumulative space
saved by adopting external files (`storage_saved_bytes`, the same counter
`omm scan` reports as "saved via omm import"; web adoption adds to it like
`omm import`). External-file search
only creates a preview; adoption requires confirmation of a selected preview
and revalidation of its size/hash. Original runner files may become links during
adoption. Cleanup accepts only previewed incomplete downloads; changed bytes,
timestamps and symlinks are rejected. CLI sharing policies and memory guard
settings are validated and saved while preserving unrelated fields. Merely
opening settings does not initialize or repair the configuration. Web comparisons
remain local regardless of the CLI benchmark-sharing policy.

## Local text conversations

**내 모델 → 대화** selects a linked Ollama or LM Studio model for the **채팅**
screen. Starting a conversation explicitly prepares that runtime. Saved load
settings are reused for a new load; a preloaded model keeps its observed context.
If another model is already loaded, a new load is rejected without unloading it.
Before each reply the GUI checks that the selected load still exists; it does
not implicitly reload an expired model or replace a different runtime instance.

The server reads Ollama's `/api/chat` NDJSON or LM Studio's OpenAI-compatible
`/v1/chat/completions` SSE stream. Browser snapshots display partial text while
the reply is generated. Requests stay on the adapter's validated loopback origin,
ignore proxy configuration, do not follow redirects and do not use a cloud
fallback. No tools are declared or executed. Only completed question/answer
pairs become the next request's context; interrupted and failed answers remain
visible as partial records and are excluded from inference history.

Each request has an idempotency ID. Input is limited to 8192 characters, replies
to at most 1024 output tokens and 16384 characters, and conversations to 40 turns
with a conservative context-character guard and a 512 KiB transcript budget.
The GUI asks for a new conversation instead of silently truncating old turns.
An incomplete runtime stream is never labeled a completed response. Cancelling
closes the owned response connection and waits for the in-flight operation to
finish before accepting another message. It can wait for the runtime's current
read timeout if that runtime has not sent headers or text yet.

UTF-8 question/answer records are written under `OMM_HOME/web-chats`; they are
not included in telemetry. Refresh/navigation preserves the active conversation.
A server restart keeps text, marks an unconfirmed response interrupted, and
requires explicit reconnection. A changed model digest cannot resume the old
history. **대화 기록 삭제** deletes a closed local record after confirmation;
explicit data removal also clears this directory. JSON export is available.

Closing a conversation or shutting down the server cancels its in-flight reply
and releases a load owned by this GUI. A preloaded or externally replaced model
is retained. Failed release remains visible and blocks further management until
the user retries or checks the runtime. Management jobs are blocked while a
conversation owns the runtime. The API obeys the manager's session, Origin and
bounded-input checks. There is no automatic startup/reload of saved chats.

## 로컬 문서에 질문하기

‘내 문서’에서 텍스트 PDF(100쪽 이하), UTF-8 TXT/Markdown, HTML(UTF-8 또는 meta charset 선언), HWPX를 파일당 6 MiB까지 등록합니다. HTML은 script/style 내용을 빼고 본문 텍스트만, HWPX는 본문 문단(표 칸 포함)만 추출하며 본문 텍스트가 없으면 등록 실패로 표시합니다. PDF 지원은 `pypdf` 의존성을 쓰며, 없으면 다시 설치하라는 안내를 표시합니다. 선택한 문서에서 단어가 일치하는 구간을 로컬 검색한 뒤, 연결된 Ollama/LM Studio 모델에 질문과 원문 구간을 보냅니다. 파일명·페이지·추출 텍스트 위치와 원문을 함께 확인하세요. 검색 점수와 출처 번호는 답변 정확도를 보장하지 않습니다. 의미 검색/임베딩·OCR·HWP(한글 97 형식)·fine-tuning은 지원하지 않습니다. 실제 한글 프로그램이 만든 HWPX 파일로는 아직 확인하지 않았습니다(미검증, 손으로 만든 샘플로만 시험).

문서와 검색 색인, 최근 30개 답변은 `OMM_HOME/documents/library.sqlite3`에 저장합니다. 원본 파일은 이동하거나 삭제하지 않습니다. 같은 이름은 명시적으로 교체해야 하며 등록 실패 시 기존 자료를 유지합니다. 교체·삭제하면 이전 답변 기록 전체를 지워 삭제한 원문 인용이 남지 않게 합니다. 최대 100개 문서·200만 자 검색 텍스트를 보관하고, 한 질문에 최대 20개 문서를 선택할 수 있습니다.

각 문서에는 색인을 만든 검색 방식(`local_lexical_v1`)과 분할 설정 버전을 함께 저장합니다. 이후 버전에서 방식이 바뀌면 그 문서는 검색을 거절하고 ‘같은 이름의 문서 교체’로 다시 등록하라고 안내합니다(같은 파일이어도 다시 색인합니다).

답변 모델·실행 앱·선택한 문서는 문서와 별도의 작은 설정(`OMM_HOME/documents/question-settings.json`)으로 기억해 다음에 열 때 복원합니다. 생성 모델을 바꾸면 무엇이 바뀌는지(모델, 실행 앱)와 문서·색인은 그대로라는 설명을 표시합니다. 새 모델이 첫 질문에서 실패하면 마지막으로 답변했던 모델로 선택만 되돌리며 문서·색인·모델 파일은 건드리지 않습니다. 새 모델이 더 좋은 답변을 낸다고 보장하지 않습니다.

모델은 질문 때마다 명시적으로 선택하며 생성 모델을 바꿔도 문서 색인을 다시 만들지 않습니다. 기존 로딩이 있으면 그 작업을 보존하고 질문을 거절합니다. 메모리 여유를 확인한 뒤 이 질문이 로딩한 모델만 해제합니다. 근거가 없으면 모델에 질문을 보내지 않습니다. 문서의 명령문은 지시가 아닌 인용 자료로 취급합니다. 모델이 올바른 출처 번호를 반환하지 않으면 답변 대신 원문을 확인하라는 안내를 표시합니다.
