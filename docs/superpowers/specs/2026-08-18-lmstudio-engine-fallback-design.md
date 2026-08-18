# Ollama 없을 때 LM Studio로 자동 폴백 (`omm benchmark`/`omm contribute`)

Issue: #96

## Problem

`omm benchmark`/`omm contribute`는 사실상 Ollama 전용으로 동작한다. Ollama가
설치되어 있지 않으면 omm의 핵심 기능(품질/속도 벤치마크, 텔레메트리 기여)을
아예 쓸 수 없다. `omm install`의 링크/검증 경로는 이미 LM Studio를 어느 정도
지원하지만(`LMStudioAdapter`, `memory_guard`의 `LMStudioManagedRuntime`,
`linker.py`의 daemon lifecycle 헬퍼), 실제 벤치마크·품질평가·기여 루프
(`quality.py`, `benchmark.py`, `cli.py`의 `_run_contribution_loop`)는
Ollama의 REST API 응답 형태에 못박혀 있다.

## API 검증 (완료, 2026-08-18)

실제 LM Studio 0.4.21(brew cask, macOS)에 헤드리스로 설치해서 확인함:

- `/v1/chat/completions`(OpenAI 호환, 기존 `linker.py`의 `verify_lmstudio_load`가
  사용 중)와 `/api/v1/chat`(`LMStudioAdapter`가 사용 중) 둘 다 `stats` 필드가
  **빈 객체**. 토큰 타이밍 없음.
- `/api/v0/chat/completions`(구버전 native 엔드포인트, 현재 코드베이스
  어디서도 안 씀)는 다음을 정확히 준다:
  ```json
  "usage": {"completion_tokens": 54, "prompt_tokens": 40, "total_tokens": 94},
  "stats": {
    "tokens_per_second": 111.03,
    "time_to_first_token": 0.083,
    "generation_time": 0.5695,
    "stop_reason": "eosFound"
  }
  ```
  이건 Ollama의 `eval_count`/`eval_duration`과 완전히 동등하다(오히려
  tokens_per_second가 이미 계산돼서 옴). **benchmark/quality 경로는 이
  `/api/v0/chat/completions`를 새로 써야 한다.**
- 모델이 로드 안 된 상태에서 요청해도 JIT 로드됨 (`lms load` 선행 불필요) —
  `linker.py`의 기존 `_probe_lmstudio_generate` 주석과 동일한 동작 확인.

## Goals

1. `omm benchmark`/`omm contribute`가 Ollama 미설치 시 LM Studio로 자동
   전환되어 정확도(quality) + 속도(speed) 평가를 모두 수행한다.
2. LM Studio에도 Ollama와 동등한 daemon lifecycle(자동 시작/종료,
   contribute 세션 중 per-iteration health-check + 재시작 + abort cap)을
   이식한다.
3. 텔레메트리에 실제 사용된 엔진(`"ollama"` 또는 `"lmstudio"`)이 정확히
   기록되어 업로드된다.
4. 기존 Ollama 경로는 동작·출력이 전혀 바뀌지 않는다(회귀 없음).

## Non-goals (범위 밖, 이유 명시)

- **세션 중간 엔진 전환**: Ollama daemon이 재시작을 반복 실패해도 LM Studio로
  넘어가지 않는다 — 지금처럼 그 세션은 포기한다. 엔진 선택은 `omm
  benchmark`/`omm contribute` **시작 시점 1회**로 고정 (사용자 확정 결정,
  2026-08-18). 사유: 범위를 명확히 하고 대회 마감(8/27) 전 완료 가능성을
  높임.
- **`runtime_snapshot`의 GPU offload 측정**(`gpu_offload_percent` 등,
  Ollama `/api/ps`의 `size_vram` 기반) LM Studio 이식. LM Studio가 로드된
  인스턴스별 VRAM 사용량을 동등하게 노출하는지 미검증 상태이고, 이 필드는
  optional(`None` 허용)로 이미 설계돼 있어 없어도 텔레메트리가 깨지지
  않는다. LM Studio 경로는 이 필드를 항상 `None`으로 반환한다.
