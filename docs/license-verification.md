# 오픈소스 라이선스 검증 절차

omm은 MIT 라이선스로 배포된다. 그런데 omm이 가져다 쓰는 다른 오픈소스 패키지는 각자 자기
라이선스가 있고, 그중 일부(GPL, AGPL 같은 "카피레프트")는 "이걸 섞어서 배포하면 너희 코드도
같은 조건으로 공개해야 한다"는 의무를 붙인다. 이 문서는 그런 의무가 omm에 몰래 섞여 들어오지
않도록 확인하는 방법을 정리한다.

검사는 사람이 매번 손으로 하지 않는다. `scripts/check_licenses.py`가 목록을 만들고 분류하고,
CI 잡 `Third-party licenses`가 모든 PR에서 같은 검사를 돌린다.

## 한눈에 보기

| 무엇 | 어디 |
|---|---|
| 검사 스크립트 | `scripts/check_licenses.py` |
| 정책 파일 (분류표·예외·외부 실행 앱 목록) | `scripts/license_policy.json` |
| 배포용 고지 파일 (스크립트가 생성) | `THIRD_PARTY_NOTICES.md` |
| 웹 화면에 들어간 npm 패키지의 라이선스 전문 | `src/omm/web/static/third-party-licenses.txt` (`web-ui/scripts/licenses.mjs`가 생성) |
| 단독 실행 파일(npm·Windows ZIP)의 라이선스 전문 | `THIRD_PARTY_LICENSES.txt` (`scripts/bundled_licenses.py`가 빌드 때 생성) |
| CI | `.github/workflows/ci.yml`의 `licenses` 잡 (Linux, 1분 안팎) |
| 테스트 | `tests/test_check_licenses.py`, `tests/test_bundled_licenses.py` |

## 6단계 검증 절차

### 1단계. 의존성 전체 목록 만들기

스크립트가 아래를 모두 모은다. 직접 쓰는 패키지뿐 아니라, 그 패키지가 또 쓰는 패키지(전이
의존성)까지 끝까지 따라간다.

- **Python, 배포되는 것**: `pyproject.toml`의 기본 의존성 + `server`, `nvidia`, `watch` 추가
  기능, 그리고 npm/Windows 단독 실행 파일에 들어가는 목록(`requirements-npm-binary.txt`,
  `requirements-windows-portable.txt`). 설치된 패키지 정보(`importlib.metadata`)를 읽는다.
- **Python, 개발용**: `dev` 추가 기능, `requirements-train.txt`, `requirements-release.txt`,
  빌드 도구(`build`, `hatchling` 등). 사용자에게 배포되지 않으므로 따로 보고만 한다.
- **npm**: `web-ui/package-lock.json`, `cf-worker/package-lock.json`. 잠금 파일에 패키지마다
  라이선스가 적혀 있어서 `node_modules` 없이도 오프라인으로 읽을 수 있다. 잠금 파일에서
  `dev` 표시가 있는 것은 개발용, 없는 것은 배포용이다.
- **npm 실행기**(`packaging/npm/launcher`): 잠금 파일이 없다. 대신 "omm 자기 패키지
  (`@omm-hippo/...`)만 의존할 수 있다"는 규칙을 검사한다. 남의 패키지가 들어오면 실패한다.

운영체제·Python 버전에 따라 깔리는 패키지가 다를 수 있다(예: `tomli`는 Python 3.11 미만에서만).
스크립트는 omm이 지원하는 모든 운영체제(Linux, macOS, Windows)와 Python 3.10~3.14를 한꺼번에
가정하고 목록을 만든다. 그래서 Windows에서 만들든 Linux CI에서 만들든 결과가 같다. 일부
환경에서만 깔리는 패키지는 정책 파일에 라이선스를 직접 적어 둬야 한다(안 적으면 "모름"으로
실패).

### 2단계. 라이선스 종류 자동 판별

패키지마다 라이선스를 이 순서로 읽는다.

1. 정책 파일의 `overrides` (사람이 원문을 확인하고 적은 값, 이유 필수)
2. 표준 라이선스 표기(`License-Expression`, 예: `Apache-2.0 OR BSD-3-Clause`)
3. 옛 방식의 `License` 칸 (짧은 이름일 때만. 라이선스 전문이 통째로 들어 있으면 무시)
4. 분류자(`License :: OSI Approved :: MIT License` 같은 것)
5. 그래도 모르면 `UNKNOWN`

