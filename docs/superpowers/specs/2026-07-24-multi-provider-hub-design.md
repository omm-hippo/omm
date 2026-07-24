# HuggingFace 외 ModelScope / CivitAI 저장소 직접 연동

## 배경

지금 omm은 모델 소스로 HuggingFace 하나만 안다. `hub.py`의
`resolve_model()`, `search.py`의 `search_huggingface()`, `contribute.py`의
`ref()`, `scripts/fetch_hf_candidates.py`가 전부 `repo_id`를 HF 전용
`org/repo` 문자열로 가정하고, 다운로드 URL도 `huggingface.co/{repo}/resolve/...`
하드코딩이다. 사용자 요청: `omm search`/`omm install`/`omm recommend`/
`omm contribute`/예측 모델(추천 엔진) 학습 파이프라인 전부에서 ModelScope와
CivitAI를 API로 직접 조회 가능하게 만든다.

**스코프 결정** (사용자 확인):
- CivitAI는 Stable Diffusion 체크포인트/LoRA 저장소라 GGUF LLM이 거의 없다.
  검색 결과에 GGUF 확장자 파일이 있는 항목만 필터링해서 보여준다 - 결과가
  거의 항상 비어 있어도 정상이다 (에러 아님).
- `omm install`에서 provider 접두사(`hf:`/`ms:`/`civitai:`)는 선택 사항.
  생략하면 관련 provider들을 조회해서, 매치가 하나면 바로 설치하고 여럿이면
  사용자에게 고르게 한다.
- 멀티스레드 다운로드는 별도 구현이 필요 없다: `downloader.download_file()`은
  이미 순수 URL 문자열만 받아서 HTTP Range 지원 여부를 프로브하고 자동
  병렬화한다 (`downloader.py:75-92, 208-236`). provider 모듈이
  `download_url(repo_id, filename) -> str`만 내놓으면 기존 파이프라인을
  그대로 탄다. 단, ModelScope/CivitAI의 다운로드 엔드포인트가 실제로 Range를
  지원하는지는 구현 중 실제 요청으로 검증한다 (추측하지 않는다).

## 확인된 API 스펙

**ModelScope** (base `https://modelscope.cn`, 공개 모델 조회에 토큰 불필요 -
`modelscope_hub` 공식 SDK 소스로 직접 확인, `require_token=False`):
- 검색: `GET /openapi/v1/models?search=&owner=&sort=&page_number=&page_size=`
- 모델 메타: `GET /api/v1/models/{repo_id}` (`repo_id` = `owner/name`, HF와 동일 형식)
- 파일 목록: `GET /api/v1/models/{repo_id}/repo/files?Revision=master&Recursive=True`
- 다운로드/HEAD: `GET /api/v1/models/{repo_id}/repo?Revision=master&FilePath=<path>`
  (URL 조립만으로 만들어짐, 별도 요청 불필요)

파일 목록 응답의 정확한 필드 이름(`Path`/`Name`/`Size` 등 케이싱)은 구현
착수 시 실제 호출로 확인한다 - 문서화가 부실해 소스코드에서도 100% 확정하지
못했다.

**CivitAI** (base `https://civitai.com/api/v1`, 공개 조회 토큰 불필요):
- 검색: `GET /models?query=&limit=&page=` (또는 cursor 페이징)
- 응답의 `modelVersions[].files[]`에 `name`, `downloadUrl`, `sizeKB`,
  `hashes.SHA256` 존재. `downloadUrl`은 바로 쓸 수 있는 완성 URL.
- 모델은 정수 id로 식별 (`org/repo` 형식 아님) - HF/ModelScope와 네임스페이스가
  절대 충돌하지 않는다.
- GGUF 판별은 `metadata.format` 필드를 믿지 않고 기존 코드 관례대로 파일명
  `.gguf` 확장자로 한다.

## 아키텍처

```
src/omm/providers/
  __init__.py      # PROVIDERS: dict[str, ProviderModule], get_provider(name)
  base.py          # ProviderError, RepoFile 등 공통 타입/예외
  huggingface.py   # hub.py의 _fetch_repo_gguf_info/remote_file_size/remote_file_sha256 이동
  modelscope.py    # 동일 인터페이스, ModelScope API로 구현
  civitai.py       # 동일 인터페이스, model id 기반이라 fetch_repo_files의 "repo_id"가 숫자 id
```

