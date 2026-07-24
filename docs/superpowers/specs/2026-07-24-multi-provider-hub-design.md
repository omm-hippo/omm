# HuggingFace 외 ModelScope 저장소 직접 연동

## 배경

지금 omm은 모델 소스로 HuggingFace 하나만 안다. `hub.py`의
`resolve_model()`, `search.py`의 `search_huggingface()`, `contribute.py`의
`ref()`, `scripts/fetch_hf_candidates.py`가 전부 `repo_id`를 HF 전용
`org/repo` 문자열로 가정하고, 다운로드 URL도 `huggingface.co/{repo}/resolve/...`
하드코딩이다. 사용자 요청: `omm search`/`omm install`/`omm recommend`/
`omm contribute`/예측 모델(추천 엔진) 학습 파이프라인 전부에서 ModelScope를
API로 직접 조회 가능하게 만든다.

**CivitAI는 이번 작업 범위에서 제외한다** (사용자 확인, 실측 후 결정):
CivitAI는 검색 API는 공개지만 실제 파일 **다운로드는 API 키 없이 401**로
막힌다 (`curl -H "Range: ..." civitai.com/api/download/models/745392` →
`401`, 익명 확인). 게다가 CivitAI에서 실제로 찾을 수 있는 "GGUF" 모델은
Stable Diffusion(Flux) 확산모델뿐이고, 그나마도 파일이 `.zip`으로 감싸져
있어(`ggufFastfluxFlux1Schnell_q40V2.zip`) 순수 `.gguf` 파일명 필터를
통과하는 경우가 사실상 없다. omm이 다루는 llama.cpp/Ollama LLM 용도와도
안 맞아 - 검색만 되고 설치는 거의 항상 실패하는 기능을 만들 가치가 없다고
판단해 제외했다.

**스코프 결정** (사용자 확인):
- `omm install`에서 provider 접두사(`hf:`/`ms:`)는 선택 사항. 생략하면
  HF와 ModelScope 양쪽을 조회해서, 매치가 하나면 바로 설치하고 둘 다
  매치하면 사용자에게 고르게 한다.