npm은 잠금 파일의 `license` 값을 그대로 쓴다.

### 3단계. GPL/LGPL 같은 카피레프트 찾아내기

읽은 라이선스를 네 가지로 나눈다. 분류표는 `scripts/license_policy.json`의 `classes`에 있다.

| 분류 | 뜻 | 예 |
|---|---|---|
| 허용형(permissive) | 저작권 표시만 남기면 자유롭게 쓸 수 있음 | MIT, BSD, Apache-2.0, ISC, PSF-2.0 |
| 약한 카피레프트(weak-copyleft) | 그 라이브러리 파일 자체를 고치면 그 부분만 공개 | MPL-2.0, LGPL |
| 강한 카피레프트(strong-copyleft) | 섞어서 배포하면 전체를 같은 조건으로 공개 | GPL, AGPL |
| 모름(unknown) | 라이선스를 못 찾았거나 처음 보는 이름 | `UNKNOWN`, 자체 라이선스 |

여러 라이선스가 섞인 표기는 이렇게 판단한다.

- `A OR B`: 둘 중 하나를 고를 수 있으므로 **더 느슨한 쪽**으로 본다.
- `A AND B`: 둘 다 지켜야 하므로 **더 엄격한 쪽**으로 본다.
- `A WITH 예외`: 예외 조항은 일단 무시하고 `A`로 본다. 예외 덕분에 괜찮다면 그 이유를
  사람이 정책 파일 `allow`에 적어야 한다.

### 4단계. 라이선스 충돌 확인

omm 자체는 MIT다. MIT 코드와 섞어서 배포했을 때 문제가 되는 것은 강한 카피레프트(그리고
라이선스를 모르는 것)다. 그래서 규칙은 이렇다.

- **배포되는 패키지**가 허용형이 아니면 **실패**한다. 약한 카피레프트도 일단 실패시키고,
  사람이 검토한 뒤 `allow`에 "패키지 이름 + 정확한 라이선스 + 이유"를 적어야 통과한다.
  나중에 그 패키지 라이선스가 바뀌면 다시 실패한다.
- **개발용 패키지**는 실패시키지 않고 보고만 한다. 사용자에게 배포되지 않으니 omm 라이선스에
  영향이 없다.

### 5단계. 소스 공개 의무 검토

소스 공개 의무는 "카피레프트 코드를 고쳐서, 또는 섞어서 배포할 때" 생긴다. 현재 배포물에 들어가는
것 중 허용형이 아닌 것은 아래 세 개뿐이고, 셋 다 omm 쪽에 소스 공개 의무를 만들지 않는다.

| 패키지 | 라이선스 | 왜 괜찮은가 |
|---|---|---|
| certifi | MPL-2.0 | 파일 단위 카피레프트다. certifi 파일을 고치지 않고 그대로 쓰므로 omm 코드는 공개 의무가 없다. certifi 파일은 원래 라이선스로 남는다. |
| pyinstaller | GPL-2.0-or-later + 부트로더 예외 | 단독 실행 파일에 들어가는 것은 부트로더뿐이고, 부트로더 예외가 "이걸 넣어서 어떤 조건으로든 배포해도 된다"고 명시한다. PyInstaller 자체는 배포하지 않는다. |
| pyinstaller-hooks-contrib | GPL-2.0-or-later AND Apache-2.0 | GPL인 일반 훅은 빌드하는 컴퓨터에서만 돌고 배포되지 않는다. 실행 파일에 들어가는 런타임 훅은 Apache-2.0이다. |

개발용에서는 `pathspec`(MPL-2.0, hatchling이 씀), `lightningcss`(MPL-2.0, Vite가 씀),
`@img/sharp-libvips-*`(LGPL-3.0, wrangler가 씀)가 보고된다. 모두 빌드·테스트 때만 쓰고 배포하지
않으므로 의무가 없다.

### 6단계. 배포물의 NOTICE·저작권 고지 확인

허용형 라이선스도 "배포할 때 저작권 표시와 라이선스 문구를 같이 넣어라"는 조건이 있다.
배포 경로별로 확인한 결과는 이렇다.