- MLX/ONNX 등 다른 런타임 (#26, #29에서 별도 다룸).
- LM Studio 쪽 MoE(is_moe/active_parameter_count_b) 정밀 감지. LM Studio의
  모델 목록 API가 Ollama의 verbose tensor inventory에 해당하는 정보를
  안 준다 — `is_moe: False`, `active_parameter_count_b` 필드 생략으로
  degrade.
- Firebase RTDB rules 콘솔 수정 자체(이 세션은 콘솔 접근 권한 없음) — 사전
  작업 체크리스트만 남기고, 실제 규칙 반영은 사용자가 수동으로 함.

## Design

### 1. `linker.py` — LM Studio daemon lifecycle을 공개 API로 노출

이미 있는 private 헬퍼(`_lms_cli_path`, `_lmstudio_server_status`,
`_start_lmstudio_server`, `_stop_lmstudio_server`, `_lmstudio_list_models`,
`_lmstudio_publisher_repo`, `_lmstudio_model_key`, `_lms_unload`)를
재사용해서, 파일 맨 아래에 얇은 public wrapper를 추가한다(로직 재구현 금지):

```python
def lmstudio_daemon_reachable() -> bool:
    """True iff `lms server status` reports running. Mirrors
    benchmark.ollama_daemon_reachable()'s role for the LM Studio path."""

def lmstudio_server_port() -> int | None:
    """Live port from `lms server status --json`, or None if not running.
    Never assume the default 1234 - the port is user-configurable."""

def start_lmstudio_daemon(timeout: float = 30.0) -> bool:
    """Best-effort `lms server start`; True iff running after the call
    (whether it was already running or freshly started)."""

def stop_lmstudio_daemon() -> None:
    """Best-effort `lms server stop`. Caller's responsibility to only call
    this when omm itself started the daemon (mirrors
    benchmark.stop_ollama_daemon's contract, but LM Studio's lifecycle is a
    named background service, not a Popen omm owns directly - no handle to
    pass back)."""

def resolve_lmstudio_model(repo_id: str | None, filename: str) -> dict | None:
    """Resolve a linked model to its LM Studio ls entry (modelKey +
    metadata), by the same path-matching _lmstudio_model_key already uses.
    Returns None if `lms` is missing, the server is down, or no match is
    found. Return shape:
      {"model_key": str, "architecture": str | None,
       "quantization_name": str | None, "quantization_bits": int | None,
       "params_string": str | None, "max_context_length": int | None,
       "trained_for_tool_use": bool}
    """

def unload_lmstudio_model(model_key: str) -> bool:
    """Best-effort `lms unload`. Wraps _lms_unload, returns whether the
    subprocess ran without raising (matches _lms_unload's soft-fail
    contract - always True unless the CLI itself is missing)."""
```

이 함수들 안에서 `_lms_cli_path()`가 `None`을 반환하면(=lms 자체가 없음)
전부 실패값(`False`/`None`)을 돌려준다 — 지금 존재하는 다른 lmstudio 헬퍼와
동일한 fail-soft 컨벤션.

### 2. `benchmark.py` — 헤더 주석만 갱신, 새 함수 없음

`omm benchmark`/`omm contribute`가 실제로 쓰는 저수준 daemon-lifecycle은
1)에서 `linker.py`에 다 생겼으므로, `benchmark.py`는 건드릴 필요 없다(파일
맨 위 "LM Studio benchmarking can be added later" 주석만 갱신). 실제 속도
측정은 `quality.py`의 `_speed_probe`/`_generate_with_runtime`을 통해서만
일어나므로 (`benchmark.benchmark_ollama_samples`는 `omm install`의 일회성
compat 체크 경로에서만 쓰이고 `omm contribute`/`omm benchmark`의 정식
경로는 이미 `quality.collect_evidence`를 쓴다 - `cli.py:2697` 확인 완료),
새 `benchmark_lmstudio_samples` 같은 병행 함수는 만들지 않는다.