- 멀티스레드 다운로드: `downloader.download_file()`은 순수 URL 문자열만
  받아 동작하므로 provider가 `download_url(repo_id, filename) -> str`만
  내놓으면 원칙적으로 그대로 탄다 - 단, **실측 결과 ModelScope의 다운로드
  엔드포인트는 Range 요청을 실제로 존중하지만(요청한 바이트 수만큼만
  정확히 내려줌, `Content-Range` 헤더 정확) HTTP 상태 코드를 206이 아니라
  200으로 돌려준다** (`curl -H "Range: bytes=100-199" ... -D -` → 실측
  `HTTP/1.1 200 OK` + `Content-Length: 100` + `Content-Range: bytes
  100-199/491400032`). `downloader.py`의 `_probe_range_support()`와
  `_download_range_worker()`는 지금 `resp.status_code == 206`을 하드
  요구하기 때문에, 고치지 않으면 ModelScope 다운로드는 항상 싱글스레드
  경로로만 빠진다. 이 픽스를 이번 작업에 포함한다 (아래 "다운로드 Range
  판별 수정" 절).

## 확인된 API 스펙 (실제 호출로 직접 검증함)

**ModelScope** (base `https://modelscope.cn`, 공개 모델 조회/다운로드에
토큰 불필요 - 실제 검색 API와 파일 목록 API를 curl로 직접 호출해 확인):

- 검색: `GET /openapi/v1/models?search=&page_number=&page_size=`
  응답 형태 (실측, `Qwen2.5-0.5B-Instruct-GGUF` 검색 결과):
  ```json
  {"success": true, "data": {"models": [
    {"id": "Qwen/Qwen2.5-0.5B-Instruct-GGUF", "downloads": 50622,
     "params": 630167424, "tags": ["library:gguf", "task:text-generation", ...],
     "private": false, "gated": false}
  ], "total_count": 117, "page_number": 1, "page_size": 3}}
  ```
  `id`가 `owner/name` 형식 (HF와 동일). `tags`에 `"library:gguf"`가 있으면
  그 레포에 GGUF 파일이 있다는 신호 (파일 목록 자체는 이 응답에 없음 -
  실제 파일명을 얻으려면 레포별로 파일 목록 API를 한 번 더 호출해야 함).
- 파일 목록: `GET /api/v1/models/{repo_id}/repo/files?Revision=master&Recursive=True`
  응답 형태 (실측):
  ```json
  {"Code": 200, "Data": {"Files": [
    {"Name": "qwen2.5-0.5b-instruct-q4_k_m.gguf",
     "Path": "qwen2.5-0.5b-instruct-q4_k_m.gguf",
     "Size": 491400032, "Sha256": "74a4da8c...", "IsLFS": true, "Type": "blob"}
  ]}}
  ```
  파일명은 `Path` (서브디렉터리 없는 레포에선 `Name`과 동일), 크기는
  `Size`, 해시는 `Sha256` - HF처럼 별도 LFS paths-info 호출이 필요 없고
  이 응답 하나로 파일명/크기/해시가 다 나온다.
- 다운로드: `GET /api/v1/models/{repo_id}/repo?Revision=master&FilePath=<path>`
  (URL 조립만으로 완성, 별도 요청 불필요). Range 헤더를 실제로 존중함
  (위 "스코프 결정" 절 참고 - 단 상태 코드는 200).

## 아키텍처

```
src/omm/providers/
  __init__.py      # PROVIDER_NAMES: 접두사→provider 이름 매핑
  base.py          # ModelResolutionError, AmbiguousModelError, AmbiguousProviderError
  huggingface.py   # hub.py의 기존 HF 관련 함수 이동 (동작 100% 동일)
  modelscope.py    # 동일 인터페이스, ModelScope API로 구현
```

각 provider 모듈은 동일한 4개 함수를 구현한다:

```python
def fetch_repo_files(repo_id: str) -> tuple[list[str], float | None]:
    """(.gguf 파일명 목록, repo-level 파라미터 수 billions 추정치 - 모르면 None)"""

def download_url(repo_id: str, filename: str) -> str: ...

def remote_file_size(repo_id: str, filename: str) -> int | None: ...

def remote_file_sha256(repo_id: str, filename: str) -> str | None: ...
```

`hub.py`는 이 함수들을 provider별로 라우팅하는 얇은 레이어로 남는다.
`ResolvedModel`에 `provider: str | None` 필드를 추가한다
(`"huggingface"` | `"modelscope"`; 직접 URL 설치는 호스트가
`huggingface.co`/`modelscope.cn`이면 자동 판별, 모르는 호스트면 지금처럼
`None`).

## install ref 문법과 provider 판별

`resolve_model(model_name)`의 분기 순서 (기존 분기 뒤에 새 분기를 끼워 넣는
형태로, 기존 동작은 전부 보존):

1. `CURATED_INDEX` 히트 → 지금과 동일 (HF 고정).
2. `http(s)://` 시작 → 지금과 동일하게 직접 URL 설치. 추가로 호스트가
   `huggingface.co`/`modelscope.cn`이면 `provider`를 채운다.
3. 명시적 접두사 (`hf:` 또는 `huggingface:`, `ms:` 또는 `modelscope:`) →
   접두사 제거하고 해당 provider로 위임. 나머지 파싱 규칙(`:filename` 옵션
   등)은 HF 기존 로직과 동일하게 provider별로 반복.
4. 접두사 없고 `/` 포함 (`org/repo[:file]` 형태) → HF와 ModelScope 양쪽에
   `fetch_repo_files`를 순차 시도한다 (404는 빠르다, 병렬화 불필요). 매치가
   하나뿐이면 그 provider로 진행. 둘 다 매치하면
   `AmbiguousProviderError(repo_id, providers=["huggingface","modelscope"])`를
   던져서 CLI가 provider 선택 프롬프트를 띄운다 (기존
   `AmbiguousModelError`의 파일 선택 프롬프트와 같은 UX 패턴). 둘 다 없으면
   기존 `ModelResolutionError`.
5. 그 외 → 기존과 동일한 "unknown model" 에러, 메시지에 큐레이션 이름 +
   접두사 문법 안내를 추가.

`AmbiguousModelError`/`AmbiguousProviderError`는 `providers/base.py`에
정의하고 `hub.py`가 재수출한다 (`from omm.hub import ...`로 쓰는 기존
`cli.py` 임포트가 그대로 동작). `AmbiguousModelError`에 `provider: str =
"huggingface"` 키워드 인자를 추가한다 (디폴트값으로 기존 위치 인자 호출
`AmbiguousModelError(repo_id, candidates)`를 쓰는 기존 테스트들이 안 깨짐).

`cli.py`의 install 커맨드가 이미 `AmbiguousModelError`를 잡아 인터랙티브
선택 프롬프트를 띄우는 지점에 `AmbiguousProviderError` 처리 분기를
추가한다. 퀀트 재선택 후 재귀 호출(`install(f"{e.repo_id}:{chosen}", ...)`)은
`install(f"{e.provider}:{e.repo_id}:{chosen}", ...)`로 바꿔서 provider
접두사를 명시 - 안 그러면 재귀 호출에서 다시 양쪽 provider를 조회하다가
드물게 재차 애매해질 수 있다.

## `omm search` 통합

`search.py`에 `search_modelscope(query)`를 `search_huggingface()`와 같은
반환 모양(`{"name", "repo_id", "filename", "description", "provider"}` dict
리스트)으로 추가한다. ModelScope 검색 API 응답엔 파일 목록이 없으므로,
`tags`에 `"library:gguf"`가 있는 상위 N개(최대 15개)에 한해 레포별로
`modelscope.fetch_repo_files()`를 추가 호출해서 대표 GGUF 파일 하나를
고른다 (기존 `search.pick_gguf_file()`이 받는 `siblings: list[{"rfilename":
...}]` 모양으로 어댑팅해서 재사용 - 로직 중복 없음). `_claims_fake_provenance()`
필터를 HF와 동일하게 적용한다. HF 결과에는 `"provider": "huggingface"`를,
큐레이션/캐시 후보 풀에도 `"provider"` 키를 채워 넣는다 (없으면
`"huggingface"`로 취급하는 하위호환 대신, 애초에 항상 채워서 나가는 쪽을
택한다 - 새로 만드는 데이터라 마이그레이션 이슈가 없다).

`cli.py`의 `search()` 커맨드가 `search_modelscope()` 결과를 기존
`local_matches + hf_matches` 병합에 추가해서 같은 fuzzy-match/family-grouping
로직에 그대로 태운다 (이 로직들은 이미 provider-agnostic한 dict 필드
기반이라 수정 불필요).

`catalog.install_ref(candidate)`가 provider를 보고 접두사를 붙여 반환하도록
바꾼다: 큐레이션 이름은 지금처럼 접두사 없음, HF는 지금처럼 `org/repo`
(하위 호환 - 접두사 안 붙임), ModelScope는 `ms:org/repo`. 검색 결과를
그대로 복붙해서 install할 수 있어야 하므로 이 부분은 정확해야 한다.

## `omm recommend` / `omm contribute` / 예측 모델 학습 파이프라인

- `scripts/fetch_hf_candidates.py` → `scripts/fetch_candidates.py`로 이름
  변경. 기존 `fetch_trending_candidates()`(HF)는 그대로 두고
  `fetch_modelscope_candidates()`를 추가한다. 각 candidate dict에
  `"provider"` 키를 채워 넣는다. `main()`의 dedupe 키를 `repo_id` 단독에서
  `(provider, repo_id)` 튜플로 바꾼다 (ModelScope가 HF와 같은 `org/repo`
  네임스페이스를 쓰므로 우연히 같은 문자열을 쓰는 경우의 충돌 방지).
- `predictor.py`, `featurize.py`, `mltree.py`는 candidate dict를 통째로
  받아 쓰는 구조라 `provider` 키가 추가돼도 깨지지 않는다 - 코드 변경 없음
  (`featurize.py`의 `repo_id.rsplit("/", 1)[-1]`도 ModelScope 형식에
  그대로 통한다).
- `contribute.py`의 `ref(candidate)`를 `f"{candidate.get('provider') or
  'huggingface'}:{repo_id}:{filename}"` 형태로 바꾼다. 기존에 쌓인
  history_refs(구 포맷, provider 접두사 없음)를 HF로 간주해서 매칭하는
  호환 처리를 추가한다 - 안 그러면 이미 벤치마크를 마친 HF 모델들이 `omm
  contribute` 큐에 "새 모델"로 재등장하게 된다.
- `cli.py`의 `_run_contribution_loop`가 `HF_DOWNLOAD.format(...)`을
  하드코딩하는 부분(`cli.py:2727`)을 `hub.download_url(candidate.get(
  "provider") or "huggingface", candidate["repo_id"], candidate["filename"])`
  호출로 바꾼다.
- `scripts/train_model.py`는 `published/candidates.json`을 완전히 opaque한
  dict 리스트로 읽어서 그대로 아티팩트에 통과시킬 뿐, `repo_id`를
  HF 형식으로 파싱하거나 URL을 만드는 코드가 없음을 소스 확인 완료
  (`load_candidates()` / `train_artifact()`) - **코드 변경 불필요**.

## registry / telemetry

- `~/.omm/models.json`(설치 레지스트리) 엔트리에 `"provider"` 필드 추가
  (`_install_impl`의 `registry.upsert_entry(...)` 호출에 `provider=resolved.provider
  or "huggingface"` 추가). 기존 엔트리엔 이 필드가 없으므로, 읽는 쪽에서
  `entry.get("provider") or "huggingface"`로 기본값 처리한다 (마이그레이션
  스크립트 불필요).
- `_update_one()`(`cli.py:1508`)이 지금 `repo_id`가 있으면 무조건 HF 전용
  `remote_file_sha256`/`HF_DOWNLOAD`를 쓰는데, 이걸 `entry.get("provider")
  or "huggingface"`로 provider를 얻어 `hub.remote_file_sha256(provider,
  repo_id, filename)` / `hub.download_url(provider, repo_id, filename)`로
  바꾼다.
- telemetry 이벤트 payload(`_report_telemetry`가 만드는 `event` dict,
  `model_repo_id` 키 옆)에 `"model_provider"` 필드를 추가한다.
- `omm info` 출력에 HF가 아닌 provider만 "Repo" 행 옆에 표시 (예: `Repo:
  Qwen/Qwen2.5-0.5B-Instruct-GGUF [ModelScope]`). `omm list` 테이블은 지금
  `repo_id` 자체를 안 보여주는 구조라 provider 표시를 넣으려면 컬럼을 새로
  추가해야 하는데, 이건 요청받지 않은 UI 확장이라 이번 작업에서는 건드리지
  않는다 (YAGNI).

## 다운로드 Range 판별 수정

`src/omm/downloader.py`의 `_probe_range_support()`(75-92)와
`_download_range_worker()`(95-121)는 `resp.status_code == 206`을 Range
지원의 유일한 증거로 요구한다. ModelScope의 다운로드 엔드포인트는 Range를
실제로 존중하면서도 상태 코드는 200을 돌려주므로, 지금 코드로는 항상
싱글스레드 폴백을 탄다. 두 함수 모두 "200이면서 요청한 바이트 수만큼
정확히 온 경우"도 Range 지원으로 인정하도록 고친다 (상태 200을 무조건
신뢰하면 Range를 무시하고 파일 전체를 돌려주는 서버까지 오탐하므로,
`Content-Length`가 요청한 바이트 수와 정확히 일치하는지까지 확인한다).

## 테스트

- 각 provider 모듈의 4개 함수: 실제 API를 mock한 unit test (기존
  `hub.py`/`test_hub_remote_sha256.py` 테스트 패턴을 따라 `monkeypatch.setattr(
  requests, "get"/"post", ...)` + 로컬 fake-response 클래스).
- `resolve_model()`의 새 분기 (접두사 2종 + 무접두사 org/repo 단일/복수
  매치) 전부 unit test.
- `AmbiguousProviderError` install CLI 프롬프트 통합 테스트.
- `search.py`의 ModelScope 결과 병합 + dedupe + fake-provenance 필터 unit test.
- `contribute.py`의 구 포맷 history_refs 호환 처리 unit test (회귀 방지 -
  기존 HF 벤치마크 이력이 재등장하지 않는지).
- `downloader._probe_range_support`/`_download_range_worker`의 "200 +
  정확한 Content-Length" 인정 케이스 unit test (mock response로, 실제
  네트워크 없이).
- ModelScope 관련 실제 네트워크 검증은 이미 이 설계 단계에서 curl로 직접
  끝냈으므로 (검색/파일목록/다운로드/Range 전부 실측) 구현 단계에서 별도
  탐색이 필요 없다.
