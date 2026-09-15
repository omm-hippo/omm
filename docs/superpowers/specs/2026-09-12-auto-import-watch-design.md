# auto-import 백그라운드 감시 — 설계

날짜: 2026-09-12
상태: 승인 대기

## 배경

`omm import`는 이미 ollama/lmstudio 등 외부 러너 디렉터리를 스캔해서(`scan_import.py`)
아직 omm 허브에 없는 GGUF를 sha256 기준으로 그룹화하고(`group_by_hash`) 허브로 편입
+ 다른 러너로 링크까지(`adopt_group`) 한 번에 처리한다. 문제는 이게 수동 트리거라는
점 — ollama/LM Studio 자체 검색·설치가 omm보다 간편하거나 빠를 수 있는데, 그때마다
사용자가 `omm import`를 따로 실행해줘야 해서 번거롭다.

이 기능은 그 수동 트리거를 없애고, 러너가 새 모델을 받을 때마다 자동으로
`omm import`와 같은 일을 백그라운드에서 하도록 만든다. 러너 자체의 다운로드
포맷/프로토콜은 건드리지 않는다 (ollama의 레지스트리 API, LM Studio의 카탈로그
소스를 omm으로 바꾸는 것이 아니다 — 그건 각 러너가 커스텀 소스를 지원하지 않아서
불가능하거나 매우 무겁다는 게 이번 브레인스토밍에서 확인된 사실이다).

## 목표 / 비목표

- 목표: 러너 디렉터리에 새 GGUF가 생기면(다운로드 완료 후) 자동으로 허브 편입 + 모든
  설치된 러너로 링크, 사용자 알림.
