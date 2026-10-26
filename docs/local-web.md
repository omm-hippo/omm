# Local model manager

Start the installed manager with `omm web --open`. The page and API run on the
same loopback origin. Starting without `--open` prints the URL and preserves
the current browser focus. Choose a model, confirm installation and a runner,
then inspect the installed file and link result under **My models**.

## Connect to a shared AI server

Choose **공유 AI** and enter the private LAN or Tailscale gateway address and
the employee token issued by its administrator. **연결 확인하고 저장** makes
one authenticated model-list request and saves the connection only after a
supported response. On the same address, leaving the token field blank reuses
the saved token. A changed address requires a new token.

Choose the model name confirmed by the administrator before sending a question.
The returned catalog can include models that are not running; catalog access
alone is not generation verification. The screen displays actual-response
confirmation only after a successful question. No installation, restart or
unload command is sent. Token, network, certificate, unsupported-response,
model-request and quota problems have separate next steps; network failure
does not establish that Tailscale itself is disabled.

Enter sends a question, Shift+Enter inserts a newline, and IME composition Enter
does not send. Up to 12 recent completed turns are sent within a bounded UTF-8
request size; older context is omitted when necessary. Questions and
answers stay in memory and disappear on reload; the address, private token,
last catalog and last successfully used model persist locally. Reloading does
not contact the shared server. **저장된 연결 지우기** deletes the local connection
after confirmation, without revoking the server token. Existing local chat,
RAG remote settings and agent settings are independent.

## Start a local server, recover, and choose by purpose

**연결 상태 → Ollama 서버 켜기** checks the existing API first. If it is off,
confirmation authorizes the mandatory native app safety checks and background
`ollama serve` on an explicit IPv4 loopback address. A running server is reused;
other processes and loaded models are never stopped. The manager tracks the
process it creates and stops only that process when the manager server exits.
Closing a browser tab does not stop the manager. LM Studio still needs its own
local server switch. No native window is foregrounded and no safety bypass is
available. Server readiness is separate from verified model generation.

Failed jobs and chat loads expose typed recovery actions: a stopped server
opens the start helper or connections, memory shortage opens the minimal-memory
recommendations, and insufficient disk space opens **파일·설정** to preview
cleanup targets. Files are deleted only after selecting targets and confirming
the existing cleanup action. Unknown errors open diagnostics. After recovery,
retry the failed preparation explicitly; the helper does not replay it silently.

In **추천**, choose **한국어 대화**, **코딩**, or **문서 질문**. The guide filters
hardware-eligible recommendations by sourced wiki tasks before shortlisting
at most three packages. It preserves the existing memory and recommendation
rules, explains the match, and links to sources/cautions. Multilingual support
is not measured Korean quality. Document candidates consume text excerpts from
**내 문서**; this does not imply native PDF/image support. No match is shown as
an empty state rather than an unsupported recommendation. When the local
recommendation cache lacks exact wiki matches, up to three wiki entries are
offered for explicit package lookup instead. They are clearly labeled as
unverified for package identity and memory fit until that lookup succeeds.

## Prepare a local model and start a conversation

Choose **이 모델로 대화 시작** in recommendations or a wiki package, or
**대화 준비하고 시작** for an existing managed model. One confirmation covers
the selected runner's installation when needed, native application safety checks,
the model download/link, a short real response check, and opening chat. A new
load is refused while another model is already loaded; OMM never unloads that
other model to make room. The
runner's local API server must be enabled; OMM stops before downloading when it
cannot reach that server and does not foreground an application to start it.

The work view shows six durable stages. Success requires an actual response;
downloaded files alone cannot open chat as verified. Once preparation succeeds,
the browser opens the existing consented chat connection automatically. After
reloading the page, use **대화 열기** from that completed job.

**중단** requests cancellation. Native installation or safety checks already in
progress finish before the next stage is skipped. **다시 시작** asks for consent
again and uses the recorded package and runner, even after a server restart.
Existing verified files are reused by the normal installer; failed steps do not
delete the file or relax safety, hash, memory or runtime checks. The user can
always inspect the recorded failed stage and error first.

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

## Workspace and reading controls

The local manager provides a collapsible sidebar, a breadcrumb, and a
searchable menu for model management. Click
**메뉴 검색** or press Cmd/Ctrl+K within the page, enter a task such as `근거 질문`
or `서버`, and open a matching destination. Enter opens the first result; Tab
moves through results; Escape closes the search. The shortcut does not interrupt
an already open confirmation dialog. On narrow screens the sidebar is a drawer;
changing to a narrow viewport closes it and the main area is inert while it is open.

**화면 모드** offers system, light, and dark appearances. Only this display
preference is saved in the browser (`omm.web.appearance.v1`). Blocking browser
storage does not prevent the manager from opening. This preference is separate
from server-side model, telemetry, and runtime settings.

Chat answers and document answers render Markdown headings, lists, tables and
code. **코드 복사** copies the code without formatting; **답변 복사** copies the
original Markdown. Copying requires a user click and browser clipboard access.
Raw HTML is not executed and image references from model output do not fetch
remote images. Existing document source excerpts remain plain text and separate
from generated answers.

