# 첫 실행 온보딩 위저드 설계 (이슈 #24)

## 배경

omm은 이름값(package **manager**)을 하려면 설치까지 실제로 대신 해줘야 한다. 지금은 `omm scan`이 미설치 엔진을 링크만 던져줄 뿐 아무것도 대신 해주지 않는다. GitHub 이슈 #24 "omm 최초 실행 시 초기 설정창 (claude code 스타일)"에 대응한다.

핵심 요구사항 3가지:
1. 설치 완료 직후 ASCII 아트 배너
2. 바로 이어서 빠른 설정 위저드 — 컴퓨터에 설치된 로컬 AI 런처 확인 + 원하는 걸 체크하면 omm이 직접 설치
3. 서버/업로드 설정은 위저드에 넣지 않음 (기존 `telemetry_send_policy=ask` 기본값이 이미 안전해서 처음부터 물을 필요 없음)

## 트리거 & 지속성

- `cli.py:_root()` 콜백 맨 앞에서 `config.get("onboarding_completed")` 체크. `True`면 기존 버전 배너 그대로.
- **마이그레이션 함정**: `config.py:load_config()`는 `CONFIG_PATH`가 아예 없을 때만 `DEFAULT_CONFIG`를 새로 쓴다(83-85행). 기존 사용자는 `config.json`은 있지만 새 키가 없어서 merge 시 `DEFAULT_CONFIG`의 기본값을 그대로 물려받는다. 그래서:
  - `DEFAULT_CONFIG["onboarding_completed"] = True` (기존 사용자가 업데이트해도 안 뜨게)
  - `load_config()`의 "파일 없음 → 새로 생성" 분기에서만 명시적으로 `onboarding_completed=False`로 저장 (진짜 신규 설치만 감지)
- 비대화형(TTY 아님, CI 등)이면 위저드 스킵, 기존 배너만 출력, 플래그는 `False`로 유지 (다음 대화형 실행 때 재시도).
- `omm setup` 커맨드 신설 — 언제든 재실행 가능. 완료 시 플래그 다시 `True`로.
- 위저드 도중 Ctrl+C → `onboarding_completed` 안 씀, 다음 실행 때 다시 뜸.

## 위저드 흐름

```
1) ASCII 아트 "omm" 로고 (터미널 폭 대응 — 기존 다운로드 프로그레스바처럼 좁으면 축약판)
2) 하드웨어 스캔 요약 (scan_hardware() 재사용, omm scan과 동일 로직/출력)
3) 로컬 AI 런처 체크리스트 (questionary.checkbox, omm import에서 쓰는 패턴 재사용)
   - linker.ENGINES 순회, is_engine_installed()로 이미 설치된 건 정보성으로만 표시(체크 불가)
     예: "Ollama (installed)"
   - 미설치 항목만 체크 가능. 항목마다 자동화 수준을 한 줄로 명시:
     "[ ] Ollama                (auto-install)"
     "[ ] LM Studio              (not yet automated — see compatibility wiki)"
     "[ ] Jan                    (not yet automated — see compatibility wiki)"
     "[ ] AnythingLLM            (not yet automated — see compatibility wiki)"
     "[ ] Msty                   (not yet automated — see compatibility wiki)"
     "[ ] text-generation-webui  (not yet automated — see compatibility wiki)"
     "[ ] KoboldCpp              (not yet automated — see compatibility wiki)"
   - Ollama 체크 시: 아래 "Ollama 자동 설치" 실행, 진행 로그를 터미널에 실시간으로 보여줌
   - 나머지 체크 시: COMPATIBLE_PROGRAMS_URL 안내만 (향후 PR에서 하나씩 같은 인터페이스로 자동화 예정)
4) 완료 메시지 + "나머지 설정은 `omm setting`에서" 한 줄 안내
```

## `install_engine()` 인터페이스 (향후 확장 대비)

`linker.py`에 `is_engine_installed()`와 대칭되는 함수 신설:

```python
def install_engine(key: str) -> EngineInstallResult:
    if key == "ollama":
        return _install_ollama()
    raise NotImplementedError(f"no automated installer for engine: {key}")
```

- `if/elif` 스타일 유지 (`is_engine_installed`와 동일 이유: 테스트에서 개별 함수 monkeypatch 가능해야 함)
- `EngineInstallResult`: `installed` / `failed` / `unsupported_platform` 상태 + 사람이 읽을 메시지
- 나머지 6개 엔진은 이번 스펙 범위 밖. 이슈에 후속 서브태스크로 남기고, 구현될 때마다 이 `if/elif`에 분기 하나씩 추가하는 구조라 위저드 UI 코드는 안 건드림.

## Ollama 자동 설치 (이번 스펙에서 실제로 구현하는 유일한 엔진)

- macOS/Linux: `curl -fsSL https://ollama.com/install.sh | sh` 서브프로세스 실행, 비대화형으로 확인됨.
- Windows: `winget`이 PATH에 있으면 `winget install -e --id Ollama.Ollama --silent`. 없으면 자동 설치 포기, 수동 링크 안내로 폴백.
- **진행 로그를 실시간으로 보여주는 게 핵심** (설치를 "확실히 대신 해주고 있다"는 걸 사용자가 체감해야 함). `cli.py:_run_pipx_install`이 이미 쓰는 패턴(`subprocess.Popen` + `stdout=PIPE` 라인 단위 스트리밍)을 재사용하되, Ollama 설치 스크립트 출력 형식은 pipx만큼 예측 가능하지 않으므로 스테이지 매칭 대신 **원본 라인을 그대로 흘려보내는 raw passthrough**로 진행 상황을 보여줌.
- 설치 후 `is_ollama_installed()`로 재확인해서 성공/실패 표시. 실패해도 위저드 중단 안 하고 다음 단계로 진행.
- winget 패키지 ID(`Ollama.Ollama`)는 구현 단계에서 실제로 검증 필요 (지금은 확신 없음, 추정치).

## 에러 처리

- 네트워크 없음/설치 스크립트 실패 → 트레이스백 대신 한 줄 에러 + 수동 링크 폴백 (기존 disk-space/error-resilience 패턴과 동일 톤)
- 위저드 자체가 실패해도 `omm`의 나머지 기능에는 영향 없음 (플래그만 `False`로 남아 다음 실행 때 재시도)

## 테스트 계획

- config 마이그레이션 분기(신규 설치 vs 기존 업그레이드) 단위테스트 — 이게 제일 중요, 기존 사용자한테 위저드가 잘못 뜨면 안 됨
- 위저드 TTY/non-TTY 스킵 분기
- `install_engine("ollama")` 서브프로세스는 monkeypatch로 mock, 실제 curl/winget 실행 안 함
- `omm setup` 재실행 커맨드 테스트
- 체크리스트에서 이미 설치된 엔진은 선택 불가능한지, 자동화 안 된 엔진 체크 시 링크만 뜨는지 테스트

## 범위 밖 (후속 이슈)

- LM Studio / Jan / AnythingLLM / Msty / text-generation-webui / KoboldCpp 자동 설치 — 같은 `install_engine()` 인터페이스로 엔진별 후속 PR에서 하나씩 추가
- 서버/업로드 설정 마법사 — 기존 `omm setting telemetry` / `omm setting upload`로 계속 별도 관리