### 3. `quality.py` — 엔진 매개변수화 (핵심 작업)

**설계 원칙**: LM Studio의 `/api/v0/chat/completions` 응답을 transport
경계에서 Ollama 응답 shape로 정규화한다. 그러면 정확도 채점(`response
["response"]`를 읽는 코드)과 속도 계산(`_tokens_per_second`가 `eval_count`/
`eval_duration`을 읽는 코드)은 **한 줄도 안 바뀐다**. 분기는
`_generate_with_runtime` 한 곳에만 존재한다.

```python
def _generate_with_runtime(
    tag: str, prompt: str, generation: dict, num_predict: int | None,
    runtime_options: dict | None, supports_thinking: bool = True,
    *, engine: str = "ollama", lmstudio_port: int | None = None,
) -> dict:
    if engine == "lmstudio":
        return _generate_lmstudio(tag, prompt, generation, num_predict, lmstudio_port)
    ...  # existing Ollama path, unchanged


def _generate_lmstudio(
    model_key: str, prompt: str, generation: dict, num_predict: int | None,
    port: int | None,
) -> dict:
    """POST /api/v0/chat/completions, normalize to Ollama's response shape:
    {"response": <text>, "eval_count": <completion_tokens>,
     "eval_duration": <int(generation_time * 1e9)>}.
    Raises QualityEvaluationError on connection/timeout/load failure, same
    failure_reason taxonomy as _request_json (reuse LoopbackJsonClient for
    the same connection_error/timeout classification - see engines/base.py,
    already used by both quality.py's Ollama path and LMStudioAdapter)."""
```

- `port`는 매 호출 시 `linker.lmstudio_server_port()`로 다시 조회하지 않고,
  `evaluate_model`/`collect_evidence` 진입 시 한 번 조회해서 인자로 흘려
  보낸다(반복 subprocess 호출 방지). daemon이 죽어서 port 조회가 실패하면
  기존 daemon-health-check 루프(아래 `collect_evidence` 변경 참고)가 잡는다.
- `num_predict`는 LM Studio 요청의 `max_tokens`에 매핑. `generation`
  dict의 `temperature` 등이 있으면 그대로 전달, 없으면 기존 Ollama 쪽
  기본값과 동일한 값을 사용(정확한 매핑은 구현 시 `generation` dict의 현재
  키를 보고 결정 - Ollama의 `options.temperature` 등과 1:1 대응되는 필드만
  옮기고, Ollama 전용 옵션(`num_thread`, `num_batch` 등)은 LM Studio
  요청에 넣지 않는다).

**`_model_metadata(tag, *, engine="ollama", lmstudio_model=None)`**:
- `engine="lmstudio"`일 때는 `lmstudio_model`(2)의 `resolve_lmstudio_model`
  반환 dict)을 그대로 Ollama metadata dict 모양으로 매핑:
  `family`←`architecture`, `quantization_level`←`quantization_name`,
  `parameter_size`←`params_string`, `capabilities`←
  `["tools"] if trained_for_tool_use else []`, `is_moe: False`,
  `digest: None`(LM Studio 모델엔 Ollama 같은 digest 개념 없음),
  `size_bytes: None` 허용(이미 optional).
- mmproj 거부 로직(Ollama의 `details.family == "clip"` 체크)은 LM Studio
  경로에도 있어야 하는지 확인 필요 - `resolve_lmstudio_model`이 embedding
  타입 모델을 애초에 안 돌려주도록(2)에서 `type != "llm"`이면 `None`
  반환하는 걸로 처리한다(이미 `LMStudioAdapter.list_models`가 같은 필터
  씀, 참고).

