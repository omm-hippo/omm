#!/usr/bin/env sh
# Remove the omm CLI and installer-managed source checkouts.
# Models and settings are preserved unless --purge is passed.
set -eu

OMM_HOME="${OMM_HOME:-$HOME/.omm}"
DEFAULT_OMM_HOME="$HOME/.omm"
PURGE=0
case "${1:-}" in
    "") ;;
    --purge) PURGE=1 ;;
    *) echo "Usage: uninstall.sh [--purge]" >&2; exit 2 ;;
esac

case "$OMM_HOME" in
    /*) ;;
    *) echo "Refusing non-absolute OMM_HOME: $OMM_HOME" >&2; exit 1 ;;
esac

if [ -d "$OMM_HOME" ]; then
    RESOLVED_HOME=$(cd -P -- "$OMM_HOME" && pwd -P)
else
    RESOLVED_HOME="$OMM_HOME"
fi
if [ -d "$DEFAULT_OMM_HOME" ]; then
    RESOLVED_DEFAULT=$(cd -P -- "$DEFAULT_OMM_HOME" && pwd -P)
else
    RESOLVED_DEFAULT="$DEFAULT_OMM_HOME"
fi
CURRENT_DIR=$(pwd -P)

case "$RESOLVED_HOME" in
    ""|/|"$HOME"|"$HOME"/) echo "Refusing unsafe OMM_HOME: $RESOLVED_HOME" >&2; exit 1 ;;
esac
case "$CURRENT_DIR" in
    "$RESOLVED_HOME"|"$RESOLVED_HOME"/*)
        echo "Refusing to uninstall while the current directory is inside OMM_HOME: $RESOLVED_HOME" >&2
        exit 1
        ;;
esac
if [ -d "$RESOLVED_HOME" ] && [ "$RESOLVED_HOME" != "$RESOLVED_DEFAULT" ] && [ ! -f "$RESOLVED_HOME/.omm-managed" ]; then
    echo "Refusing unrecognized custom OMM_HOME (missing .omm-managed): $RESOLVED_HOME" >&2
    exit 1
fi

if command -v python3 >/dev/null 2>&1 && python3 -m pipx --version >/dev/null 2>&1; then
    python3 -m pipx uninstall omm || true
elif command -v python >/dev/null 2>&1 && python -m pipx --version >/dev/null 2>&1; then
    python -m pipx uninstall omm || true
elif command -v pipx >/dev/null 2>&1; then
    pipx uninstall omm || true
else
    echo "pipx was not found; removing installer-managed files only." >&2
fi

rm -rf "$RESOLVED_HOME/src" "$RESOLVED_HOME/sources"

if [ "$PURGE" = "1" ]; then
    # Delete only paths the application owns. Never recursively remove the
    # OMM_HOME container itself: a custom home may contain unrelated files.
    for owned_dir in models evaluations catalog-history session; do
        rm -rf "$RESOLVED_HOME/$owned_dir"
    done
    for owned_file in \
        config.json models.json link-ownership.json rules.json \
        recommend-model.json calibration.json benchmark_history.json \
        contribute_state.json telemetry.log telemetry_pending.json \
        update_check.json .omm-managed; do
        rm -f "$RESOLVED_HOME/$owned_file" "$RESOLVED_HOME/$owned_file.lock"
    done
    # Corrupt backups and interrupted atomic writes use these application-
    # owned suffixes. Limit cleanup to the known JSON filenames above.
    for owned_json in config.json models.json link-ownership.json rules.json recommend-model.json calibration.json benchmark_history.json contribute_state.json telemetry_pending.json update_check.json; do
        rm -f "$RESOLVED_HOME/$owned_json".corrupt-* "$RESOLVED_HOME/.$owned_json".*.tmp
    done
    rmdir "$RESOLVED_HOME" 2>/dev/null || true
    echo "Removed omm models, settings, and cached data. Unrelated files in $RESOLVED_HOME were preserved."
else
    echo "Removed omm. Models and settings remain in $RESOLVED_HOME (use --purge to remove them)."
fi
