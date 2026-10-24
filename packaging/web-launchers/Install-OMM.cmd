@echo off
rem OMM double-click installer for Windows (#423).
rem Downloads the same install.ps1 that `irm ... | iex` uses, runs it with
rem ExecutionPolicy Bypass for this one process only (no machine or user policy
rem change), and asks it to open the local web manager when it finishes.
rem This file is stored with CRLF line endings on purpose (see .gitattributes):
rem cmd.exe mis-parses labels in LF-only batch files.
setlocal DisableDelayedExpansion
rem Korean text below is UTF-8. chcp runs on its own top-level line so cmd
rem decodes every following line as UTF-8.
chcp 65001 >nul
title OMM 설치 (OMM installer)
echo ============================================================
echo  OMM 설치를 시작합니다. 끝날 때까지 이 창을 닫지 마세요.
echo  Installing OMM. Please do not close this window.
echo ============================================================
echo  Python, git, pipx가 없으면 자동으로 설치합니다. 몇 분 걸릴 수 있습니다.
echo  If Python, git or pipx is missing, they are installed first. This can take a few minutes.
echo.
set "OMM_INSTALL_OPEN_WEB=1"
if defined OMM_INSTALL_SCRIPT goto run_installer
set "OMM_INSTALL_SCRIPT=%TEMP%\omm-install-%RANDOM%%RANDOM%.ps1"
set "OMM_INSTALL_DOWNLOADED=1"
echo  설치 스크립트를 내려받는 중... (Downloading install.ps1)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference = 'Stop'; [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/omm-hippo/omm/main/install.ps1' -OutFile $env:OMM_INSTALL_SCRIPT"
if errorlevel 1 goto download_failed
:run_installer
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%OMM_INSTALL_SCRIPT%"
set "OMM_INSTALL_STATUS=%ERRORLEVEL%"
if defined OMM_INSTALL_DOWNLOADED del /q "%OMM_INSTALL_SCRIPT%" >nul 2>&1
rem -1073741510 is 0xC000013A: the window got Ctrl+C (usually to stop the web manager).
if "%OMM_INSTALL_STATUS%"=="-1073741510" goto interrupted
if not "%OMM_INSTALL_STATUS%"=="0" goto install_failed
echo.
echo  OMM 설치가 끝났습니다. (OMM is installed.)
echo  관리 화면을 다시 열려면: 새 PowerShell 창에서 omm web --open
echo  바탕화면에 더블클릭 파일 만들기: omm web --create-launchers "$HOME\Desktop"
echo  로그인할 때 자동으로 켜기(기본은 꺼짐): omm setting web-autostart enable
echo.
echo  Enter를 누르면 창이 닫힙니다. (Press Enter to close.)
pause >nul
exit /b 0
:interrupted
echo.
echo  Ctrl+C로 중지했습니다. 설치가 이미 끝났다면 새 PowerShell 창에서 omm web --open 으로 관리 화면을 다시 엽니다.
echo  설치 도중에 멈췄다면 이 파일을 다시 더블클릭하세요. 받아 둔 모델과 설정은 그대로 있습니다.
echo  Stopped with Ctrl+C. If the install had finished, run omm web --open in a new PowerShell window; otherwise double-click this file again.
echo.
echo  Enter를 누르면 창이 닫힙니다. (Press Enter to close.)
pause >nul
exit /b 0
:download_failed
if defined OMM_INSTALL_DOWNLOADED del /q "%OMM_INSTALL_SCRIPT%" >nul 2>&1
echo.
echo  [실패] 설치 스크립트를 내려받지 못했습니다. 인터넷 연결(회사/학교 프록시 포함)을 확인한 뒤 이 파일을 다시 더블클릭하세요.
echo  [FAILED] Could not download install.ps1. Check the internet connection, then double-click this file again.
goto failure_help
:install_failed
echo.
echo  [실패] OMM 설치가 끝나지 않았습니다(오류 코드 %OMM_INSTALL_STATUS%). 위쪽의 영어 오류 문장을 확인하세요.
echo  [FAILED] OMM was not installed (exit code %OMM_INSTALL_STATUS%). See the messages above.
echo  받아 둔 모델과 설정은 지우지 않았습니다. 원인을 고친 뒤 이 파일을 다시 더블클릭하면 됩니다.
echo  Downloaded models and settings were kept. Fix the cause, then double-click this file again.
:failure_help
echo  오류 문장별 해결 방법: https://github.com/omm-hippo/omm/blob/main/docs/beginner-install.md
echo.
echo  Enter를 누르면 창이 닫힙니다. (Press Enter to close.)
pause >nul
exit /b 1