**`_speed_probe`/`evaluate_model`**: `engine`/`lmstudio_port`를 인자로
받아서 `_generate_with_runtime`에 그대로 전달하는 것 외 변경 없음.
`evaluate_model`의 반환 dict에 `"engine": engine` 필드 추가 (4)에서
텔레메트리가 이걸 읽음).

**`collect_evidence`**:
```python
def collect_evidence(
    tags: list[str],
    hardware: HardwareInfo,
    pack_path: Path | None = None,
    speed_runs: int = 3,
    *,
    engine: str = "ollama",
    lmstudio_models: dict[str, dict] | None = None,  # tag(=model_key) -> resolve_lmstudio_model() 결과, engine="lmstudio"일 때 필수
    confirm_performance_timeout: bool = False,
    on_model_start=None,
    on_daemon_event=None,
) -> dict:
```
- `tags`의 의미는 엔진에 따라 다르다: Ollama는 지금처럼 ollama tag,
  LM Studio는 **modelKey 문자열**(호출자가 미리 `resolve_lmstudio_model`로
  구해서 넘김). 이렇게 하면 `tags: list[str]` 시그니처 자체는 안 바뀌고,
  `model["tag"]`로 매칭하는 `cli.py`의 기존 로직(`_report_telemetry` 호출부
  등)도 그대로 재사용 가능.
- daemon-health 루프(`if ollama_version() is None: ...`)를 엔진별로 분기:
  `lmstudio`는 `linker.lmstudio_daemon_reachable()` 체크 + 재시작은
  `linker.start_lmstudio_daemon()`. 재시작 로직(연속 실패 카운트,
  `_MAX_CONSECUTIVE_DAEMON_FAILURES`, backoff)은 기존 상수/구조 그대로
  재사용.
- `environment.engine`을 인자로 받은 `engine` 값으로 (하드코딩된
  `"ollama"` 제거). `environment.engine_version`은 lmstudio일 때
  `LMStudioAdapter(base_url=f"http://127.0.0.1:{port}").health().version`
  사용(이미 있는 어댑터 재사용, x-lm-studio-version 헤더 파싱 로직도 이미
  있음).

**`unload_model(tag, *, engine="ollama")`**: lmstudio면
`linker.unload_lmstudio_model(tag)` 호출.

### 4. `cli.py` — 엔진 선택 + `_run_contribution_loop`/`omm benchmark` 일반화

**엔진 선택 (신규 헬퍼, 시작 시점 1회)**:
```python
def _select_benchmark_engine() -> str | None:
    """"ollama" if Ollama's daemon can be reached or started, else
    "lmstudio" if LM Studio's can, else None (caller must error out with
    an actionable message - neither engine usable)."""
```
이 함수는 daemon을 실제로 켜지는 않고 "켤 수 있는지"만 판단
(`benchmark.find_ollama_executable()`/`linker._lms_cli_path()`가 있는지,
또는 이미 reachable인지) - 실제 시작은 기존 흐름(각 명령의 `finally`에서
정리)과 동일한 지점에서 일어나야 하므로, 호출자가 뒤이어 각자
`benchmark.start_ollama_daemon()` 또는 `linker.start_lmstudio_daemon()`을
부른다.

**`_run_contribution_loop`** (`cli.py:5207`): 지금 `benchmark.
ollama_daemon_reachable()`/`benchmark.start_ollama_daemon()`에 못박힌
daemon-health-check-and-restart 블록(라인 5221-5241, 5323-5343 두 곳)을
`engine` 매개변수를 받아 분기하도록 일반화. `link_only_ollama=True`
하드코딩(라인 5308, 5352)은 `link_only_engine=engine` 파라미터로 교체 -
`_install_impl`/`_link_model`/`linker.link_model` 체인(라인 1800대,
1992대, 2280대)의 `only_ollama: bool` 파라미터를 `only_engine: str | None`
로 넓힌다(`None`이면 기존처럼 모든 엔진에 링크, 문자열이면 그 엔진에만 -
기존 `only_ollama=True`는 `only_engine="ollama"`와 동치이므로 기존 호출부
전부 마이그레이션).

