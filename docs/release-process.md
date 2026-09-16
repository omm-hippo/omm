# 릴리스 절차 (Release process)

> 한 줄 요약: **릴리스는 자동으로 시작되지 않는다.** 관리자가 `main` 커밋에
> 서명한 `vX.Y.Z` 태그를 push해야 시작되고, 그 뒤 PyPI · npm · GitHub
> Release · Windows 포터블 · Homebrew 알림까지는 전부 자동이다.
> PR을 `main`에 머지하는 것만으로는 아무것도 배포되지 않는다.

이 문서는 2026-09 기준 `.github/workflows/` 내용을 설명한다. 워크플로가
바뀌면 이 문서도 같이 고친다.

## 왜 태그가 필요한가

버전 번호는 커밋할 때마다 자동으로 올라간다(`scripts/pre-commit` 훅이
`pyproject.toml`과 npm 런처 `package.json`의 patch 버전을 한 칸씩 올린다).
그래서 `main`의 버전이 계속 올라가더라도, 그것은 "다음 릴리스 후보 번호"일
뿐 배포된 버전이 아니다.

배포를 시작하는 유일한 신호는 `v*` 태그 push다. 태그는 `src/omm/trust/
allowed_signers`에 등록된 SSH 키로 서명돼야 하고, 모든 릴리스 워크플로가
첫 단계에서 이 서명을 검증한다. 서명 키는 사람이 가지고 있으므로, 이 단계는
의도적으로 사람이 한다.

## 사전 조건

- `allowed_signers`에 등록된 SSH 서명 키가 있는 관리자 계정.
- git에 SSH 서명이 설정돼 있을 것:

  ```sh
  git config --global gpg.format ssh
  git config --global user.signingkey ~/.ssh/<서명용 키>.pub
  ```

- 로컬 `origin/main`이 최신일 것. 태그는 반드시 `origin/main` 히스토리 위의
  커밋을 가리켜야 한다. 릴리스 워크플로가 `merge-base --is-ancestor`로 검사한다.
- 태그 이름이 `pyproject.toml`의 `version`과 정확히 같아야 한다
  (`v` + 버전). `scripts/release_artifacts.py check-tag`가 검사한다.
- 같은 버전이 PyPI/npm에 이미 올라가 있지 않을 것. 두 레지스트리 모두 같은
  버전을 다시 올릴 수 없다. 실패한 릴리스를 다시 하려면 새 버전이 필요하다.

## 절차

1. `origin/main` 최신 커밋으로 이동한다.

   ```sh
   git fetch origin main
   git checkout --detach origin/main
   ```

2. 릴리스할 버전을 확인한다. 아래 두 값이 같아야 한다(훅이 맞춰 주지만
   확인한다).

   ```sh
   grep '^version' pyproject.toml
   python scripts/npm_package.py version
   ```

3. 서명 태그를 만든다. `X.Y.Z`는 2단계에서 본 버전이다.

   ```sh
   git tag -s vX.Y.Z -m "OMM vX.Y.Z"
   ```

4. push 전에 로컬에서 워크플로와 같은 검사를 돌려 본다. 서명자 목록, 태그와
   버전 일치, `HEAD`와 태그 일치, `origin/main` 조상 여부를 한 번에 본다.

   ```sh
   python scripts/release_artifacts.py verify-release --tag vX.Y.Z
   ```

5. 태그를 push한다. 이 순간부터 릴리스가 시작된다.

   ```sh
   git push origin vX.Y.Z
   ```

6. 진행 상황을 본다.

   ```sh
   gh run list --branch vX.Y.Z
   gh release view vX.Y.Z
   ```

## 태그 push 후 자동으로 도는 것

| 워크플로 | 하는 일 | 결과물 |
|---|---|---|
| `release.yml` | wheel/sdist 빌드 → 3개 OS 설치 스모크 → TestPyPI 발행·검증 → PyPI 발행 → 파일 해시·provenance 검증 → 공개 pipx 설치 검증 → Homebrew Tap에 dispatch → Formula 렌더 → GitHub Release에 Python 에셋 추가 | PyPI `omm-model`, GitHub Release 에셋 3개 |
| `npm-release.yml` | 플랫폼 5종 바이너리 패키지 + 런처 빌드 → 번들 검증 → tarball 스모크 → npm 발행 → 공개 레지스트리 설치 검증 | npm `@omm-hippo/omm` + `@omm-hippo/omm-<platform>` 5종 |
| `windows-portable.yml` | PyInstaller로 Windows x64 포터블 빌드 → Defender 스캔 → winget 매니페스트 생성·검증 → GitHub Release에 Windows 에셋 추가 | GitHub Release 에셋 2개, winget 매니페스트 아티팩트 |
| `github-release.yml` (위 두 워크플로가 호출) | 초안(draft) Release를 만들고 에셋을 올린다. 에셋 5개(wheel, sdist, SHA256SUMS, zip, zip.sha256)가 모두 모이면 그때 공개로 전환한다 | 공개 GitHub Release |

Homebrew는 여기서 끝나지 않는다. `sync-homebrew` job은 `omm-hippo/homebrew-omm`
Tap에 `pypi_release_verified` 이벤트만 보낸다. Tap은 Homebrew의 업스트림
cooldown이 끝난 뒤 예약 실행에서 `brew bump`를 돌려 Formula PR을 연다. 그 PR을
관리자가 머지해야 `brew install omm-hippo/omm/omm`이 새 버전을 받는다.
자세한 내용은 [homebrew-release-sync.md](homebrew-release-sync.md).

