@echo off
setlocal DisableDelayedExpansion
if exist "%~dp0omm.exe" (
  "%~dp0omm.exe" web --open
) else (
  omm web --open
)
if not errorlevel 1 exit /b 0
rem Korean text below is UTF-8. chcp runs on its own line, outside any
rem parenthesized block, so cmd decodes the following lines as UTF-8.
chcp 65001 >nul
echo OMM 관리 화면을 시작하지 못했습니다. OMM이 설치되어 있지 않거나 새 창에서 아직 PATH가 반영되지 않았을 수 있습니다.
echo 설치 후 이 파일을 다시 열어 주세요. 설치를 막 끝냈다면 새 PowerShell 창에서 omm web --open 을 실행해 보세요.
echo 처음 설치 안내: https://github.com/omm-hippo/omm/blob/main/docs/beginner-install.md
echo Install OMM first: https://github.com/omm-hippo/omm#installation
pause
exit /b 1
