# Plan: Ollama 없을 때 LM Studio로 자동 폴백

Spec: `docs/superpowers/specs/2026-08-18-lmstudio-engine-fallback-design.md`
(반드시 먼저 읽을 것 - 이 플랜의 모든 함수 시그니처/설계 근거가 거기 있음)
Issue: #96

## Global Constraints

- 기존 Ollama 경로는 동작·출력이 전혀 바뀌지 않는다. 모든 새 매개변수는
  `engine: str = "ollama"` 기본값.
- `linker.py`의 기존 private 헬퍼(`_lms_*`, 밑줄 접두)는 그대로 두고
  재사용만 한다 - 시그니처 변경 금지, 로직 복제 금지.
- 새 코드 경로도 기존처럼 fail-soft (daemon 없음/이상 응답은 예외가 아니라
  `QualityEvaluationError` + 적절한 `failure_reason`).
- 텔레메트리에 파일 경로/모델 파일명 원문 등 개인정보 포함 금지 - 기존
  관례(`raw_hardware_names_stored: False` 등) 유지.
- 각 태스크는 관련 기존 테스트 스위트(`pytest tests/ -x -q`, 관련 파일만
  좁혀서 먼저 확인 가능)를 그린으로 유지하고, 새 동작에 대한 테스트를
  추가한다. 참고할 기존 테스트: `tests/test_quality.py`,
  `tests/test_cli_benchmark.py`, `tests/test_contribute_loop.py`,
  `tests/test_linker_new_engines.py`(LM Studio 관련 기존 테스트 패턴).
- 실제 LM Studio 데몬이 필요한 검증은 이 머신에 이미 brew cask로 설치돼
  있고(`~/.cache/lm-studio`), `lms` CLI는 `~/.cache/lm-studio/bin/lms`에
  있다(PATH엔 없음, 절대경로로 호출). 헤드리스로만 쓴다 - GUI 앱을 열지
  않는다. 테스트 모델로 `Qwen/Qwen2.5-0.5B-Instruct-GGUF`(680MB, Q8_0)가
  이미 다운로드돼 있다(`lms ls --json`으로 확인 가능).

## Task 1: `linker.py`에 LM Studio daemon-lifecycle 공개 API 추가

스펙 섹션 1, 2 구현.

`src/omm/linker.py` 파일 끝에 다음 public 함수 6개 추가 (스펙에 정확한
시그니처/docstring 있음, 그대로 옮길 것):

- `lmstudio_daemon_reachable() -> bool`
- `lmstudio_server_port() -> int | None`
- `start_lmstudio_daemon(timeout: float = 30.0) -> bool`
- `stop_lmstudio_daemon() -> None`
- `resolve_lmstudio_model(repo_id: str | None, filename: str) -> dict | None`
- `unload_lmstudio_model(model_key: str) -> bool`

전부 이미 파일에 있는 private 헬퍼(`_lms_cli_path`,
`_lmstudio_server_status`, `_start_lmstudio_server`,
`_stop_lmstudio_server`, `_lmstudio_list_models`,
`_lmstudio_publisher_repo`, `_lmstudio_model_key`, `_lms_unload`)를
호출만 하는 얇은 wrapper다 - 새 subprocess 로직을 만들지 않는다.
`_lms_cli_path()`가 `None`이면 (`lms` 없음) 전부 실패값(`False`/`None`)
반환.

`resolve_lmstudio_model`은 `_lmstudio_model_key`처럼 path-matching으로
entry를 찾되, key 문자열만이 아니라 스펙에 명시된 dict 전체를 돌려줘야
한다 (`_lmstudio_list_models`가 돌려주는 raw entry의 `architecture`,
`quantization`, `paramsString`, `maxContextLength`, `trainedForToolUse`
필드에서 매핑). **embedding 타입 모델(`entry.get("type") != "llm"`)은
매치 대상에서 제외** (스펙 섹션 3의 mmproj/embedding 거부 요구사항).