## 릴리스가 끝났는지 확인하는 법

```sh
gh run list --branch vX.Y.Z                      # 세 워크플로 모두 success
gh release view vX.Y.Z --json isDraft,assets     # isDraft=false, 에셋 5개
pip index versions omm-model                     # 최신이 X.Y.Z
npm view @omm-hippo/omm version                  # X.Y.Z
gh pr list --repo omm-hippo/homebrew-omm         # bump-omm-X.Y.Z PR
```

## 실패했을 때

**원칙: 실패한 job만 다시 돌린다.** 이미 올라간 PyPI/npm 패키지와 GitHub
Release 에셋은 바꿀 수 없다. 워크플로는 재실행 시 이미 발행된 것을 그대로
재사용하도록 짜여 있다(`npm_release.py reuse-published`, `windows-portable.yml`의
"Reuse a verified immutable release asset on reruns").

| 증상 | 원인 | 대응 |
|---|---|---|
| 첫 job에서 "not signed by an allowed signer" | 서명 키가 `allowed_signers`에 없거나 서명 없이 태그를 만듦 | 태그를 지우고(`git push origin :vX.Y.Z`) 서명해서 다시 push. 아직 아무것도 발행되지 않았으므로 같은 버전으로 가능 |
| "release tag does not match pyproject version" | 태그 이름과 `pyproject.toml` 버전 불일치 | 위와 같이 태그를 지우고 맞는 이름으로 다시 |
| "Verify public npm path on <platform>" 실패, 로그에 UNMET OPTIONAL DEPENDENCY | npm CDN 전파 지연. 다른 플랫폼은 통과했다면 거의 확실 | 10~30분 뒤 그 job만 재실행 |
| `sync-homebrew`가 "HOMEBREW_TAP_DISPATCH_TOKEN" 부재로 실패 | 저장소 secret이 없거나 만료 | secret 복구 후 그 job만 재실행. PyPI는 이미 발행된 상태 |
| Tap에 bump PR이 며칠째 안 열림 | Homebrew cooldown, 또는 Tap 예약 실행 실패 | `render-homebrew-formula` job의 `homebrew-formula-X.Y.Z` 아티팩트로 Tap PR을 손으로 연다 |
| GitHub Release가 계속 draft | 에셋 5개 중 일부가 안 올라옴 | 어느 워크플로가 실패했는지 `gh run list --branch vX.Y.Z`로 찾아 그것만 재실행 |
| 빌드 자체가 깨져서 새 코드가 필요 | 버그 | `main`에 수정 PR을 머지하면 훅이 버전을 올린다. 그 새 버전으로 처음부터 다시 태그 |

`windows-portable.yml`과 `npm-release.yml`은 `workflow_dispatch`로도 실행할 수
있다(입력값 `version`, `v` 없이). 태그 push 시 실행이 아예 안 잡혔거나, 나중에
Windows 에셋만 다시 만들 때 쓴다. 이때도 태그는 이미 서명돼 있어야 한다.

## 버전 번호를 손으로 올리고 싶을 때 (minor/major)

훅은 커밋이 `pyproject.toml`의 `version` 줄을 직접 바꾸면 그 값을 존중하고
patch를 올리지 않는다. 따라서 `version = "0.4.0"`으로 고친 커밋을 만들어
머지하면 된다. npm 런처 `package.json`은 훅이 같은 값으로 맞춘다.

브랜치 하나에서 커밋을 여러 번 하면 patch가 커밋 수만큼 뛴다. 릴리스 번호가
띄엄띄엄 보이는 것은 정상이며(예: 0.3.33 → 0.3.41), 굳이 예쁘게 만들 필요는
없다. 정리하고 싶다면 머지 전에 버전 줄을 원하는 값으로 되돌리는 커밋 하나를
추가하면 된다.

## 자주 묻는 것

**Q. 머지했는데 왜 배포가 안 되나요?**
태그를 아무도 안 찍었기 때문이다. 이 문서의 "절차"를 따른다.

**Q. 자동 태그로 바꾸면 안 되나요?**
태그 서명이 이 저장소의 배포 신뢰 경계다. 설치 스크립트와 자동 업데이트는
`allowed_signers`로 검증된 커밋만 받는다(`CONTRIBUTING.md`의 "Trusted
pull-request head", `docs/install-staging-flow.md`). CI 봇 키로 태그를 찍게
하면 그 경계가 CI 토큰 하나로 좁아진다. 바꾸려면 별도 설계 논의가 필요하다.

**Q. 지난 릴리스(v0.3.41)는 왜 일부 실패로 표시되나요?**
두 job이 실패했지만 배포 자체는 정상이었다. npm 윈도우 검증은 pwsh에서
환경변수가 비어 들어가던 워크플로 버그였고 PR #249로 고쳐졌다(태그 이후 머지라
그 실행에는 반영되지 않았다). Homebrew 버전 동기화 검사는 Tap이 cooldown 뒤에
따라오는 구조상 항상 먼저 실패했고, 지금은 그 검사 단계가 제거됐다.