| 배포물 | 들어가는 제3자 코드 | 고지 상태 |
|---|---|---|
| PyPI wheel (`omm-model`) | 없음. 의존성은 pip이 따로 설치하고, 각 패키지가 자기 라이선스 파일을 함께 설치한다 | 문제 없음 |
| PyPI sdist | 없음 (`THIRD_PARTY_NOTICES.md` 포함) | 문제 없음 |
| 웹 화면 자산 (`src/omm/web/static`) | React 등 npm 105개 | `third-party-licenses.txt`에 전문 포함, CI가 최신인지 확인 |
| npm 단독 실행 파일 `@omm-hippo/omm-*` | Python 패키지 45개 + CPython + OpenSSL | `LICENSE` + `THIRD_PARTY_LICENSES.txt`(전문·NOTICE 포함) |
| Windows 포터블 ZIP | 위와 같음 | `LICENSE.txt` + `THIRD_PARTY_LICENSES.txt` |

단독 실행 파일 두 종류에 라이선스 전문을 넣는 방법은 아래 절에 있다.

## 단독 실행 파일의 라이선스 전문 (`THIRD_PARTY_LICENSES.txt`)

npm 플랫폼 패키지와 Windows 포터블 ZIP은 PyInstaller로 만든 실행 파일 하나에 CPython,
OpenSSL, Python 패키지를 모두 담는다. 그래서 두 배포물에는 `THIRD_PARTY_LICENSES.txt`를 같이
넣는다. 이 파일은 빌드하는 그 컴퓨터에서 `scripts/bundled_licenses.py`가 만든다.

- **패키지 목록**: `requirements-npm-binary.txt`(Windows는 `requirements-windows-portable.txt`)에서
  빌드 도구를 뺀 의존성 전체 + 실행 파일에 들어가는 빌드 도구(PyInstaller 부트로더, 런타임 훅).
  라이선스 이름과 예외 판단은 `check_licenses.py`와 같은 정책 파일을 쓴다.
- **전문**: 설치된 각 패키지의 `.dist-info`에서 `LICENSE`, `COPYING`, `NOTICE`, `AUTHORS`
  파일과 `licenses/` 폴더를 그대로 옮긴다. `requests`, `questionary`의 `NOTICE`도 여기서 들어간다.
- **CPython**: 빌드에 쓴 Python의 `LICENSE.txt`. **OpenSSL**: 저장소에 넣어 둔
  `packaging/licenses/OpenSSL-LICENSE.txt`(출처 주소 포함).
- **웹 화면**: 설치된 omm의 `web/static/third-party-licenses.txt`도 마지막에 붙인다. 이 파일은
  `--collect-data omm` 때문에 실행 파일 안에도 이미 들어 있지만, 밖에서 읽을 수 있게 한 번 더 넣는다.
- **실패 조건**: 라이선스 파일이 없는 패키지가 있으면 빌드가 멈춘다. 정말 파일이 없는 패키지라면
  원문을 `packaging/licenses/`에 넣고 정책 파일 `python.license_texts`에
  `{"path": ..., "reason": ...}`로 이유와 함께 적는다.
- **검사**: `npm_package.py`, `npm_release.py`, `windows_portable.py`가 파일 목록에 이 파일이 있는지,
  생성된 머리말과 CPython·OpenSSL 절이 있는지 확인한다. 이 파일이 생기기 전에 올린 Windows ZIP을
  재실행 때 다시 검사하는 경로만 예외로 이 파일 없이도 통과한다.

직접 만들어 보려면 (배포 의존성이 설치된 환경에서):

```sh
python scripts/bundled_licenses.py --target linux-x64-gnu --output THIRD_PARTY_LICENSES.txt
```

## 실행 방법

설치된 패키지 정보를 읽기 때문에, 배포되는 의존성이 설치된 환경에서 돌린다.

```sh
python -m pip install packaging -e ".[server,nvidia,watch]" -r requirements-npm-binary.txt

python scripts/check_licenses.py           # 결과만 출력
python scripts/check_licenses.py --write   # THIRD_PARTY_NOTICES.md 다시 만들기
python scripts/check_licenses.py --check   # CI와 같은 검사
```

종료 코드: `0` 문제 없음, `1` 정책 위반(배포물에 카피레프트·모름), `2` `THIRD_PARTY_NOTICES.md`가
오래됨.

의존성을 추가·변경하면 `--write`로 고지 파일을 다시 만들어 같이 커밋한다. CI가 실패하면 출력에서
`VIOLATION` 줄을 보고, 패키지를 바꾸거나 원문을 확인한 뒤 정책 파일에 이유와 함께 적는다.
개발용 의존성을 다 설치하지 않으면 개발용 일부가 `missing`으로 보고되지만 실패는 아니다.