`src/omm/benchmark.py` 파일 맨 위 docstring(1-4번 줄, "LM Studio
benchmarking can be added later" 부분)을 갱신 - 실제 속도 측정은
`quality.py`(Task 3에서 처리)를 통해 일어나고 이 파일은 daemon lifecycle
헬퍼 목적이 아님을 명확히 하는 한 문장으로. **이 파일의 나머지 코드는
건드리지 않는다** (스펙 섹션 2: benchmark.py에 새 함수 안 만듦).

**테스트**: `tests/test_linker_new_engines.py` 패턴을 참고해서 새
6개 함수에 대한 유닛 테스트 추가 (subprocess mock 기반 - 기존 `_lms_*`
헬퍼 테스트가 이미 비슷한 mock 패턴을 쓰고 있을 것, 찾아서 따라간다).
그 다음, 이 머신에 실제 설치된 LM Studio로 최소 1개 함수(예:
`lmstudio_daemon_reachable`)를 실제로 헤드리스 서버 켜고/끄면서 수동
확인(pytest 테스트가 아니라 스크립트로) - 결과를 보고서에 적을 것.

## Task 2: `quality.py` 엔진 매개변수화

스펙 섹션 3 전체 구현. 이 태스크가 가장 크다 - 스펙을 정확히 따를 것,
특히 "transport 경계에서 정규화" 원칙(`_generate_lmstudio`가 Ollama
응답 shape `{"response":..., "eval_count":..., "eval_duration":...}`로
정규화 - 그 아래(정확도 채점, `_tokens_per_second`)는 무변경).

구현 대상 (스펙 섹션 3에 정확한 시그니처):
- `_generate_with_runtime(...)`에 `engine`/`lmstudio_port` 키워드 인자
  추가, `engine=="lmstudio"`면 신규 `_generate_lmstudio(...)`로 분기
- `_generate_lmstudio(model_key, prompt, generation, num_predict, port)`:
  `POST http://127.0.0.1:{port}/api/v0/chat/completions`, 응답을
  Ollama shape로 정규화, 연결/타임아웃 실패는 `LoopbackJsonClient`
  재사용해서 기존 `failure_reason` taxonomy로 매핑 (Task 1에서 만든
  `linker.lmstudio_server_port()`는 이 함수의 호출자가 미리 조회해서
  `port` 인자로 넘긴다 - 이 함수 자체는 port를 재조회하지 않는다)
- `_model_metadata(tag, *, engine="ollama", lmstudio_model=None)`:
  `lmstudio_model`은 Task 1의 `resolve_lmstudio_model()` 반환 dict -
  스펙의 필드 매핑표대로 Ollama metadata dict 모양으로 변환
- `_speed_probe`/`evaluate_model`: `engine`/`lmstudio_port` 관통,
  `evaluate_model`의 반환 dict에 `"engine": engine` 필드 추가
- `collect_evidence(...)`: `engine`/`lmstudio_models` 키워드 인자 추가
  (스펙의 정확한 시그니처), daemon-health 루프를 엔진별로 분기(Ollama
  분기는 기존 코드 그대로, LM Studio 분기는 `linker.
  lmstudio_daemon_reachable()`/`linker.start_lmstudio_daemon()` 사용,
  기존 `_MAX_CONSECUTIVE_DAEMON_FAILURES`/backoff 상수·구조 재사용),
  `environment.engine`을 인자로 받은 값으로(하드코딩 `"ollama"` 제거),
  `environment.engine_version`은 lmstudio일 때
  `engines.lmstudio.LMStudioAdapter(base_url=f"http://127.0.0.1:{port}").health().version`
- `unload_model(tag, *, engine="ollama")`: lmstudio면
  `linker.unload_lmstudio_model(tag)`

**테스트**: `tests/test_quality.py`의 기존 Ollama 테스트 구조를 그대로
LM Studio용으로 미러링 - 최소한 `_generate_lmstudio`의 정규화 로직(가짜
`/api/v0/chat/completions` 응답 → 정확한 `eval_count`/`eval_duration`
변환), `_model_metadata`의 LM Studio 매핑, `collect_evidence`의 엔진
분기(daemon 죽음→재시작 시나리오 포함, 기존 Ollama 쪽 daemon-recovery
테스트와 대칭). 기존 Ollama 테스트는 전부 그린 유지해야 한다(회귀 없음
확인 - `pytest tests/test_quality.py -q`).

이후, 이 머신의 실제 LM Studio로 `evaluate_model("qwen2.5-0.5b-instruct",
pack, engine="lmstudio", ...)` 같은 실호출을 스크립트로 1회 실행해서
진짜 정확도+속도 결과가 나오는지 확인 (pack은 `omm`의 기존 quality pack
로더 사용). 결과를 보고서에 적을 것.

## Task 3: `cli.py` 엔진 선택 + contribute/benchmark 일반화

스펙 섹션 4 전체 구현. Task 2가 끝난 뒤 착수 (새 `quality.py` 시그니처에
의존).

- 신규 `_select_benchmark_engine() -> str | None` 헬퍼 (스펙에 정확한
  동작 정의 - daemon을 켜지는 않고 켤 수 있는지만 판단)
- `_run_contribution_loop`(`cli.py:5207` 부근)의 두 daemon-health-check
  블록(라인 5221-5241, 5323-5343 - **정확한 라인 번호는 Task 1/2의 diff로
  약간 밀렸을 수 있으니 `benchmark.ollama_daemon_reachable()`/
  `benchmark.start_ollama_daemon()` 호출부를 grep으로 다시 찾을 것**)을
  `engine` 매개변수로 분기
- `link_only_ollama=True` 하드코딩(같은 함수 내 두 `_install_impl` 호출부)을
  `link_only_engine=engine`으로 교체. 이걸 위해 `_install_impl` →
  `_link_model` → `linker`의 링크 체인에 있는 `only_ollama: bool`
  파라미터(대략 `cli.py:1800`, `1992`, `2280` 부근 - grep으로 정확한
  현재 위치 확인)를 `only_engine: str | None`로 넓힌다. **기존
  `only_ollama=True` 호출부는 전부 `only_engine="ollama"`로, `False`는
  `only_engine=None`으로 마이그레이션** - 동작 동일해야 함
- `omm benchmark`/`omm contribute` 명령 진입부에 `_select_benchmark_engine()`
  적용, `None`이면 "Ollama도 LM Studio도 쓸 수 없습니다" 에러(기존
  Ollama-없음 에러 메시지를 엔진 이름만 일반화)
- `_report_telemetry`/`_report_failure_telemetry` 내부의 `"engine":
  "ollama"` 하드코딩(grep으로 정확한 현재 라인 확인 - 스펙 작성 시점
  기준 `cli.py:4656`대와 `_report_failure_telemetry` 내부 동급 라인)을
  실제 엔진 값으로. 호출부(`cli.py:4560`, `4583` 부근)는
  `report["environment"]["engine"]`(Task 2에서 이미 정확한 값이 들어옴)을
  그대로 넘기도록 수정

**테스트**: `tests/test_cli_benchmark.py`, `tests/test_contribute_loop.py`,
`tests/test_cli_contribute.py`의 기존 Ollama 테스트가 전부 그린 유지되는지
확인(회귀 없음이 이 태스크에서 제일 중요). `_select_benchmark_engine`,
`only_engine` 마이그레이션, daemon-health 분기에 대한 새 테스트 추가 -
기존 Ollama daemon-recovery 테스트(`test_contribute_loop.py`에 있을
가능성 높음, grep해서 찾을 것)를 LM Studio 버전으로 미러링.

## Task 4: `memory_guard.py` LM Studio 경로 parity 확인

스펙 섹션 5. **먼저 코드를 읽고 실제 갭이 있을 때만 수정** - 이미 있는
것을 새로 만들지 않는다.

확인할 것:
1. `_contribute_candidate_memory_plan`(다운로드 전 사전 체크)이 engine이
   `lmstudio`로 선택된 세션에서도 정확한 하드웨어 가용 메모리를 계산하는가
   (Ollama 잔여 로드 상태가 아니라 LM Studio 잔여 로드 상태를 봐야 함)
2. `_guard_lmstudio_load`(이미 존재)가 Task 3에서 생긴 새 호출 경로에서도
   Ollama 경로와 동일한 시점에 호출되는가
3. daemon 재시작 시나리오(Task 3의 `_run_contribution_loop` LM Studio
   분기)에서 guard 상태(예: "이 모델은 omm이 로드했다"는 소유권 추적)가
   재시작 후에도 꼬이지 않는가 - Ollama 쪽에 이미 있는 동급 처리와 비교
4. `[[project_omm_lmstudio_guard_gap_and_thread_leak]]`에서 예전에 고친
   "LM Studio 이중 로드" 버그와 같은 클래스의 문제가 Task 3의 새 코드
   경로에서 재발하지 않는가

갭을 발견하면 최소한의 수정으로 고치고, 갭이 없으면 "확인함, 갭 없음"을
보고서에 근거(읽은 코드 위치)와 함께 적는다. 새 테스트는 실제 갭을
고쳤을 때만 추가.

## Task 5: 실기기 라이브 검증 + Firebase rules 체크리스트

Task 1-4 완료 후 착수. 이 태스크는 버그를 잡는 게 목적이다 - 발견한
문제는 이 태스크 안에서 고친다(범위가 크면 발견 사실만 보고서에 적고
컨트롤러 판단에 맡긴다).

**시뮬레이션 절차**:
1. `PATH`에서 `ollama`를 일시적으로 숨기고(예: `PATH` 재구성한
   서브셸에서 실행 - 실제 ollama 바이너리는 건드리지 않는다) `omm
   benchmark`/`omm contribute`를 실행해 `_select_benchmark_engine()`이
   `"lmstudio"`를 고르는지 확인
2. LM Studio 헤드리스 서버 켜고(`~/.cache/lm-studio/bin/lms server
   start`), 이미 받아져 있는 `qwen2.5-0.5b-instruct`로 `omm benchmark`
   전체 흐름(정확도+속도 평가, evidence JSON 저장까지) 실제 완주
3. 가능하면 `omm contribute` 루프도 1개 후보 모델 기준으로 짧게 실행해서
   LM Studio 경로로 daemon health-check/재시작 로직이 실제로 도는지 확인
   (daemon을 수동으로 죽여서 재시작 감지되는지까지 확인하면 더 좋음)
4. 확인 후 `lms unload --all && lms server stop`으로 정리

**Firebase rules 체크리스트 작성**: 코드는 건드리지 않고,
`docs/`(적절한 기존 문서가 있으면 그 옆에, 없으면
`docs/telemetry-v9-lmstudio-engine-checklist.md` 신규) 에 다음을 적은
체크리스트 문서 작성 - [[project_omm_contribute_telemetry_model_provider_rules_bug]]
사고를 그대로 재현하지 않기 위한 사전 확인 목록:
- Firebase 콘솔 → `localfit-8ab57` → Realtime Database → Rules에서
  `/telemetry` 노드의 `engine` 필드 validation이 `'ollama'` 리터럴로
  제한돼 있는지 확인
- 제한돼 있다면 `'lmstudio'`도 허용하도록 고치고 Publish
- Publish 후 `engine: "lmstudio"` 페이로드로 실제 write 200 확인
  (curl 예시 명령 포함)
- 이 작업은 콘솔 접근 권한이 있는 사람이 코드 머지 전 또는 직후 바로
  해야 함 - 안 하면 LM Studio 텔레메트리가 조용히 401로 유실됨

**테스트**: 이 태스크는 코드 변경보다 검증이 목적. Task 1-4에서 놓친
통합 지점(예: `_select_benchmark_engine`과 `_run_contribution_loop`가
실제로 맞물리는지)의 결함을 실행으로 잡는다 - 잡히면 최소 수정 + 해당
회귀를 잡는 테스트 1개 추가.