You can prepare a question before connecting a model. Starter prompts fill the
composer but never send or connect automatically. Choose Enter or Cmd/Ctrl+Enter
in **전송 키**; Shift+Enter always inserts a newline. Composition Enter is ignored
while entering Korean/Japanese text. Drafts, selected documents, and active chat
polling survive menu navigation within the page. Unsent drafts are held in memory,
not saved to browser storage, and are lost on a full page reload. Completed
conversation history continues to use the existing local JSON store.

Frontend checks: `npm --prefix web-ui test` and `npm --prefix web-ui run build`.
The build includes runtime license texts in `static/third-party-licenses.txt`.
Browser rendering and fixture-model tests do not prove actual Ollama/LM Studio
generation or an installed package upgrade; verify those paths separately.

## Click launchers

After OMM is installed, `packaging/web-launchers/Open-OMM.command` on macOS or
`Open-OMM.cmd` included beside a Windows portable `omm.exe` opens the manager. The macOS
launcher checks common installed CLI locations and explains missing installs.
`omm web --create-launchers DIRECTORY` creates personal launchers bound to the
current executable. Existing unrelated files are not overwritten.

These launchers start the local manager; they do not install Python or OMM on a
clean Mac. Login auto-start is off by default and is registered only by
`omm setting web-autostart enable` (launchd on macOS, systemd --user on Linux,
the per-user Run key on Windows). It starts `omm web` without opening a browser;
`omm setting web-autostart disable` unregisters it and stops the copy it started,
keeping models and settings. Real Finder double-click, sign-in auto-start and
Windows installation are separate acceptance checks from wrapper tests.
First-time install help in Korean: [beginner-install.md](beginner-install.md).
Korean demo checklist and feature status: [mentoring-demo.md](mentoring-demo.md).

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
saved by adopting external files (`storage_saved_bytes`; web adoption adds to
it like `omm import`). `omm scan` instead reports the space saved right now by
every hard link or symlink omm owns, installs included. External-file search
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

## 웹에서 공유하기 (내 문서 탭)

‘내 문서’ 탭은 `omm rag` 컬렉션·공유 명령을 화면으로 옮긴 것입니다. 권한 규칙과 오류 문장은 CLI와 같습니다([사내 공유](rag-mcp.md#사내-공유)).

- **📍 지금 보는 곳** — `이 PC`(이 PC의 문서 저장소, 관리자) 또는 `회사 head (<주소>) · 나: <출입증 이름>`. [회사에 연결]은 head 주소와 직원 출입증(토큰)을 받아 `omm rag remote set`과 똑같이 head에 토큰을 보여 이름을 확인한 뒤에만 저장합니다. [이 PC로 돌아가기]는 `omm rag remote clear`와 같습니다.
- **컬렉션** — 왼쪽 목록에 이름, 문서 수, 만든 사람, 내 역할(관리자 / 주인 / 공유받음)이 보입니다. [+ 새 컬렉션]으로 만들고, 주인·관리자는 확인 후 지울 수 있습니다(기본 컬렉션은 지울 수 없음).
- **문서** — 주인·관리자는 파일을 올리고(같은 이름 교체 가능) 지웁니다. 올리기는 작업(worker)으로 돌고 진행 막대를 보여 줍니다. head로 올릴 때는 “회사 head로 보내고 색인 만드는 중”으로 표시되고, head가 텍스트 추출과 색인을 마칠 때까지 최대 15분 기다립니다.
- **공유** — 주인·관리자에게만 보입니다. 👤 사람(출입증 이름)과 👥 팀 칩이 있고 ✕로 공유를 거둡니다. [사람 추가]·[팀 추가]는 CLI처럼 있는 출입증 이름과 팀만 받습니다.
- **질문하기** — 이 PC 모드에서는 지금처럼 고른 로컬 모델이 고른 문서로 답하고 출처를 보여 줍니다. [답변 없이 근거만 찾기]는 모델 없이 그 컬렉션의 원문 구간만 찾습니다. 회사 head 모드에서는 답변을 만들지 않고 head가 찾은 근거 구간을 원문 그대로 보여 줍니다.
- **AI 연결** — Claude Code·Codex·Cursor·pi마다 연결 상태(이 PC 문서 / 회사 head 문서)와 [연결]·[해제]가 있습니다. `omm rag connect`(회사 head 모드에서는 `--remote`)·`disconnect`와 같은 백업·복원 방식입니다. 연결한 뒤에는 그 도구를 다시 시작해야 합니다.
- **팀 관리** — 이 PC 모드(회사 head를 정하지 않은 관리자)에서만 보입니다. 팀 만들기·지우기, 사람 넣기·빼기.
- **직원 출입증 (토큰)** — 이 PC가 클러스터 head(`cluster.json`의 role이 head)이거나 이미 `OMM_HOME/cluster/tokens.json`이 있을 때만 보입니다. 새 출입증은 만들 때 한 번만 보여 주고(복사 버튼, “다시 볼 수 없어요”), 목록에는 이름과 만든 때만 나옵니다. 취소는 확인 후 바로 적용됩니다.

직원 토큰은 `OMM_HOME/rag-remote.json`에만 있고 브라우저로 보내지 않습니다. API 응답, 작업 기록(`web-jobs`), 오류 문장 어디에도 들어가지 않습니다. 예외는 관리자가 새 출입증을 만들 때의 그 한 번 응답뿐입니다. 모든 변경 요청은 다른 관리 기능과 같은 세션 쿠키·같은 출처 머리글 검사를 받고, 지우기·공유 거두기·해제·연결 끊기는 확인을 거칩니다.

