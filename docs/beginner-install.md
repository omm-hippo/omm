# 처음 설치하는 분을 위한 OMM 설치 안내

명령줄(터미널)을 처음 써 보는 분을 위한 안내입니다. 명령어는 **그대로 복사해서
붙여넣기**만 하면 됩니다. 영어 오류가 나오면 아래 [오류 메시지별 해결](#오류-메시지별-해결)에서
같은 문장을 찾아보세요.

> 이 문서의 오류 문장은 실제 설치 스크립트(`install.ps1`, `install.sh`)에 들어 있는 문장만
> 옮겼습니다. 각 문장 옆 주석에 파일 위치(파일:줄)를 적었습니다. 줄 번호는 이 문서를 쓴
> 시점 기준이라 이후 스크립트가 바뀌면 조금 달라질 수 있습니다.

## 0. 가장 쉬운 방법: 파일 하나 내려받아 더블클릭

명령어를 붙여넣는 것도 어렵다면 이 방법을 먼저 쓰세요. 아래 파일이 1번의 설치 명령을 대신
실행하고, 설치가 끝나면 **브라우저에 OMM 관리 화면을 자동으로 엽니다.** 관리 화면에서 모델을
골라 설치하면 됩니다.

### Windows: `Install-OMM.cmd`

1. [Install-OMM.cmd 페이지](https://github.com/omm-hippo/omm/blob/main/packaging/web-launchers/Install-OMM.cmd)를
   열고 오른쪽 위의 **다운로드 버튼(Download raw file, 아래 화살표 모양)**을 누릅니다.
2. 내려받은 `Install-OMM.cmd`를 더블클릭합니다.
3. 파란 창에 **"Windows의 PC 보호"**가 나오면 **추가 정보 → 실행**을 누릅니다. 인터넷에서 받은
   파일이라 Windows가 한 번 묻는 것입니다.
4. 검은 창이 열리고 "끝날 때까지 이 창을 닫지 마세요"가 나옵니다. Python·git이 없으면 먼저
   설치하느라 몇 분 걸릴 수 있습니다. 중간에 설치 허락 창이 뜨면 **예**를 누르세요.
5. 설치가 끝나면 브라우저에 관리 화면이 열립니다. **관리 화면을 쓰는 동안에는 검은 창을 닫지
   마세요.** 다 쓰면 검은 창에서 `Ctrl+C`를 누르거나 창을 닫으면 관리 화면이 멈춥니다.

이 파일은 이 실행 한 번에만 PowerShell 실행 정책을 건너뜁니다(`-ExecutionPolicy Bypass`).
컴퓨터의 보안 설정은 바꾸지 않습니다.

### macOS: `Install-OMM.command`

1. [Install-OMM.command 페이지](https://github.com/omm-hippo/omm/blob/main/packaging/web-launchers/Install-OMM.command)에서
   다운로드 버튼을 눌러 내려받습니다.
2. 브라우저로 내려받은 파일에는 실행 권한이 없고 macOS 격리 표시(Gatekeeper)가 붙어서 그냥
   더블클릭하면 열리지 않습니다. **터미널**(응용 프로그램 → 유틸리티 → 터미널)을 열고 아래를 한 번
   붙여넣으세요.

```sh
sh ~/Downloads/Install-OMM.command
```

   그다음 과정(창을 닫지 말 것, 끝나면 브라우저가 열림)은 Windows와 같습니다. macOS에서 진짜
   더블클릭이 되려면 실행 권한을 보존하는 압축 파일(zip)로 배포해야 하는데, 이 배포는 아직
   준비되지 않았습니다(미검증). 터미널이 익숙하다면 1번의 `curl` 명령과 결과가 같습니다.

### 실패했을 때, 다시 할 때, 지울 때

- 실패하면 창이 닫히지 않고 `[실패]`로 시작하는 한국어 안내가 남습니다. 그 위쪽의 영어 문장을
  아래 [오류 메시지별 해결](#오류-메시지별-해결) 표에서 찾으세요.
- 원인을 고친 뒤 **같은 파일을 다시 더블클릭**하면 됩니다. 다시 설치해도 받아 둔 모델과 설정은
  지우지 않습니다(5번 참고).
- 로그인할 때 관리 화면이 자동으로 켜지는 기능은 **기본으로 꺼져 있고**, 이 파일도 켜지 않습니다.
  원할 때만 3번의 `omm setting web-autostart enable`로 켜세요.
- 지우는 방법은 7번과 같습니다.

## 1. 무엇을 열고 무엇을 붙여넣나

### Windows

1. 시작 메뉴에서 **Windows PowerShell**을 엽니다. (명령 프롬프트 `cmd`나 Git Bash가 아닙니다.)
2. 아래 두 줄을 한꺼번에 복사해서 붙여넣고 Enter를 누릅니다.

```powershell
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; irm https://omm.run/install.ps1 | iex
```

- Python이나 git이 없으면 설치 스크립트가 winget으로 먼저 설치를 시도합니다.
  Windows 10 2004 이전처럼 winget이 없는 PC라면 [Python 3.10 이상](https://www.python.org/downloads/)과
  [git](https://git-scm.com/downloads)을 직접 설치한 뒤 다시 실행하세요.
- Microsoft Store에서 받은 Python은 쓸 수 없습니다. python.org 설치판을 쓰세요.

### macOS

1. **응용 프로그램 → 유틸리티 → 터미널**을 엽니다.
2. 아래 한 줄을 붙여넣고 Enter를 누릅니다.

```sh
curl -fsSL https://omm.run/install.sh | sh
```

- Python 3.10 이상이나 git이 없으면 Homebrew로 설치합니다. Homebrew가 없으면 Homebrew
  공식 설치기를 먼저 실행합니다.

### Linux

macOS와 같은 `curl -fsSL https://omm.run/install.sh | sh` 명령을 터미널에서 실행합니다.
`apt-get`, `dnf`, `yum`, `pacman`, `apk` 중 하나가 있으면 Python·git을 자동으로 설치하며,
이때 `sudo` 비밀번호를 물을 수 있습니다.

## 2. 설치가 끝나면: 새 창을 여세요

설치가 끝나면 마지막에 이런 문장이 나옵니다.

- Windows: `Done. If 'omm' isn't found, open a new PowerShell window (pipx just updated your PATH).`
  <!-- install.ps1:901 -->
- macOS/Linux: `Done. Open a new shell so zsh loads the pipx PATH configured by the installer.`
  <!-- install.sh:921 -->

**지금 열려 있는 창을 닫고 새 터미널(PowerShell) 창을 여세요.** 설치 직후의 창은 `omm`
명령 위치(PATH)를 아직 모릅니다. 새 창에서 아래를 실행합니다.

```sh
omm          # 처음 실행: 하드웨어 확인 + 실행기(Ollama, LM Studio 등) 고르기
omm scan     # 메모리, 실행기, 모델 요약
```

## 3. 웹 관리 화면 열기

명령어 대신 브라우저 화면으로 모델을 찾고 설치·삭제할 수 있습니다.

```sh
omm web --open
```

- 브라우저에 이 컴퓨터 안에서만 열리는 주소(`http://127.0.0.1:...`)가 뜹니다.
- 창(터미널)을 닫거나 `Ctrl+C`를 누르면 관리 화면이 멈춥니다.
- 매번 명령어를 치기 싫다면 바탕화면에 더블클릭용 파일을 만드세요.

```powershell
omm web --create-launchers "$HOME\Desktop"     # Windows PowerShell
```

```sh
omm web --create-launchers "$HOME/Desktop"     # macOS / Linux
```

  Windows는 `Open-OMM.cmd`, macOS는 `Open-OMM.command`를 더블클릭하면 관리 화면이 열립니다.
  같은 이름의 다른 파일이 이미 있으면 덮어쓰지 않고 멈춥니다.
- 로그인할 때마다 관리 화면을 자동으로 켜고 싶다면(기본은 꺼짐):

```sh
omm setting web-autostart enable    # 켜기 (브라우저는 열지 않음)
omm setting web-autostart status    # 상태 보기
omm setting web-autostart disable   # 끄기
```

## 4. 실행기(Ollama, LM Studio 등)가 없을 때

OMM이 자동 설치·업데이트하는 실행기에는 필수 안전 검사가 적용됩니다. 공식 패키지
출처와 무결성 보호를 먼저 확인하고, 설치 뒤 OS 서명·보안 검사까지 통과해야 성공으로
표시합니다. 검사 도구가 없거나 실패하면 성공으로 처리하지 않습니다. 설치 후 검사 실패로
파일이 남을 수 있으므로 실행하지 말고 원인을 확인하세요. 이 검사는 모든 취약점이 없다는
보증은 아닙니다. 플랫폼별 범위는 [필수 설치 안전 검사](mandatory-install-safety.md)를 보세요.

OMM은 모델 파일을 관리하고, 실제 대화는 실행기 프로그램이 합니다. 처음 `omm`을 실행하면
설치할 실행기를 고르는 목록이 나옵니다. 나중에 다시 하려면:

```sh
omm setup                 # 처음 설정 마법사를 다시 실행
omm engine install        # 실행기 하나를 골라 설치
omm engine status         # 앱 설치 여부와 로컬 API 응답을 따로 보여줌
```

Windows에서 AnythingLLM과 Msty는 자동 설치를 지원하지 않아 안내 문구만 나옵니다. 직접 설치하세요.

## 5. 다시 설치·고치기

설치가 중간에 실패했거나 `omm`이 이상하게 동작하면 **같은 설치 명령을 다시 실행**하면
됩니다. 다시 설치해도 받아 둔 모델과 설정(아래 6번 위치)은 지우지 않습니다. 설치 스크립트가
정리하는 것은 OMM 프로그램 소스(`OMM_HOME/sources`)와 예전 방식 소스 폴더(`OMM_HOME/src`)뿐입니다.

설치 후 문제를 찾을 때는 아래 명령이 읽기만 하고 다음에 할 일을 알려 줍니다.

```sh
omm doctor
```

## 6. 모델은 어디에 저장되나

- 기본 위치: 사용자 폴더 아래 `.omm` (Windows `C:\Users\<이름>\.omm`, macOS `/Users/<이름>/.omm`,
  Linux `/home/<이름>/.omm`). 모델 파일은 그 안의 `models` 폴더에 있습니다.
- 다른 드라이브에 두려면 **설치 전에** `OMM_HOME`을 정하고, 이후에도 같은 값으로 씁니다.
  Windows 예:

```powershell
[Environment]::SetEnvironmentVariable("OMM_HOME", "D:\omm", "User")
$env:OMM_HOME = "D:\omm"
```

## 7. 지우기 (모델은 남기기)

아래 명령은 OMM 프로그램만 지우고 **받은 모델과 설정은 남깁니다.**

```powershell
irm https://omm.run/uninstall.ps1 | iex          # Windows
```

```sh
curl -fsSL https://omm.run/uninstall.sh | sh     # macOS / Linux
```

모델과 설정까지 모두 지우려면 스크립트를 내려받아 Windows는 `-Purge`, macOS/Linux는 `--purge`를
붙여 실행합니다. 이 옵션은 되돌릴 수 없으니 정말 필요할 때만 쓰세요.
웹 관리 화면 자동 시작을 켜 두었다면 지우기 전에 `omm setting web-autostart disable`을 먼저
실행하세요.

## 오류 메시지별 해결

화면에 보이는 영어 문장으로 찾으세요.

| 화면에 보이는 문장 | 뜻 | 할 일 |
|---|---|---|
| `Windows detected. Run the native PowerShell installer instead:` <!-- install.sh:60 --> | Windows에서 macOS/Linux용 명령을 실행함 | 위 Windows 명령을 PowerShell에서 실행 |
| `No usable Python 3.10+ found (the Microsoft Store Python cannot host pipx environments).` <!-- install.ps1:261 --> | 쓸 수 있는 Python이 없어 winget으로 설치를 시도함 | 기다리면 됨. 다음 줄에 실패가 나오면 아래 줄 참고 |
| `Python 3.10+ not found. Install it from https://www.python.org/downloads/ ...` <!-- install.ps1:267 --> | Python 자동 설치 실패 | python.org에서 Python 설치 → **새 창**에서 설치 명령 다시 실행 |
| `Python 3.10+ not found and no supported package manager was found.` <!-- install.sh:307 --> 또는 `Python 3.10+ not found after dependency bootstrap` <!-- install.sh:352 --> | Linux/macOS에서 Python 자동 설치 실패 | Python 3.10 이상을 직접 설치 → 새 창에서 다시 실행 |
| `git not found. Install git first (needed to fetch omm from GitHub): https://git-scm.com/downloads` <!-- install.ps1:280, install.ps1:285 --> | git 자동 설치 실패 (Windows) | git 설치 → 새 창에서 다시 실행 |
| `git not found and no supported package manager was found.` <!-- install.sh:323 --> 또는 `git not found after dependency bootstrap` <!-- install.sh:357 --> | git 자동 설치 실패 (macOS/Linux) | git 설치 → 새 창에서 다시 실행 |
| `git 2.34+ is required to verify SSH commit signatures` <!-- install.ps1:132, install.sh:117 --> | git이 너무 오래됨 | git을 최신으로 업데이트 후 다시 실행 |
| `Homebrew is required on macOS when Python 3.10+ or git is missing.` <!-- install.sh:243 --> | Homebrew 자동 설치가 꺼져 있음 | [brew.sh](https://brew.sh/)에서 Homebrew 설치 후 다시 실행 |
| `Cannot install system dependencies: root or sudo is required.` <!-- install.sh:148 --> | Linux에서 패키지 설치 권한이 없음 | 관리자에게 Python·git 설치를 부탁하거나 `sudo` 권한으로 다시 실행 |
| `Python ensurepip/venv support is missing; install the Python venv package first.` <!-- install.sh:382 --> | Python 가상환경 패키지가 없음 | 배포판의 `python3-venv` 등 설치 후 다시 실행 |
| `git clone failed or stalled (no progress for 60s)` <!-- install.ps1:702, install.sh:729 --> | 인터넷 연결 문제로 다운로드가 멈춤 | 네트워크(회사·학교 프록시 포함) 확인 후 다시 실행 |
| `Signature verification failed - refusing to install untrusted code.` <!-- install.ps1:730, install.sh:756 --> | 받은 코드의 서명이 맞지 않아 설치를 거부함 | 우회하지 말고 [이슈](https://github.com/omm-hippo/omm/issues)로 알려 주세요 |
| `pipx reports the '...' environment as broken (see 'pipx list').` <!-- install.ps1:665, install.sh:690 --> | 이전 설치 흔적(pipx 환경)이 깨짐 | `pipx list`로 확인 후 해당 환경을 지우고 다시 실행. 모델은 영향 없음 |
| `omm installer failed; see the messages above.` <!-- install.ps1:15 --> | 위쪽에 실제 원인이 따로 출력됨 | 바로 위 빨간 문장을 이 표에서 찾기 |
| `omm` 명령을 찾을 수 없음 (`'omm' is not recognized...` / `command not found`) | 설치한 창이 아직 PATH를 모름 | 창을 닫고 새 창에서 다시 시도 |

더 많은 경우는 README의 [Troubleshooting](../README.md#troubleshooting) 표에 있습니다.

## 확인 범위

이 문서는 설치 스크립트의 실제 문구와 README 설치 절을 바탕으로 썼습니다. 깨끗한 Windows·macOS
실제 기기에서 이 문서만 보고 처음부터 설치해 본 기록은 아직 없습니다(미검증).
더블클릭 파일(`Install-OMM.cmd`, `Install-OMM.command`)은 실제 설치 스크립트 대신 가짜 스크립트로
성공·실패 안내만 자동 테스트했습니다. 실제 설치 → 관리 화면까지 이어지는 흐름은 미검증입니다.