각 provider 모듈은 동일한 4개 함수를 구현한다 (HF 기준 시그니처를 그대로
따름, `hub.py`가 이미 이 모양으로 되어 있어 추상화 비용이 낮다):

```python
def fetch_repo_files(repo_id: str) -> tuple[list[str], float | None]:
    """(.gguf 파일명 목록, repo-level 파라미터 수 billions 추정치)"""

def download_url(repo_id: str, filename: str) -> str: ...

def remote_file_size(repo_id: str, filename: str) -> int | None: ...

def remote_file_sha256(repo_id: str, filename: str) -> str | None: ...
```

`hub.py`는 이 함수들을 provider별로 라우팅하는 얇은 레이어로 남는다.
`ResolvedModel`에 `provider: str` 필드를 추가한다 (`"huggingface"` |
`"modelscope"` | `"civitai"`; 직접 URL 설치는 호스트명으로 자동 판별을
시도하고, 모르는 호스트면 지금처럼 값 없음으로 둔다 - 필드 자체는
`str | None`이 아니라 항상 채워지도록 `"unknown"`이 아니라 기존 관례를 따라
`provider: str | None`로 선언한다).

## install ref 문법과 provider 판별

`resolve_model(model_name)`의 분기 순서 (기존 분기 뒤에 새 분기를 끼워 넣는
형태로, 기존 동작은 전부 보존):

1. `CURATED_INDEX` 히트 → 지금과 동일 (HF 고정).
2. `http(s)://` 시작 → 지금과 동일하게 직접 URL 설치. 추가로 호스트가
   `huggingface.co`/`modelscope.cn`/`civitai.com`이면 `provider`를 채운다.
3. 명시적 접두사 (`hf:`, `ms:` 또는 `modelscope:`, `civitai:`) → 접두사
   제거하고 해당 provider로 위임. 나머지 파싱 규칙(`:filename` 옵션 등)은
   HF 기존 로직과 동일하게 provider별로 반복.
4. 접두사 없고 `/` 포함 (`org/repo[:file]` 형태) → HF와 ModelScope 양쪽에
   `fetch_repo_files`를 시도(병렬 아니어도 됨, 순차 호출로 충분 - 404는
   빠르다). 매치가 하나뿐이면 그 provider로 진행. 둘 다 매치하면 새 예외
   `AmbiguousProviderError(repo_id, providers=["huggingface","modelscope"])`를
   던져서 CLI가 provider 선택 프롬프트를 띄운다 (기존
   `AmbiguousModelError`의 파일 선택 프롬프트와 같은 UX 패턴). 둘 다 없으면
   기존 `ModelResolutionError`.
5. 접두사 없고 순수 숫자 (`str.isdigit()`) → CivitAI model id로 간주.
   해당 모델의 모든 버전에서 `.gguf` 파일을 모아, 하나면 바로 진행, 여럿이면
   기존 `AmbiguousModelError`와 같은 패턴으로 파일명 선택.
6. 그 외 → 기존과 동일한 "unknown model" 에러, 메시지에 큐레이션 이름 +
   3-provider 접두사 문법 안내를 추가.

`AmbiguousModelError`/`AmbiguousProviderError`는 `hub.py`에 정의하고
`cli.py`의 install 커맨드가 이미 `AmbiguousModelError`를 잡아 인터랙티브
선택 프롬프트를 띄우는 지점에 `AmbiguousProviderError` 처리 분기를 추가한다.

## `omm search` 통합

`search.py`에 `search_modelscope(query)`, `search_civitai(query)`를
`search_huggingface()`와 같은 반환 모양(`{"name", "repo_id", "filename",
"description", "provider"}` dict 리스트)으로 추가한다. 둘 다
`_claims_fake_provenance()` 필터를 그대로 적용한다 (레포명 스팸 패턴은
provider 무관). CivitAI는 검색 응답에서 `.gguf` 파일이 하나도 없는 모델은
결과에서 제외한다.

`cli.py`의 `search()` 커맨드가 세 함수의 결과를 `local_candidate_pool()`과
합쳐서 기존 fuzzy-match/family-grouping 로직에 그대로 태운다 (이 로직들은
이미 provider-agnostic한 dict 필드 기반이라 수정 불필요).

