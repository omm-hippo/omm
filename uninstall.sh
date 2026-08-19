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

PIPX_AVAILABLE=0
if command -v python3 >/dev/null 2>&1 && python3 -m pipx --version >/dev/null 2>&1; then
    run_pipx() { python3 -m pipx "$@"; }
    PIPX_AVAILABLE=1
elif command -v python >/dev/null 2>&1 && python -m pipx --version >/dev/null 2>&1; then
    run_pipx() { python -m pipx "$@"; }
    PIPX_AVAILABLE=1
elif command -v pipx >/dev/null 2>&1 && pipx --version >/dev/null 2>&1; then
    run_pipx() { pipx "$@"; }
    PIPX_AVAILABLE=1
fi

CLI_RECOVERY_STATE=unchanged
uninstall_failed() {
    echo "OMM was not fully uninstalled. Source checkouts and user data were preserved." >&2
    if [ "$CLI_RECOVERY_STATE" = "verified" ]; then
        echo "The omm command was repaired and verified after the pipx failure." >&2
    elif [ "$CLI_RECOVERY_STATE" = "uncertain" ]; then
        echo "pipx may have removed the omm command link before failing; command repair could not be verified." >&2
    else
        echo "No pipx uninstall mutation was attempted, so the existing command was left unchanged." >&2
    fi
    echo "Recovery: repair pipx, run 'pipx uninstall omm-model' (and 'pipx uninstall omm' only if it is OMM), then rerun this script." >&2
    exit 1
}

if [ "$PIPX_AVAILABLE" != "1" ]; then
    echo "pipx was not found; refusing to remove source checkouts." >&2
    uninstall_failed
fi

if ! PIPX_LOCAL_VENVS=$(run_pipx environment --value PIPX_LOCAL_VENVS 2>/dev/null); then
    echo "pipx venv location could not be determined; refusing to remove source checkouts." >&2
    uninstall_failed
fi
if ! PIPX_SNAPSHOT=$(run_pipx list --json 2>/dev/null); then
    echo "pipx environments could not be listed; refusing to remove source checkouts." >&2
    uninstall_failed
fi

PIPX_PARSER=""
for parser_candidate in python3 python; do
    if command -v "$parser_candidate" >/dev/null 2>&1; then
        PIPX_PARSER=$(command -v "$parser_candidate")
        break
    fi
done
if [ -z "$PIPX_PARSER" ]; then
    for parser_env in omm-model omm; do
        parser_candidate="$PIPX_LOCAL_VENVS/$parser_env/bin/python"
        if [ -x "$parser_candidate" ]; then
            PIPX_PARSER="$parser_candidate"
            break
        fi
    done
fi
if [ -z "$PIPX_PARSER" ]; then
    echo "Python was not found to validate pipx metadata; refusing to remove source checkouts." >&2
    uninstall_failed
fi

pipx_snapshot_has_environment() {
    printf '%s' "$PIPX_SNAPSHOT" | "$PIPX_PARSER" -c \
        'import json, sys; raise SystemExit(0 if sys.argv[1] in json.load(sys.stdin).get("venvs", {}) else 1)' \
        "$1"
}

pipx_snapshot_environment_is() {
    printf '%s' "$PIPX_SNAPSHOT" | "$PIPX_PARSER" -c '
import json, os, sys
from pathlib import Path

name, distribution, local_venvs = sys.argv[1:]
venv = json.load(sys.stdin).get("venvs", {}).get(name)
if not isinstance(venv, dict):
    raise SystemExit(1)
metadata = venv.get("metadata", {})
main = metadata.get("main_package", {})
expected_dir = Path(local_venvs) / name / ("Scripts" if os.name == "nt" else "bin")
app_paths = main.get("app_paths", [])
omm_paths = [
    Path(item.get("__Path__", "")) for item in app_paths
    if isinstance(item, dict) and Path(item.get("__Path__", "")).name.lower() in {"omm", "omm.exe"}
]
valid = (
    metadata.get("environment") in (None, name)
    and main.get("package") == distribution
    and main.get("suffix") == ""
    and isinstance(main.get("apps"), list)
    and "omm" in main["apps"]
    and len(omm_paths) == 1
    and omm_paths[0].parent.resolve() == expected_dir.resolve()
)
raise SystemExit(0 if valid else 1)
' "$1" "$2" "$PIPX_LOCAL_VENVS"
}

pipx_environment_python() {
    env_python="$PIPX_LOCAL_VENVS/$1/bin/python"
    [ -x "$env_python" ] || return 1
    printf '%s\n' "$env_python"
}