## 현재 결과 요약 (2026-10-24)

| 구성 요소 | 구분 | 패키지 수 | 허용형 | 약한 카피레프트 | 강한 카피레프트 | 모름 |
|---|---|---|---|---|---|---|
| Python | 배포 | 45 | 42 | 1 (certifi, 예외 등록) | 2 (pyinstaller 계열, 예외 등록) | 0 |
| web-ui (npm) | 배포 | 105 | 105 | 0 | 0 | 0 |
| cf-worker (npm) | 배포 | 0 | - | - | - | - |
| npm 실행기 | 배포 | 0 (자기 패키지만) | - | - | - | - |
| Python | 개발 | 26 | 23 | 1 (pathspec) | 0 | 2 (로컬 미설치: twine, importlib-metadata) |
| web-ui (npm) | 개발 | 41 | 29 | 12 (lightningcss) | 0 | 0 |
| cf-worker (npm) | 개발 | 168 | 142 | 26 (lightningcss, sharp-libvips) | 0 | 0 |

배포물에서 실패한 항목은 없다. 위 표의 예외 3개는 모두 5단계에서 이유를 적고 등록했다.

## 정책 파일에 등록된 예외

`scripts/license_policy.json`에서 관리한다. 모든 항목에 `reason`이 있어야 한다(테스트가 확인).

- `allow` (배포물 예외): certifi, pyinstaller, pyinstaller-hooks-contrib. 이유는 5단계 표와 같다.
- `overrides` (메타데이터가 부정확해서 사람이 원문을 보고 고친 값):
  - colorama, jinja2, prompt-toolkit: 메타데이터에는 "BSD"라고만 적혀 있다. 함께 설치된
    LICENSE 파일을 열어 보니 3조항 BSD(BSD-3-Clause)다.
  - nvidia-ml-py: 메타데이터에 "BSD"라고만 있고 라이선스 파일이 없다. 소스 머리말의 문구가
    BSD-3-Clause다.
  - scipy: `License` 칸에 라이선스 전문이 통째로 들어 있다. 내용은 BSD-3-Clause다.
  - pyinstaller, pyinstaller-hooks-contrib: 위 5단계 참고.
  - tomli, exceptiongroup (Python 3.11 미만 전용), pefile, pywin32-ctypes (Windows 전용),
    macholib (macOS 전용): 일부 환경에서만 설치되므로, 어떤 컴퓨터에서 돌려도 결과가 같도록
    원래 라이선스(MIT/BSD-3-Clause)를 적어 둔다.

## omm이 실행만 하는 외부 프로그램

omm은 로컬 LLM 실행 앱을 **설치하도록 돕거나 찾아서 실행**하지만, 그 코드를 omm 안에 넣거나
함께 묶어 배포하지 않는다. 목록은 `src/omm/engine_packages.py`와 `src/omm/linker.py`,
`src/omm/launcher.py`에서 확인했다. 라이선스는 각 공식 저장소의 LICENSE 파일(GitHub API
`repos/<owner>/<repo>/license`)과 공식 약관 페이지로 2026-10-24에 확인했다.

| 프로그램 | 라이선스 | 확인한 곳 | omm과의 관계 |
|---|---|---|---|
| Ollama | MIT | github.com/ollama/ollama | Homebrew·winget·공식 설치 파일로 설치, 로컬 HTTP API와 CLI로 대화 |
| llama.cpp | MIT | github.com/ggml-org/llama.cpp | Ollama 등의 내부 엔진. omm은 GGUF 파일 형식만 읽음 |
| LM Studio | 독점(비공개) 소프트웨어 | lmstudio.ai/app-terms | Homebrew·winget으로 설치, 공개된 `lms` CLI(MIT)와 로컬 API로 대화 |
| Jan | Apache-2.0 | github.com/janhq/jan | Homebrew·winget·Flatpak으로 설치, 모델 폴더 설정만 씀 |
| AnythingLLM | MIT | github.com/Mintplex-Labs/anything-llm | Homebrew로 설치, 저장 폴더에 모델 파일 연결 |
| Msty Studio | 독점 소프트웨어 | msty.ai/terms | Homebrew로 설치, 저장 폴더에 모델 파일 연결 |
| KoboldCpp | AGPL-3.0 | github.com/LostRuins/koboldcpp | 사용자가 직접 설치, omm이 별도 프로세스로 실행 |
| text-generation-webui | AGPL-3.0 | github.com/oobabooga/text-generation-webui | 사용자가 직접 설치, 모델 연결 후 그 앱의 시작 스크립트를 별도 프로세스로 실행 |

