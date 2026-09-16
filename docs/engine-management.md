# 엔진 관리와 짧은 명령 안내

`omm engine status [ENGINE]`는 프로그램 설치, 식별한 패키지와 버전, 로컬 API
상태를 구분한다. GUI가 설치된 것과 API 서버가 켜진 것은 다르다. API 조회는
Ollama·LM Studio만 지원하며 다른 엔진은 미지원으로 표시한다. `--no-api`는
프로그램과 패키지만 읽는다. `engine doctor`는 다음 행동도 안내한다. 이 두
진단 명령은 서버를 시작하거나 OMM 로그/사용 통계/설정을 생성하지 않는다.

```text
omm engine status --json
omm engine doctor ollama
omm engine security
omm engine security ollama --fix-local-only
omm engine update ollama --dry-run
omm engine update ollama --yes
omm engine uninstall lmstudio --dry-run --json
```

변경은 현재 설치가 확인된 Homebrew cask(또는 Ollama formula) / WinGet의 정확한 패키지 ID / Flatpak
설치 범위로만 수행한다. 직접 설치나 출처 불명확한 앱은 경로를 추정해 삭제하지
않고 수동 안내를 준다. WinGet은 수동 설치 앱도 공식 ID로 식별할 수 있으므로,
식별 결과가 과거에 WinGet으로 설치했다는 증거라는 뜻은 아니다. Flatpak에 같은
앱이 여러 범위로 설치돼 있으면 자동 선택하지 않는다.

`--dry-run`은 명령 미리보기다. 실제 변경에는 대화형 확인 또는 `--yes`가
필요하고, JSON 모드에서는 프롬프트를 띄우지 않는다. 동의 후에도 패키지가
바뀌지 않았는지 재확인한다. 설치/업데이트/제거는 같은 엔진 작업 잠금을 쓴다.
실행 후 패키지를 다시 조회해 확인하지 못하면 완료라고 표시하지 않는다.
엔진 프로그램이 감지되는 것만으로 실제 추론을 검증했다고 하지 않는다.

`engine security`는 Ollama·LM Studio의 실제 TCP 리스너와 프로세스 정체를
함께 확인해 `이 컴퓨터만`, `외부 연결 허용`, `확인 불가`를 구분한다. 포트가
보인다는 사실만으로 해당 엔진이라고 추정하지 않는다. `--fix-local-only`는
PID·시작 시각·실행 파일·명령을 다시 확인해 OMM이 시작한 Ollama라고 증명되는
경우에만 쓸 수 있다. 먼저 기존 로컬 클라이언트 연결과 메모리 작업이 끊기고
서버가 재시작된다는 영향을 보여준 뒤 동의를 받는다. LM Studio와 다른 앱 또는
사용자가 시작한 서버는 상태만 보여주고 설정하거나 재시작하지 않는다.

OMM 모델 삭제, `--zap`, `--purge`, 전체 패키지 업데이트는 실행하지 않는다.
엔진 자체의 앱 데이터 처리는 해당 패키지 관리자의 정책을 따른다. 엔진의
설치 출처와 패키지 상태를 알 수 없으면 자동 변경을 중단한다.

설치 진행에는 다운로드/설치/확인, 필요한 경우 손상된 앱 재설치 단계를 표시한다.
네이티브 설치 도구의 경과 시간을 보여주되 남은 시간이나 가짜 진행률은 만들지
않는다. 이 변경 자체가 설치 속도를 두 배 빠르게 만든다는 주장은 하지 않는다.

명령 사용 오류는 짧은 원인과 해당 `--help` 경로를 표시한다. 대화형 터미널에서는
명령별 첫 오류에 짧은 설명과 사용법을 추가하고 같은 터미널의 반복 안내를 줄인다.
파이프에는 세션 안내를 저장하지 않으며, JSON을 요청한 인자 오류는 구조화된
오류 문서와 종료 코드 2를 반환한다.

## 근거

- GitHub 토의: #336, #339, #332. #336에는 아직 댓글 합의가 없다.
- [Homebrew 명령](https://docs.brew.sh/Manpage)
- [WinGet list](https://learn.microsoft.com/en-us/windows/package-manager/winget/list),
  [upgrade](https://learn.microsoft.com/en-us/windows/package-manager/winget/upgrade),
  [uninstall](https://learn.microsoft.com/en-us/windows/package-manager/winget/uninstall)
- [WinGet 오류 코드](https://github.com/microsoft/winget-cli/blob/master/src/AppInstallerSharedLib/Public/AppInstallerErrors.h)
- [Flatpak 명령](https://docs.flatpak.org/en/latest/flatpak-command-reference.html)

## 검증 범위

패키지 변경은 명령/결과 대역과 임시 파일로 계약 검증한다. 이 Mac의 실제
읽기 전용 상태 조회에서는 Ollama API 가용 여부와 LM Studio API 미가용을
구분했고 임시 OMM_HOME이 생성되지 않음을 확인했다. 사용자 앱을 실제로
업데이트/삭제하는 검사는 하지 않았으며, Windows/Linux 실물은 미검증이다.