`catalog.install_ref(candidate)`가 provider를 보고 접두사를 붙여 반환하도록
바꾼다: 큐레이션 이름은 지금처럼 접두사 없음, HF는 지금처럼 `org/repo`
(하위 호환 - 접두사 안 붙임), ModelScope는 `ms:org/repo`, CivitAI는
`civitai:<modelId>:<filename>`. 검색 결과를 그대로 복붙해서 install할 수
있어야 하므로 이 부분은 정확해야 한다.

## `omm recommend` / `omm contribute` / 예측 모델 학습 파이프라인

- `scripts/fetch_hf_candidates.py` → `scripts/fetch_candidates.py`로 이름
  변경. 기존 `fetch_trending_candidates()`(HF)는 그대로 두고
  `fetch_modelscope_candidates()`, `fetch_civitai_candidates()`를 추가한다.
  각 candidate dict에 `"provider"` 키를 채워 넣는다. `main()`의 dedupe 키를
  `repo_id` 단독에서 `(provider, repo_id)` 튜플로 바꾼다 (다른 provider가
  우연히 같은 `repo_id` 문자열을 쓸 경우의 충돌 방지 - CivitAI는 숫자 id라
  실질적으로 걱정 없지만 ModelScope는 HF와 같은 `org/repo` 네임스페이스라
  실제로 겹칠 수 있다).
- `predictor.py`, `featurize.py`, `mltree.py`는 candidate dict를 통째로
  받아 쓰는 구조라 `provider` 키가 추가돼도 깨지지 않는다 - 코드 변경 없음
  (`featurize.py`의 `repo_id.rsplit("/", 1)[-1]`도 ModelScope 형식에
  그대로 통한다; CivitAI는 `repo_id`가 숫자라 이 부분 결과가 의미 없는
  문자열이 되지만 기존에도 "source" 피처 중 하나일 뿐이라 랭킹 로직을 깨진
  않는다).
- `contribute.py`의 `ref(candidate)`를 `f"{candidate['provider']}:{...}"`
  형태로 바꾼다. 기존에 쌓인 history_refs(구 포맷, provider 접두사 없음)를
  HF로 간주해서 매칭하는 호환 처리를 추가한다 - 안 그러면 이미 벤치마크를
  마친 HF 모델들이 `omm contribute` 큐에 "새 모델"로 재등장하게 된다.
- `train_model.py`가 `published/candidates.json`을 어떻게 소비하는지는
  구현 단계에서 먼저 읽어보고, `provider` 키를 무시하고 통과시키는지
  확인한다 (추측하지 않는다 - 필요하면 여기서 최소 수정).

## registry / telemetry

- `~/.omm/models.json`(설치 레지스트리) 엔트리에 `"provider"` 필드 추가.
  기존 엔트리엔 이 필드가 없으므로, 읽는 쪽에서 `entry.get("provider") or
  "huggingface"`로 기본값 처리한다 (마이그레이션 스크립트 불필요).
- telemetry 이벤트 payload(`cli.py`에서 `"repo_id": ...`를 채우는 자리들)에
  `"provider"` 필드를 나란히 추가한다.
- `omm list` / `omm info` 출력에 HF가 아닌 provider만 뱃지로 표시 (예:
  `[ModelScope]`, `[CivitAI]`) - 기존 HF 전용 출력 포맷은 그대로 둔다.

## 테스트

- 각 provider 모듈의 4개 함수: 실제 API를 mock한 unit test (기존
  `hub.py`/`_fetch_repo_gguf_info` 테스트 패턴을 따라감).
- `resolve_model()`의 새 분기 5가지 (접두사 3종 + 무접두사 org/repo 단일/
  복수 매치 + 무접두사 숫자) 전부 unit test.
- `AmbiguousProviderError` CLI 프롬프트 통합 테스트.
- `search.py`의 provider별 결과 병합 + dedupe + fake-provenance 필터 unit test.
- `contribute.py`의 구 포맷 history_refs 호환 처리 unit test (회귀 방지 -
  기존 HF 벤치마크 이력이 재등장하지 않는지).
- 실제 네트워크로 ModelScope/CivitAI 다운로드 엔드포인트의 Range 지원
  여부와 파일 목록 응답 필드명을 확인하는 1회성 수동 검증 (자동화 테스트에
  넣지 않음 - 외부 API 응답 형태 확정용).
