# omm benchmark all 키워드 + 진행 피드백

## 배경

`omm benchmark all`을 실행하면 `all`이 리터럴 Ollama 태그로 취급되어
`/api/tags`에 없는 모델 이름으로 조회 -> `model_load_failed` ->
`transient_error`로 떨어진다 (`_model_metadata`, `quality.py:349`).
"전체 설치 모델 벤치마크" 키워드가 애초에 없었다 (버그 아니라 미구현
기능). 사용자 요청으로 이번에 추가한다.

동시에, `omm benchmark`는 여러 모델을 순차로 평가하는 동안
(모델당 quality pack 문항 반복 + speed run 반복) 콘솔에 아무 출력이 없어
스크립트처럼 멈춘 것처럼 보인다. 사용자가 직접 터미널에서 돌릴 때 최소한의
진행 피드백을 요청함.

## 스코프 결정 (사용자 확인)

- `all`의 범위: **Ollama에 설치된 태그 전부** (`/api/tags`), omm
  레지스트리 관리 여부와 무관. mmproj/clip 계열은 벤치마크 불가능한
  모델이므로 자동 제외 (에러로 노출하지 않음).
- `all`은 **단독 인자일 때만** 키워드로 해석한다. `omm benchmark all foo`
  처럼 다른 태그와 섞이면 모호하므로 에러로 반려한다.
- 진행 피드백은 **모델 단위 스피너**로 충분하다 ("Benchmarking
  llama3.2:3b (2/5)..." + 경과 시간). quality pack 문항 단위 세부 진행률은
  이번 스코프 밖 (quality.py 내부를 더 깊이 뜯어야 해서 범위 초과).

## 설계

### 1. `quality.py`: `list_benchmarkable_tags()`

```python
def list_benchmarkable_tags() -> list[str]:
    """All Ollama tags that could plausibly be benchmarked right now.

    Excludes mmproj/clip projector models (see _model_metadata) - they
    have no tokenizer of their own and would just fail every time.
    """
```

- `_request_json("GET", "/api/tags", timeout=10)` 재사용, `models` 리스트의
  `name`(문자열)만 뽑되 `details.family == "clip"`인 항목은 제외.
- 정렬은 API가 주는 순서 그대로 두되, 결정론적 테스트를 위해 이름 오름차순
  정렬한다.
- 빈 리스트 반환 가능 (설치된 모델이 하나도 없을 때) - 호출부에서 처리.

### 2. `cli.py`: `benchmark_cmd`의 `all` 확장

- 데몬 reachable 확인/기동 블록 **다음에** (daemon이 실제로 떠 있어야
  `/api/tags` 조회가 의미 있으므로):
  - `models`가 정확히 `["all"]`이면 `list_benchmarkable_tags()` 호출.
    - 결과가 비어 있으면 빨간 에러 "Ollama에 설치된 모델이 없습니다"
      출력 후 `typer.Exit(1)`.
    - 아니면 `models`를 이 리스트로 교체하고 "Expanding 'all' to N
      model(s): tag1, tag2, ..." 한 줄 안내.
  - `models`에 `"all"`이 포함되어 있는데 길이가 1이 아니면 (섞여 온 경우)
    빨간 에러로 반려: "`all` must be the only argument".
- `_resolve_benchmark_tag`는 그대로 둔다 (숫자 ref 처리는 `all`과
  무관하므로 순서상 `all` 판별을 그 뒤에 넣는다 - `"all"`은 `isdigit()`이
  아니므로 그대로 통과해 문제 없음).

### 3. 진행 피드백: `collect_evidence`에 콜백 추가

- 시그니처에 키워드 전용 인자 추가:
  `on_model_start: Callable[[str, int, int], None] | None = None`
  (tag, 1-based index, total).
- 태그 루프 최상단, `_evaluate_tag_once` 호출 직전에 존재하면 호출.
- confirm-performance-timeout 재시도(`_confirm_generation_timeout`)는
  같은 태그에 대한 재시도이므로 콜백을 다시 부르지 않는다 (모델 단위
  피드백이지 시도 단위가 아니므로).

### 4. `cli.py`: Rich Progress로 감싸기

- 기존 `_run_pipx_install_with_progress`와 동일한 스타일:
  `Progress(SpinnerColumn(), TextColumn(...), TimeElapsedColumn(),
  console=console)`.
- `collect_evidence` 호출을 이 `with Progress(...)` 블록 안으로 이동,
  `on_model_start` 콜백에서 `progress.update(task_id,
  description=f"[cyan]Benchmarking {tag} ({i}/{n})[/cyan]",
  completed=i - 1)`.
- 블록을 벗어나기 전 `progress.update(task_id, completed=total)`로 마무리
  (전량 완료 표시).
- `--json` 여부와 무관하게 항상 표시 (다운로드/reinstall 진행률과 동일한
  기존 관례 - Rich가 non-tty에서 알아서 축약 렌더링).

## 테스트

- `list_benchmarkable_tags`: clip 제외, 빈 리스트, 정상 목록 - 단위 테스트.
- `benchmark_cmd`의 `all` 확장: 단독 `all` -> 확장됨, `all`+다른 태그 ->
  에러, 빈 설치 목록 -> 에러. 기존 `tests/test_cli_benchmark.py` 패턴 따름.
- `collect_evidence(on_model_start=...)`: 콜백이 태그당 정확히 1회, 올바른
  (tag, index, total) 순서로 불리는지 - `tests/test_quality.py`에 추가.