- 비목표: 러너 자체의 검색/다운로드 소스를 omm으로 교체. 러너 포맷(ollama blob,
  manifest 등) 재구현. pin/rollback, export/import, unlink — 이 셋은 별도 브레인스토밍
  (2026-09-12, 이슈 #295-297)에서 나온 무관한 트랙.

## 아키텍처

```
[로그인 시 OS 서비스 시작]
        │
        ▼
  omm _auto-import-run  (숨김 서브커맨드, 상주 실행)
        │  watchdog.Observer 등록
        ▼
  러너별 모델 디렉터리  (linker.ollama_models_dir() 등 기존 함수 재사용)
        │  FS 이벤트 (생성/수정)
        ▼
  경로별 debounce 타이머 (15초 무변경 대기)
        │  타이머 만료
        ▼
  파일 크기 안정성 2회 확인 (다운로드 진행 중 편입 방지)
        │  안정적이면
        ▼
  scan_import.find_external_models → group_by_hash → 신규 해시만 adopt_group
        │
        ▼
  notify.notify(...)  +  runlog 기록
```

새 모듈 2개, 기존 모듈 재사용, 새 트리거/서비스 등록 코드만 추가하는 구조.

## 컴포넌트

- **`src/omm/watch.py`** (신규)
  - `run_watch_loop()` — 지원 러너 디렉터리마다 watchdog observer 등록, 존재하지
    않는 디렉터리(미설치 러너)는 스킵.
  - `_debounced_scan(engine: str)` — 경로별 15초 debounce, 만료 후 파일 크기
    2회 측정(간격 duration 확보) 후 안정적일 때만 진행.
  - 진행 시 `scan_import.find_external_models()` → `group_by_hash()` →
    이미 `registry.load_registry()`에 있는 해시는 건너뛰고 신규 그룹만
    `adopt_group()`.
  - `adopt_group` 실패(`OSError`, `linker.LinkError`)는 잡아서 알림 + 로그, 루프는
    계속 실행 (`_run_import_flow`의 기존 catch 패턴과 동일).

- **`src/omm/notify.py`** (신규)
  - `notify(title: str, body: str) -> None` — `plyer.notification.notify` lazy
    import. 실패해도 예외 삼킴 (`runlog.py`/`usage.py`처럼 import-side-effect-free,
    호출 시점에만 의존성 접근).

- **`omm _auto-import-run`** (숨김 CLI 서브커맨드) — 서비스가 실행하는 전경 프로세스.
  `_SKIP_AUTO_IMPORT_SUBCOMMANDS`에도 추가해서 기존 1회성 온보딩 스캔과 안 겹치게 함.

- **`omm setting auto-import enable|disable|status`** — `setting_app` 아래 새
  서브그룹 (`upload_app`과 같은 패턴).
  - `enable`: `watchdog`/`plyer` import 가능 확인 (안 되면
    `pip install "omm-model[watch]"` 안내 후 중단) → OS별 서비스 등록 → `config.json`에
    `auto_import_enabled: true`.
    - macOS: `~/Library/LaunchAgents/com.omm.auto-import.plist` + `launchctl load`.
    - Linux: `~/.config/systemd/user/omm-auto-import.service` +
      `systemctl --user enable --now`.
    - Windows: `schtasks /create` (로그온 트리거, 관리자 권한 불필요).
  - `disable`: 서비스 완전 종료 + 파일 제거 + `auto_import_enabled: false`.
  - `status`: 설정 플래그 + 실제 서비스 등록/실행 상태 확인해서 출력.
  - 이미 켜진 상태에서 `enable` 재호출, 이미 꺼진 상태에서 `disable` 재호출은
    멱등 (안내만 하고 끝).

- **의존성**: `pyproject.toml`에 신규 optional extra
  `watch = ["watchdog>=4", "plyer>=2.1"]`. 기본 설치엔 포함 안 됨 (nvidia extra와
  같은 패턴). `watch.py`/`notify.py` 내부에서만 lazy import, `cli.py` 모듈 top-level엔
  안 올려서 기존 시동 속도(`omm help` ~140ms) 안 깨짐.

## 안전장치 (핵심)

다운로드 진행 중인 파일을 그대로 허브로 편입하면, 원본 경로가 심링크로 바뀌거나
하드링크로 공유되어 러너가 이후에도 쓰기를 계속하는 동안 허브 사본이 손상될 위험이
있다. 그래서 편입 전 반드시:

1. FS 이벤트 후 15초 동안 추가 이벤트 없을 것 (debounce)
2. 그 후 파일 크기를 두 번 측정해서(사이 간격을 두고) 변화 없을 것

두 조건 다 만족해야 `adopt_group` 호출. ollama는 블롭이 content-addressed라
최종 이름으로 rename된 시점엔 이미 불변이라 이 안전장치가 사실상 보수적으로만
작동하고, LM Studio 등 다른 러너의 실제 다운로드 임시파일 처리 방식은 구현 단계에서
실제 앱으로 검증한다 (스펙 승인을 막는 항목은 아님, 구현 계획의 검증 태스크로 남김).

## 설정 / 온보딩

- 기본값 꺼짐, opt-in. `omm setup` 온보딩(`onboarding.run_data_sharing_step`,
  usage+crash 동의 흐름)에는 안 넣음 — 이건 원격 전송이 없는 로컬 자동화라 그
  동의 흐름과 성격이 다름.
- `PRIVACY.md`에 "로컬 파일 자동화이며 원격 전송 없음" 한 줄 추가.

## 에러 처리

- 러너 디렉터리 없음(미설치) → 스킵, 에러 아님.
- `adopt_group` 실패 → 알림 + `runlog` 기록, 루프 유지.
- `watchdog`/`plyer` 미설치 상태에서 `enable` → 안내 후 중단, 서비스 등록 안 함.
- `disable` 시 서비스 프로세스 확실히 종료 (좀비 프로세스 방지).

## 테스트 계획

- `scan_import`/`adopt_group`은 기존 테스트 재사용, 새 로직 없음.
- `watch.py`: debounce/크기-안정성 로직 유닛테스트, `watchdog` observer는 목.
- 서비스 등록 함수: 실제 `launchctl`/`systemctl`/`schtasks` 실행 없이 생성되는
  plist/unit 파일 내용만 검증 (subprocess 목).
- 구현 완료 후 실제 ollama/LM Studio로 실제 다운로드 트리거해서 자동 편입되는지
  실사용 검증 필요 (CLAUDE.md 검증 스타일 — 스펙/유닛테스트만으로 끝내지 않음).

## 열린 질문 / 구현 단계에서 확정할 것

- LM Studio 실제 다운로드 임시파일 명명 규칙 (temp+rename 여부) — 실제 앱으로 확인.
- anythingllm/msty/text-generation-webui/koboldcpp/jan 각각의 실제 모델 디렉터리 경로
  확정 — `scan_import.py`의 기존 스캔 함수들이 이미 알고 있는 경로 그대로 재사용
  (신규 탐색 불필요), 다만 각 경로가 실제로 watchdog observer로 감시 가능한지
  (예: 심볼릭 링크 경유 등) 구현 시 확인.
