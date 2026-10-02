#!/bin/sh
set -eu
if [ -n "${OMM_EXECUTABLE:-}" ]; then
    exec "$OMM_EXECUTABLE" web --open
fi
if command -v omm >/dev/null 2>&1; then
    exec omm web --open
fi
for candidate in "$HOME/.local/bin/omm" /opt/homebrew/bin/omm /usr/local/bin/omm; do
    if [ -x "$candidate" ]; then
        exec "$candidate" web --open
    fi
done
printf '%s\n' 'OMM을 먼저 설치해 주세요. 설치 후 이 파일을 다시 열면 로컬 관리 화면이 시작됩니다.' '설치 안내: https://github.com/omm-hippo/omm#installation'
printf '%s' 'Enter를 누르면 닫힙니다: '
read -r answer || true
exit 1