verify_omm_pipx_environment() {
    env_name="$1"
    distribution="$2"
    require_legacy_source="$3"
    env_python=$(pipx_environment_python "$env_name") || return 1
    "$env_python" - "$distribution" "$OMM_HOME" "$require_legacy_source" <<'PY'
import importlib.metadata
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

distribution, omm_home, require_source = sys.argv[1:]
try:
    dist = importlib.metadata.distribution(distribution)
except importlib.metadata.PackageNotFoundError:
    raise SystemExit(1)
entry_points = [
    ep for ep in dist.entry_points
    if ep.group == "console_scripts" and ep.name == "omm"
]
if len(entry_points) != 1 or entry_points[0].value != "omm.cli:main":
    raise SystemExit(1)
if require_source != "1":
    raise SystemExit(0)
raw_direct_url = dist.read_text("direct_url.json")
if not raw_direct_url:
    raise SystemExit(1)
try:
    direct_url = json.loads(raw_direct_url)
except json.JSONDecodeError:
    raise SystemExit(1)

def known_repo(value: str) -> bool:
    value = value.strip()
    if value.startswith("git@github.com:"):
        host, path = "github.com", "/" + value.split(":", 1)[1]
    else:
        parsed_repo = urlparse(value)
        host, path = (parsed_repo.hostname or "").lower(), parsed_repo.path
    normalized_path = "/" + path.strip("/").removesuffix(".git").lower()
    return host == "github.com" and normalized_path in {
        "/omm-hippo/omm",
        "/minigu5/omm",
        "/minigu5/localfit",
    }

url = str(direct_url.get("url", ""))
if direct_url.get("vcs_info") and known_repo(url):
    raise SystemExit(0)
parsed = urlparse(url)
if parsed.scheme != "file":
    raise SystemExit(1)
source = Path(url2pathname(unquote(parsed.path))).resolve()
home = Path(omm_home).resolve()
legacy_source = (home / "src").resolve()
versioned_root = (home / "sources").resolve()
is_versioned_source = (
    source.parent == versioned_root
    and re.fullmatch(r"[0-9a-fA-F]{40}", source.name) is not None
)
if source != legacy_source and not is_versioned_source:
    raise SystemExit(1)
try:
    origin = subprocess.run(
        ["git", "-C", str(source), "remote", "get-url", "origin"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
except (OSError, subprocess.CalledProcessError):
    raise SystemExit(1)
raise SystemExit(0 if known_repo(origin) else 1)
PY
}

verify_exposed_omm_environment() {
    env_name="$1"
    distribution="$2"
    require_legacy_source="$3"
    PIPX_SNAPSHOT=$(run_pipx list --json 2>/dev/null) || return 1
    pipx_snapshot_environment_is "$env_name" "$distribution" || return 1
    verify_omm_pipx_environment "$env_name" "$distribution" "$require_legacy_source" || return 1
    env_python=$(pipx_environment_python "$env_name") || return 1
    internal_app="$PIPX_LOCAL_VENVS/$env_name/bin/omm"
    exposed_app=$(run_pipx environment --value PIPX_BIN_DIR 2>/dev/null)/omm
    [ -x "$internal_app" ] && [ -x "$exposed_app" ] || return 1
    [ "$exposed_app" -ef "$internal_app" ] || return 1
    expected_version=$("$env_python" -c 'import importlib.metadata, sys; print(importlib.metadata.version(sys.argv[1]))' "$distribution" 2>/dev/null) || return 1
    version_output=$("$exposed_app" --version 2>/dev/null) || return 1
    [ "$version_output" = "omm $expected_version" ]
}

repair_after_failed_uninstall() {
    env_name="$1"
    distribution="$2"
    require_legacy_source="$3"
    CLI_RECOVERY_STATE=uncertain
    if run_pipx reinstall "$env_name" >/dev/null 2>&1 && \
       verify_exposed_omm_environment "$env_name" "$distribution" "$require_legacy_source"; then
        CLI_RECOVERY_STATE=verified
    fi
}

REMOVED_ANY=0
REMOVED_NEW=0
REMOVED_LEGACY=0
HAS_NEW=0
LEGACY_IS_OMM=0
if pipx_snapshot_has_environment omm-model; then
    HAS_NEW=1
    if ! pipx_snapshot_environment_is omm-model omm-model || \
       ! verify_omm_pipx_environment omm-model omm-model 0; then
        echo "The omm-model environment could not be verified as OMM; it was preserved." >&2
        uninstall_failed
    fi
fi
if pipx_snapshot_has_environment omm; then
    if pipx_snapshot_environment_is omm omm && \
       verify_omm_pipx_environment omm omm 1; then
        LEGACY_IS_OMM=1
    else
        echo "Preserving unrelated pipx environment 'omm'." >&2
        echo "Resolve the pipx environment-name conflict manually before uninstalling OMM." >&2
        uninstall_failed
    fi
fi

# Remove a verified legacy environment first. If removal fails, its app and
# source remain intact. If it succeeds and removing the new environment then
# fails, the new environment still owns the runnable `omm` command.
if [ "$LEGACY_IS_OMM" = "1" ]; then
    if ! run_pipx uninstall omm; then
        echo "pipx uninstall omm failed." >&2
        repair_after_failed_uninstall omm omm 1
        uninstall_failed
    fi
    REMOVED_ANY=1
    REMOVED_LEGACY=1
fi
if [ "$HAS_NEW" = "1" ]; then
    if ! run_pipx uninstall omm-model; then
        echo "pipx uninstall omm-model failed." >&2
        repair_after_failed_uninstall omm-model omm-model 0
        uninstall_failed
    fi
    REMOVED_ANY=1
    REMOVED_NEW=1
fi
if [ "$REMOVED_ANY" = "1" ]; then
    if ! PIPX_SNAPSHOT=$(run_pipx list --json 2>/dev/null); then
        echo "pipx could not confirm that OMM environments were removed." >&2
        uninstall_failed
    fi
    if { [ "$REMOVED_NEW" = "1" ] && pipx_snapshot_has_environment omm-model; } || \
       { [ "$REMOVED_LEGACY" = "1" ] && pipx_snapshot_has_environment omm; }; then
        echo "pipx still reports an OMM environment after uninstall." >&2
        uninstall_failed
    fi
elif [ -e "$RESOLVED_HOME/src" ] || [ -e "$RESOLVED_HOME/sources" ]; then
    echo "No verified OMM pipx environment was removed; refusing to remove source checkouts." >&2
    uninstall_failed
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
