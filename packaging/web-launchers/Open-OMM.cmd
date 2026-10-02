@echo off
setlocal DisableDelayedExpansion
if exist "%~dp0omm.exe" (
  "%~dp0omm.exe" web --open
) else (
  omm web --open
)
if errorlevel 1 (
  echo Install OMM first: https://github.com/omm-hippo/omm#installation
  pause
)
