# `install.sh` / `omm update`에 커밋 서명 검증 추가

## 배경

이 리포는 `main` 브랜치를 그대로 따라가는 롤링 릴리스 모델이다
([[project-omm-update-editable-clone]]). `install.sh`는 `git clone`으로,
`omm update`는 `git fetch && git reset --hard origin/main`으로 `main`을
그대로 받아 실행한다 (`_git_update_src`, `cli.py:682`). 둘 다 HTTPS 전송
무결성(TLS)은 보장되지만, **받아온 코드가 실제로 신뢰하는 메인테이너가
커밋했는지는 검증하지 않는다** - push 권한이 있는 계정/토큰이 하나라도
털리면 다음 `omm update`가 그 코드를 그대로 실행한다.

GitHub 저장소(`minigu5/Omm`)에 push 권한이 있는 사람은 `minigu5`(본인,
SSH 서명 키 등록 완료)와 `Matwaetle`(현재 서명 키 없음) 둘이다.

## 신뢰 앵커 배치 (핵심 설계 결정)

검증에 쓰는 신뢰 목록(`allowed_signers`)이 검증 대상 커밋 안에 있으면
안 된다 - push 권한을 가진 공격자가 자기 키를 같은 커밋에 끼워넣고
자기 서명을 자기가 통과시킬 수 있기 때문이다. 따라서:

- **`omm update` (이미 설치된 상태)**: `git fetch`로 `origin/main`을
  받아온 직후, 아직 `reset --hard`로 워킹트리를 덮어쓰기 *전* 상태에서
  **업데이트 전(구버전) 코드에 이미 박혀있던** `trust/allowed_signers`로
  새로 받은 `origin/main`의 서명을 검증한다. 통과해야 `reset --hard`를
  진행한다. N번째 상태가 N+1번째를 검증하는 체인이 되고, 키 교체/추가도
  기존에 신뢰된 키로 서명된 커밋을 통해서만 가능해진다 (자연스러운 키
  로테이션).
- **최초 설치 (`install.sh`)**: 이전 신뢰 상태가 없다 (TOFU). 유일한
  신뢰 앵커는 `install.sh` 스크립트 자체에 하드코딩한다. 사용자가
  `curl ... | sh`를 실행하는 시점에 이미 그 스크립트 내용을 신뢰한
  것이므로, 그 신뢰를 클론된 repo 검증으로 넘겨준다.
- **한계 (범위 밖)**: 최초 설치는 여전히 TOFU라 `raw.githubusercontent.com`
  / HTTPS 채널 신뢰에 의존한다. 별도 out-of-band 루트 없이는 원천적으로
  없앨 수 없고, 이번 작업 범위가 아니다.

## 신뢰 앵커 파일

`src/omm/trust/allowed_signers` - SSH `allowed_signers` 포맷, 한 줄에
한 메인테이너:

```
seong381400@gmail.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPh12ERbI3Yx6DPiaROPjCyI2GIQXb9Ihbp9J9L4bnpe
```

일반 패키지 데이터로 wheel에 포함된다 (`quality-pack-v1.json`과 같은
패턴, `pyproject.toml`의 `packages = ["src/omm", ...]`가 서브트리 전체를
담아간다 - 별도 include 설정 불필요). `Matwaetle`은 아직 서명 키가 없어
목록에 없다 - branch protection이 커밋 서명을 강제하게 되면 자기 키를
등록하기 전까지는 push 자체가 막힌다 (의도된 동작).

## 검증 메커니즘

```
git -c gpg.format=ssh -c gpg.ssh.allowedSignersFile=<anchor경로> \
    -C <repo> verify-commit <commit>
```

exit 0 = 통과. SSH 커밋 서명 검증은 Git 2.34+에서만 지원되므로 `install.sh`
와 `omm update` 둘 다 사전에 git 버전을 확인한다 (기존 Python 3.10 체크와
같은 패턴). **검증이 아예 불가능한 경우(git too old)도 검증 실패로
취급한다 (fail-closed)** - "검증 못 하면 통과시킨다"는 이 기능의 목적을
무력화하므로.

## 통합 지점

- **`install.sh`**: `git clone` 직후, `pipx install` 전에 하드코딩된
  anchor로 클론된 repo의 `HEAD`를 검증. 실패하면 클론 삭제하고 exit 1.
- **`cli.py::_git_update_src()`**: `git fetch` 직후, `git rev-parse
  origin/main`으로 타겟 커밋을 얻고, `reset --hard` 전에
  `trust.current_trust_anchor()`(현재 설치된, 아직 안 바뀐 SRC_DIR의
  anchor)로 검증. 실패하면 `reset --hard`를 실행하지 않고 실패를 반환.
- **`cli.py::_migrate_to_editable_install()`**: 임시 클론(`tmp_dir`) 후,
  `SRC_DIR`을 덮어쓰기 전에 **현재 실행 중인 omm 패키지**
  (`importlib.resources`로 읽은, pipx venv에 박힌 old anchor)로 검증.
  실패하면 `tmp_dir`만 지우고 기존 설치는 그대로 둔다 (클론 실패 시
  기존 동작과 동일한 불변식).

## 롤아웃 부트스트랩 허점 (구조적으로 불가피)

이 기능이 배포되는 시점에 이미 설치된 유저들의 구버전에는
`trust/allowed_signers` 자체가 없다 - 그다음 첫 `omm update` 때 비교할
구(舊) anchor가 없다. `trust.current_trust_anchor()`가 `None`을 반환하는
경우(anchor 파일 없음) **이번 1회는 검증을 건너뛰고 통과시킨다** (최초
설치와 동급 TOFU). 신버전은 anchor를 갖고 있으므로 그다음부터는 정상
체인이 작동한다. 이 1회짜리 허점은 신버전이 존재하기 전에는 검증할
방법 자체가 없으므로 구조적으로 피할 수 없다.

## GitHub 설정 변경

`main` 브랜치에 "Require signed commits" branch protection을 켠다.
서명 안 된 커밋은 push 자체가 거부되므로, 다른 머신/`Matwaetle`이 서명
없이 실수로 push해서 모든 유저의 `omm update`가 하드페일하는 사고를
막는다.

## 에러 메시지

검증 실패 시 어떤 커밋(짧은 SHA)이 왜 실패했는지(서명 없음 / 신뢰 안 된
키 / git 버전 부족) `git verify-commit`의 stderr를 그대로 포함해 출력한다.
`install.sh`는 클론을 지우고 exit 1, `omm update`는 `SRC_DIR`을 건드리지
않고 exit 1 (기존 실패 경로와 동일하게 `err_console`로 출력 후
`typer.Exit(1)`).

## 테스트

- `src/omm/trust/__init__.py`의 `verify_commit`/`current_trust_anchor`
  단위 테스트: 실제 git 저장소를 `tmp_path`에 만들어 SSH 키로 서명한
  커밋과 안 한 커밋 양쪽에 대해 통과/실패 검증 (테스트 전용 ed25519 키
  생성, 실제 메인테이너 키와 무관).
- `tests/test_cli_update.py`에 `_git_update_src`/
  `_migrate_to_editable_install`이 `trust.verify_commit`을 호출하고,
  실패 시 `reset --hard`/`rename`을 건너뛰는지 monkeypatch로 검증.
- `install.sh`는 기존에 셸 레벨 테스트가 없음 - 수동 검증(정상 클론
  통과, anchor 안 맞는 fork로 clone했을 때 거부)으로 대체.