참고: exo(github.com/exo-explore/exo)는 GPL로 알려진 경우가 있지만 실제 저장소 라이선스는
**Apache-2.0**이다. 그리고 현재 omm 코드에는 exo 연동이 없다.

### 별도 프로세스로 실행하면 왜 카피레프트가 omm에 옮지 않나

- GPL·AGPL의 공개 의무는 그 코드를 **고치거나, 내 프로그램과 한 덩어리로 합쳐서(링크·번들)
  배포할 때** 생긴다. 자유소프트웨어재단의 GPL FAQ도 "별개 프로그램이 명령줄·파이프·소켓으로
  주고받는 것은 보통 별개의 작품"이라고 설명한다.
- omm은 KoboldCpp나 text-generation-webui의 코드를 가져오지도, 고치지도, 함께 배포하지도 않는다.
  사용자가 공식 저장소에서 직접 받은 프로그램을 **따로 실행**하고, 모델 파일을 연결하고, 로컬 HTTP로
  요청을 보낼 뿐이다.
- AGPL의 추가 조항(13조)은 "**고친** AGPL 프로그램을 네트워크 서비스로 제공하면 소스를 공개하라"는
  것이다. omm은 고치지 않았고 남에게 서비스로 제공하지도 않으므로 해당하지 않는다.
- Ollama·LM Studio·Jan 등도 패키지 관리자나 공식 설치 파일이 **원래 배포처에서 직접** 받아
  설치한다. omm이 그 바이너리를 다시 배포(미러링)하지 않으므로 omm은 "배포자"가 아니다.
- LM Studio·Msty는 오픈소스가 아니다. 약관이 재배포와 역공학을 막지만, omm은 재배포하지 않고
  공개된 연동 수단(CLI·API·모델 폴더)만 쓴다.

### 필요한 고지

omm이 이 프로그램들을 배포하지 않으므로 그 라이선스 전문을 실을 의무는 없다. 대신 사용자가 헷갈리지
않도록 `THIRD_PARTY_NOTICES.md` 마지막 절과 README의 License 절에 "이 앱들은 각자의 라이선스와
약관을 따른다"고 적어 두었다.

**주의**: 나중에 omm이 이 프로그램의 바이너리를 직접 내려받아 omm 쪽 서버에서 다시 나눠 주거나,
omm 패키지 안에 넣게 되면 그때는 omm이 배포자가 된다. 그러면 라이선스 전문을 함께 넣어야 하고,
AGPL 프로그램이라면 해당 소스를 받을 수 있는 방법도 제공해야 한다. 그런 변경을 할 때는 이 문서를
다시 검토한다.

## 모델 가중치 라이선스

모델 파일(Qwen, Llama, Gemma, EXAONE 등)의 라이선스는 위 소프트웨어 라이선스와 **완전히 별개**다.
omm은 모델 파일을 Hugging Face 등 원래 배포처에서 사용자 컴퓨터로 직접 내려받게 하고, 다시
배포하지 않는다. 대신 모델마다 라이선스를 보여 준다(새로 만든 기능 아님, 기존 기능 확인).

- `omm info`: 모델 저장소가 알려 준 라이선스를 `License` 줄에 보여 준다
  (`src/omm/providers/huggingface.py`가 모델 카드의 `license`나 `license:` 태그에서 읽음).
- 로컬 웹 화면: 모델 위키(`src/omm/model-wiki.json`, 114개 모델)의 라이선스 이름과 원문 링크를
  "라이선스 표기"로 보여 준다. 그중 17개는 아직 "미확인"이다.
- 품질 측정 기록: GGUF 파일 안의 `general.license` 값을 함께 저장한다(`src/omm/quality.py`).

Llama 커뮤니티 라이선스처럼 사용 조건이 있는 모델이나, EXAONE "NC"처럼 상업적 사용을 막는
모델도 있다. 이 조건을 지키는 것은 모델을 쓰는 사용자의 몫이고, omm은 라이선스를 보여 주는
역할을 한다.