**`omm benchmark`/`omm contribute` 명령 진입부**: `_select_benchmark_engine()`
결과가 `None`이면 "Ollama도 LM Studio도 쓸 수 없습니다" 에러로 즉시 종료
(둘 다 없는 경우 - 지금 Ollama 없으면 나던 에러와 동급 메시지, 엔진 이름만
일반화).

**`omm benchmark`의 텔레메트리 하드코딩** (`cli.py:4656`대 `_report_telemetry`
내부 `event = {..., "engine": "ollama", ...}`, `_report_failure_telemetry`의
동급 라인): 실제 사용된 엔진 값을 받아서 채우도록 일반화. 호출부
(`cli.py:4560`, `4583`)는 `report["environment"]["engine"]`(3)에서 이미
정확한 값이 들어옴)을 그대로 넘기면 됨.

### 5. `memory_guard.py` — LM Studio 쪽 per-iteration parity 확인

[[project_omm_lmstudio_guard_gap_and_thread_leak]]에서 이미 한 번 "Ollama
전용이던 memory-guard 갭"을 고친 적 있음. 이번엔 `_run_contribution_loop`가
LM Studio 엔진으로 실행될 때도 (a) 다운로드 전 메모리 사전 체크
(`_contribute_candidate_memory_plan`), (b) 로드 전 guard
(`_guard_lmstudio_load` - 이미 존재), (c) daemon 재시작 시나리오에서
guard 상태가 꼬이지 않는지 코드 리딩으로 재확인. 이미 존재하는 로직이면
새로 안 만든다 - 이 태스크는 "확인 및 갭 발견 시만 수정"이다.

### 6. 텔레메트리 스키마 / Firebase rules 사전 작업

**중요, 코드 배포 전 반드시 확인**: Firebase RTDB `/telemetry` 노드의
validation rules가 `engine` 필드를 enum으로 제한하고 있다면(v8 규칙에
`"engine": {".validate": "newData.val() === 'ollama'"}` 같은 형태라면)
`engine: "lmstudio"` 업로드가 조용히 401로 막힌다 - 정확히
[[project_omm_contribute_telemetry_model_provider_rules_bug]]와 같은
사고 패턴. 코드를 머지하기 전에 (또는 직후 바로) Firebase 콘솔에서
현재 규칙의 `engine` 필드 제약을 확인하고, 필요하면 `'ollama'` 외
`'lmstudio'`도 허용하도록 고쳐야 한다. **이 세션은 콘솔 접근 권한이
없으므로 이 확인/수정은 사용자가 직접 한다** - 계획의 마지막 태스크로
체크리스트만 남긴다.

## Global Constraints (모든 태스크 공통)

- 기존 Ollama 경로는 동작·출력이 **전혀 바뀌지 않는다**. 모든 새 매개변수는
  `engine: str = "ollama"` 기본값으로 기존 호출부 무변경 보장.
- `linker.py`의 기존 private 헬퍼(`_lms_*`)는 그대로 두고 재사용만 한다
  (로직 복제 금지, 시그니처 변경 금지 - 다른 코드가 이미 그 시그니처에
  의존).
- 새 LM Studio 코드 경로도 기존 Ollama 코드처럼 fail-soft: daemon 없음/
  응답 이상은 예외가 아니라 `QualityEvaluationError`(적절한
  `failure_reason`)로 - 이미 있는 taxonomy 재사용, 새 reason 문자열은
  꼭 필요한 경우만 추가.
- 개인정보(파일 경로, 모델 파일명 원문 등)는 지금처럼 텔레메트리에 절대
  포함하지 않는다 - `raw_hardware_names_stored: False` 같은 기존 관례 유지.
