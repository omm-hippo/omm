#!/bin/sh
# OMM double-click installer for macOS (#423).
# Downloads the same install.sh that `curl ... | sh` uses, runs it, and asks it
# to open the local web manager when it finishes. Nothing here enables login
# autostart; that stays an explicit `omm setting web-autostart enable`.
set -u

pause_before_close() {
    printf '%s' 'Enter를 누르면 창이 닫힙니다 (Press Enter to close): '
    read -r answer || true
}

printf '%s\n' \
    '============================================================' \
    ' OMM 설치를 시작합니다. 끝날 때까지 이 창을 닫지 마세요.' \
    ' Installing OMM. Please do not close this window.' \
    '============================================================' \
    ' Python이나 git이 없으면 Homebrew로 먼저 설치합니다. 몇 분 걸릴 수 있고 Mac 암호를 물을 수 있습니다.' \
    ' If Python or git is missing, Homebrew installs them first. This can take a few minutes.' \
    ''

downloaded=""
# Ctrl+C (for example to stop the web manager) still removes the temp copy.
trap '[ -n "$downloaded" ] && rm -f "$downloaded"; exit 130' INT TERM
script="${OMM_INSTALL_SCRIPT:-}"
if [ -z "$script" ]; then
    downloaded=$(mktemp "${TMPDIR:-/tmp}/omm-install.XXXXXX") || downloaded=""
    script="$downloaded"
    printf '%s\n' ' 설치 스크립트를 내려받는 중... (Downloading install.sh)'
    if [ -z "$downloaded" ] || ! curl -fsSL https://raw.githubusercontent.com/omm-hippo/omm/main/install.sh -o "$downloaded"; then
        [ -n "$downloaded" ] && rm -f "$downloaded"
        printf '%s\n' '' \
            ' [실패] 설치 스크립트를 내려받지 못했습니다. 인터넷 연결(회사/학교 프록시 포함)을 확인한 뒤 이 파일을 다시 여세요.' \
            ' [FAILED] Could not download install.sh. Check the internet connection, then open this file again.' \
            ' 오류 문장별 해결 방법: https://github.com/omm-hippo/omm/blob/main/docs/beginner-install.md'
        pause_before_close
        exit 1
    fi
fi

status=0
OMM_INSTALL_OPEN_WEB=1 sh "$script" || status=$?
[ -n "$downloaded" ] && rm -f "$downloaded"

if [ "$status" -ne 0 ]; then
    printf '%s\n' '' \
        " [실패] OMM 설치가 끝나지 않았습니다(오류 코드 $status). 위쪽의 영어 오류 문장을 확인하세요." \
        " [FAILED] OMM was not installed (exit code $status). See the messages above." \
        ' 받아 둔 모델과 설정은 지우지 않았습니다. 원인을 고친 뒤 이 파일을 다시 열면 됩니다.' \
        ' Downloaded models and settings were kept. Fix the cause, then open this file again.' \
        ' 오류 문장별 해결 방법: https://github.com/omm-hippo/omm/blob/main/docs/beginner-install.md'
    pause_before_close
    exit "$status"
fi

printf '%s\n' '' \
    ' OMM 설치가 끝났습니다. (OMM is installed.)' \
    ' 관리 화면을 다시 열려면: 새 터미널 창에서 omm web --open' \
    ' 바탕화면에 더블클릭 파일 만들기: omm web --create-launchers "$HOME/Desktop"' \
    ' 로그인할 때 자동으로 켜기(기본은 꺼짐): omm setting web-autostart enable' \
    ''
pause_before_close
exit 0
