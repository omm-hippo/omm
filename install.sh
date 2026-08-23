#!/usr/bin/env sh
# Installs omm (Open source Model Manager) as an isolated CLI command via pipx.
# Usage: curl -fsSL https://omm.run/install.sh | sh
set -eu

REPO_URL="https://github.com/omm-hippo/omm.git"
OMM_HOME="${OMM_HOME:-$HOME/.omm}"
SOURCES_DIR="$OMM_HOME/sources"
case "$OMM_HOME" in
    /*) ;;
    *) echo "Refusing non-absolute OMM_HOME: $OMM_HOME" >&2; exit 1 ;;
esac
case "$OMM_HOME" in
    ""|/|"$HOME"|"$HOME"/) echo "Refusing unsafe OMM_HOME: $OMM_HOME" >&2; exit 1 ;;
esac
if [ -d "$OMM_HOME" ]; then
    resolved_omm_home=$(cd -P -- "$OMM_HOME" && pwd -P)
    current_dir=$(pwd -P)
    case "$current_dir" in
        "$resolved_omm_home"|"$resolved_omm_home"/*)
            echo "Refusing OMM_HOME that contains the current directory: $resolved_omm_home" >&2
            exit 1
            ;;
    esac
fi

case "$(uname -s 2>/dev/null || true)" in
    MINGW*|MSYS*|CYGWIN*)
        echo "Windows detected. Run the native PowerShell installer instead:" >&2
        echo "  irm https://raw.githubusercontent.com/omm-hippo/omm/main/install.ps1 | iex" >&2
        exit 1
        ;;
esac

# Trust anchor for the signature check below - must stay identical to
# src/omm/trust/allowed_signers in the repo (that copy is what `omm
# update` verifies future commits against once installed; this one is
# the TOFU root for a brand new machine, since there's no prior install
# to carry a trusted copy yet).
ALLOWED_SIGNERS_CONTENT="seong381400@gmail.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPh12ERbI3Yx6DPiaROPjCyI2GIQXb9Ihbp9J9L4bnpe
ahseongchoi@gmail.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIO5UPWuM/1GxGo5TQ5nEJm9UvXShygIozjbvxB1VT9u6
fakeminjun7321@gmail.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIL4gaNZPEizBHr81LObieqSxd6HExCPK7UKupsTniJ8s
github-actions[bot]@users.noreply.github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICn3omW2ymuC5oHshx3WC7AcPP/wP0sLn2E/x4njWMP+"

# Resolves $1 (a commit-ish) in the git repo at $2 to the commit whose
# signature actually matters. The repo only accepts changes to main via a
# GitHub-merged PR ("create a merge commit" strategy) - GitHub builds that
# merge commit itself and signs it with GitHub's own key, while the
# contributor's signature lives on the merge commit's second parent (the
# PR branch tip). For a normal two-parent merge commit, resolve to that
# second parent; anything else (a direct single-parent commit, or an
# octopus merge) is returned as-is.
signing_commit() {
    commit="$1"
    repo_dir="$2"

    parents=$(git -C "$repo_dir" rev-list --parents -n 1 "$commit")
    # shellcheck disable=SC2086 # word-splitting is exactly what we want here
    set -- $parents
    if [ "$#" -eq 3 ]; then
        echo "$3"
    else
        echo "$commit"
    fi
}

# Verifies $1 (a commit-ish, usually HEAD) in the git repo at $2 is
# SSH-signed by a key from ALLOWED_SIGNERS_CONTENT. Fails closed: git
# too old to check SSH signatures, or verification itself erroring out,
# is treated the same as an actual bad signature - "can't verify" must
# never silently mean "trust it anyway".
verify_commit_signature() {
    commit="$1"
    repo_dir="$2"
    quiet="${3:-}"

    git_version=$(git --version | awk '{print $3}')
    git_major=$(echo "$git_version" | cut -d. -f1)
    git_minor=$(echo "$git_version" | cut -d. -f2)
    if [ "$git_major" -lt 2 ] || { [ "$git_major" -eq 2 ] && [ "$git_minor" -lt 34 ]; }; then
        echo "git 2.34+ is required to verify SSH commit signatures (found $git_version)." >&2
        return 1
    fi

    signers_file=$(mktemp)
    printf '%s\n' "$ALLOWED_SIGNERS_CONTENT" > "$signers_file"

    # `set -e` would abort the whole script on a nonzero exit here (a
    # known gotcha with `var=$(cmd)` assignments) - routing through an
    # `if` explicitly guards against that so we can print our own error
    # and return, instead of the script just dying mid-verification.
    if verify_output=$(git -c gpg.format=ssh -c gpg.ssh.allowedSignersFile="$signers_file" \
        -C "$repo_dir" verify-commit "$commit" 2>&1); then
        rm -f "$signers_file"
        return 0
    fi
    rm -f "$signers_file"
    if [ "$quiet" != "quiet" ]; then
        echo "$verify_output" | sed 's/^/  /' >&2
    fi
    return 1
}

# run_as_root() runs as root directly, or via sudo if available and needed -
# bare containers are usually root already (no sudo binary at all).
run_as_root() {
    if [ "$(id -u)" = "0" ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        echo "Cannot install system dependencies: root or sudo is required." >&2
        return 1
    fi
}

find_supported_python() {
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 && \
           "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

detect_package_manager() {
    for manager in apt-get dnf yum pacman apk; do
        if command -v "$manager" >/dev/null 2>&1; then
            echo "$manager"
            return 0
        fi
    done
    return 1
}

install_system_packages() {
    manager="$1"
    shift
    case "$manager" in
        apt-get)
            run_as_root apt-get update -qq
            run_as_root apt-get install -y --no-install-recommends "$@"
            ;;
        dnf)
            run_as_root dnf install -y "$@"
            ;;
        yum)
            run_as_root yum install -y "$@"
            ;;
        pacman)
            run_as_root pacman -Sy --noconfirm "$@"
            ;;
        apk)
            run_as_root apk add --no-cache "$@"
            ;;
        *)
            echo "Unsupported system package manager: $manager" >&2
            return 1
            ;;
    esac
}

find_brew() {
    if command -v brew >/dev/null 2>&1; then
        command -v brew
        return 0
    fi
    for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
        if [ -x "$candidate" ]; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

prepare_macos_brew() {
    BREW=$(find_brew || true)
    if [ -z "$BREW" ]; then
        if [ "${OMM_AUTO_INSTALL_HOMEBREW:-1}" != "1" ]; then
            echo "Homebrew is required on macOS when Python 3.10+ or git is missing." >&2
            echo "Install it first: https://brew.sh/" >&2
            echo "To let this installer bootstrap it, unset OMM_AUTO_INSTALL_HOMEBREW or set it to 1." >&2
            return 1
        fi
        if ! command -v curl >/dev/null 2>&1 || [ ! -x /bin/bash ]; then
            echo "Cannot bootstrap Homebrew: curl and /bin/bash are required." >&2
            return 1
        fi
        echo "Homebrew not found, installing it with Homebrew's official installer..."
        if ! homebrew_script=$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh); then
            echo "Could not download the Homebrew installer: https://brew.sh/" >&2
            return 1
        fi
        if [ -r /dev/tty ]; then
            brew_install_status=0
            /bin/bash -c "$homebrew_script" </dev/tty || brew_install_status=$?
        else
            brew_install_status=0
            NONINTERACTIVE=1 /bin/bash -c "$homebrew_script" || brew_install_status=$?
        fi
        if [ "$brew_install_status" -ne 0 ]; then
            echo "Homebrew installation failed. Install it manually from https://brew.sh/ and rerun this installer." >&2
            return 1
        fi
        unset homebrew_script
        BREW=$(find_brew || true)
    fi
    if [ -z "$BREW" ]; then
        echo "Homebrew was installed but brew is still not on PATH." >&2
        echo "Add the Homebrew shellenv shown by the installer, then rerun this command." >&2
        return 1
    fi
    BREW_PREFIX=$("$BREW" --prefix)
    PATH="$BREW_PREFIX/bin:$PATH"
    export PATH
    BREW=$(find_brew)
}

OS_NAME=$(uname -s 2>/dev/null || true)
PACKAGE_MANAGER=""
PY=$(find_supported_python || true)

if [ "$OS_NAME" = "Darwin" ]; then
    # A clean macOS installation normally has neither Python nor git. Homebrew
    # is the supported bootstrap for both; the official installer also checks
    # the macOS/Command Line Tools requirements before changing the machine.
    if [ -z "$PY" ] || ! command -v git >/dev/null 2>&1; then
        prepare_macos_brew
    fi
    if [ -z "$PY" ]; then
        echo "Python 3.10+ not found, installing it via Homebrew..."
        "$BREW" install python
    fi
    if ! command -v git >/dev/null 2>&1; then
        echo "git not found, installing it via Homebrew..."
        "$BREW" install git
    fi
else
    PACKAGE_MANAGER=$(detect_package_manager || true)
    if [ -z "$PY" ]; then
        if [ -z "$PACKAGE_MANAGER" ]; then
            echo "Python 3.10+ not found and no supported package manager was found." >&2
            echo "Supported managers: apt-get, dnf, yum, pacman, apk." >&2
            echo "Install Python 3.10+ and python-pip/venv first: https://www.python.org/downloads/" >&2
            exit 1
        fi
        echo "Python 3.10+ not found, installing it via $PACKAGE_MANAGER..."
        case "$PACKAGE_MANAGER" in
            apt-get) install_system_packages "$PACKAGE_MANAGER" python3 python3-venv python3-pip ca-certificates ;;
            pacman) install_system_packages "$PACKAGE_MANAGER" python python-pip ca-certificates ;;
            apk) install_system_packages "$PACKAGE_MANAGER" python3 py3-pip ca-certificates ;;
            *) install_system_packages "$PACKAGE_MANAGER" python3 python3-pip ca-certificates ;;
        esac
        PY=$(find_supported_python || true)
    fi
    if ! command -v git >/dev/null 2>&1; then
        if [ -z "$PACKAGE_MANAGER" ]; then
            echo "git not found and no supported package manager was found." >&2
            echo "Install git 2.34+ first: https://git-scm.com/downloads" >&2
            exit 1
        fi
        echo "git not found, installing it via $PACKAGE_MANAGER..."
        install_system_packages "$PACKAGE_MANAGER" git ca-certificates
    fi
fi

PY=$(find_supported_python || true)
if [ -z "$PY" ]; then
    echo "Python 3.10+ not found after dependency bootstrap: https://www.python.org/downloads/" >&2
    exit 1
fi

if ! command -v git >/dev/null 2>&1; then
    echo "git not found after dependency bootstrap (needed to fetch omm from GitHub)." >&2
    exit 1
fi

if ! "$PY" -c "import ensurepip" >/dev/null 2>&1; then
    if [ "$OS_NAME" = "Darwin" ]; then
        if [ -z "${BREW:-}" ]; then
            prepare_macos_brew
        fi
        echo "Python venv support is missing, reinstalling Homebrew Python..."
        "$BREW" install python
    elif [ -n "$PACKAGE_MANAGER" ]; then
        echo "Python venv support is missing, installing pip/venv support..."
        case "$PACKAGE_MANAGER" in
            apt-get) install_system_packages "$PACKAGE_MANAGER" python3-venv python3-pip ;;
            pacman) install_system_packages "$PACKAGE_MANAGER" python python-pip ;;
            apk) install_system_packages "$PACKAGE_MANAGER" python3 py3-pip ;;
            *) install_system_packages "$PACKAGE_MANAGER" python3-pip ;;
        esac
    else
        echo "Python ensurepip/venv support is missing; install the Python venv package first." >&2
        exit 1
    fi
    PY=$(find_supported_python || true)
fi

if ! "$PY" -m pip --version >/dev/null 2>&1; then
    echo "pip is unavailable for $PY; install pip for Python 3.10+ and rerun this installer." >&2
    exit 1
fi

# Always run pipx through the exact Python interpreter validated above.
# A random `pipx` executable on PATH may belong to an older Python.
run_pipx() {
    "$PY" -m pipx "$@"
}

PIPX_ENV="omm-model"
LEGACY_PIPX_ENV="omm"

pipx_snapshot_has_environment() {
    printf '%s' "$PIPX_SNAPSHOT" | "$PY" -c \
        'import json, sys; raise SystemExit(0 if sys.argv[1] in json.load(sys.stdin).get("venvs", {}) else 1)' \
        "$1"
}

pipx_snapshot_environment_is() {
    printf '%s' "$PIPX_SNAPSHOT" | "$PY" -c '
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

# The `omm` name is also used by an unrelated PyPI project. Treat that pipx
# environment as our legacy install only when its own distribution metadata
# exposes `omm = omm.cli:main` and its direct source is a known OMM Git repo,
# or a verified checkout under this installer's OMM_HOME.
verify_omm_pipx_environment() {
    env_name="$1"
    distribution="$2"
    require_legacy_source="$3"
    expected_version="${4:-}"
    env_python=$(pipx_environment_python "$env_name") || return 1
    "$env_python" - "$distribution" "$OMM_HOME" "$require_legacy_source" "$expected_version" <<'PY'
import importlib.metadata
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

distribution, omm_home, require_source, expected_version = sys.argv[1:]
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
if expected_version and dist.version != expected_version:
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

refresh_pipx_snapshot() {
    PIPX_SNAPSHOT=$(run_pipx list --json 2>/dev/null)
}

verify_installed_omm_model() {
    refresh_pipx_snapshot || return 1
    pipx_snapshot_has_environment "$PIPX_ENV" || return 1
    pipx_snapshot_environment_is "$PIPX_ENV" "$PIPX_ENV" || return 1
    verify_omm_pipx_environment "$PIPX_ENV" "$PIPX_ENV" 0 "$EXPECTED_VERSION" || return 1
    [ -x "$PIPX_BIN_DIR/omm" ] || return 1
    [ -x "$PIPX_LOCAL_VENVS/$PIPX_ENV/bin/omm" ] || return 1
    [ "$PIPX_BIN_DIR/omm" -ef "$PIPX_LOCAL_VENVS/$PIPX_ENV/bin/omm" ] || return 1
    version_output=$("$PIPX_BIN_DIR/omm" --version 2>/dev/null) || return 1
    [ "$version_output" = "omm $EXPECTED_VERSION" ]
}

verify_exposed_existing_environment() {
    env_name="$1"
    distribution="$2"
    require_legacy_source="$3"
    refresh_pipx_snapshot || return 1
    pipx_snapshot_environment_is "$env_name" "$distribution" || return 1
    verify_omm_pipx_environment "$env_name" "$distribution" "$require_legacy_source" || return 1
    env_python=$(pipx_environment_python "$env_name") || return 1
    internal_app="$PIPX_LOCAL_VENVS/$env_name/bin/omm"
    [ -x "$internal_app" ] && [ -x "$PIPX_BIN_DIR/omm" ] || return 1
    [ "$PIPX_BIN_DIR/omm" -ef "$internal_app" ] || return 1
    expected_version=$("$env_python" -c 'import importlib.metadata, sys; print(importlib.metadata.version(sys.argv[1]))' "$distribution" 2>/dev/null) || return 1
    version_output=$("$PIPX_BIN_DIR/omm" --version 2>/dev/null) || return 1
    [ "$version_output" = "omm $expected_version" ]
}

ensure_pipx_bin_path() {
    # `pipx ensurepath` updates shell startup files, but it cannot update the
    # environment of this already-running `curl | sh` process. Add the bin
    # directory here as well so every command in this installer sees the same
    # PATH, and so a freshly opened shell can find `omm` immediately.
    case ":$PATH:" in
        *":$PIPX_BIN_DIR:"*) ;;
        *) PATH="$PIPX_BIN_DIR:$PATH"; export PATH ;;
    esac

    # macOS Terminal and iTerm start zsh as a login shell, which reads
    # ~/.zprofile. pipx's automatic profile detection is not reliable when
    # the installer itself is run through `sh` (and it may only update a
    # non-login startup file), so make the login-shell contract explicit.
    case "$(uname -s 2>/dev/null || true)" in
        Darwin)
            zprofile="$HOME/.zprofile"
            if [ -e "$zprofile" ] && [ ! -f "$zprofile" ]; then
                echo "Cannot configure zsh PATH: $zprofile is not a regular file." >&2
                return 1
            fi
            if ! grep -Fq "$PIPX_BIN_DIR" "$zprofile" 2>/dev/null; then
                # Escape the two characters that could change the shell
                # assignment while preserving the literal $PATH expansion.
                shell_path=$(printf '%s' "$PIPX_BIN_DIR" | sed 's/[\\"]/\\&/g')
                # shellcheck disable=SC2016  # $PATH must stay literal in the profile entry.
                if ! printf '\n# Added by omm installer for pipx applications.\nexport PATH="%s:$PATH"\n' \
                    "$shell_path" >> "$zprofile"; then
                    echo "Cannot configure zsh PATH in $zprofile." >&2
                    return 1
                fi
            fi
            ;;
    esac
}

if ! "$PY" -m pipx --version >/dev/null 2>&1; then
    echo "pipx not found, installing it..."
    if "$PY" -m pip install --user --quiet pipx 2>/dev/null; then
        :
    else
        # Homebrew/PEP-668 "externally-managed-environment" Pythons refuse
        # plain --user installs; pipx itself is safe to force here since it
        # only manages its own isolated venvs afterward.
        "$PY" -m pip install --user --quiet --break-system-packages pipx
    fi
fi

# pipx's shared venv (pip/setuptools) is borrowed by every `pipx install`;
# a half-upgraded pip in there (seen on Windows as `ImportError: cannot
# import name 'get_runnable_pip'`) kills every install before omm's code is
# reached. If its pip can't print a version, drop it - pipx rebuilds it.
PIPX_SHARED_LIBS=$(run_pipx environment --value PIPX_SHARED_LIBS 2>/dev/null || true)
if [ -n "$PIPX_SHARED_LIBS" ] && [ -x "$PIPX_SHARED_LIBS/bin/python" ] \
    && ! "$PIPX_SHARED_LIBS/bin/python" -m pip --version >/dev/null 2>&1; then
    echo "pipx's shared pip is broken; rebuilding it..."
    rm -rf "$PIPX_SHARED_LIBS"
fi

PIPX_LOCAL_VENVS=$(run_pipx environment --value PIPX_LOCAL_VENVS)
PIPX_BIN_DIR=$(run_pipx environment --value PIPX_BIN_DIR)
# Keep pipx's normal shell integration for non-macOS shells, then explicitly
# make the path reliable for macOS login zsh and for this process itself.
run_pipx ensurepath >/dev/null 2>&1 || true
ensure_pipx_bin_path
if ! refresh_pipx_snapshot; then
    echo "Could not inspect existing pipx environments; refusing an unsafe migration." >&2
    exit 1
fi

LEGACY_PIPX_PRESENT=0
if pipx_snapshot_has_environment "$LEGACY_PIPX_ENV"; then
    if ! pipx_snapshot_environment_is "$LEGACY_PIPX_ENV" "$LEGACY_PIPX_ENV" || \
       ! verify_omm_pipx_environment "$LEGACY_PIPX_ENV" "$LEGACY_PIPX_ENV" 1; then
        echo "Refusing to replace unrelated pipx environment 'omm'. Remove or rename that environment manually first." >&2
        exit 1
    fi
    LEGACY_PIPX_PRESENT=1
fi

NEW_PIPX_PRESENT=0
if pipx_snapshot_has_environment "$PIPX_ENV"; then
    if ! pipx_snapshot_environment_is "$PIPX_ENV" "$PIPX_ENV" || \
       ! verify_omm_pipx_environment "$PIPX_ENV" "$PIPX_ENV" 0; then
        echo "Refusing to replace an unverified $PIPX_ENV pipx environment." >&2
        exit 1
    fi
    NEW_PIPX_PRESENT=1
fi

mkdir -p "$SOURCES_DIR"
STAGING_DIR="$SOURCES_DIR/checkout.$$"
rm -rf "$STAGING_DIR"
echo "Cloning omm source to a versioned staging directory ..."
git clone --filter=blob:none --quiet "$REPO_URL" "$STAGING_DIR"

echo "Verifying commit signature ..."
head_commit=$(git -C "$STAGING_DIR" rev-parse HEAD)
# Try the commit itself first - a maintainer can directly SSH-sign a merge
# commit (e.g. syncing one branch into another outside GitHub's PR flow),
# and that signature is trustworthy on its own. Only fall back to
# signing_commit's second-parent heuristic - for GitHub's own "create a
# merge commit" PRs, where GitHub signs the merge with a key this script
# doesn't trust and the real signature is one hop down, on the PR branch
# tip - when the direct check fails.
if verify_commit_signature "$head_commit" "$STAGING_DIR" quiet; then
    verified_commit="$head_commit"
else
    resolved_commit=$(signing_commit "$head_commit" "$STAGING_DIR")
    if [ "$resolved_commit" != "$head_commit" ] && verify_commit_signature "$resolved_commit" "$STAGING_DIR"; then
        verified_commit="$resolved_commit"
    elif [ "$resolved_commit" = "$head_commit" ] && verify_commit_signature "$head_commit" "$STAGING_DIR"; then
        verified_commit="$head_commit"
    else
        verified_commit=""
    fi
fi
if [ -z "$verified_commit" ]; then
    rm -rf "$STAGING_DIR"
    echo "Signature verification failed - refusing to install untrusted code." >&2
    exit 1
fi
if [ "$verified_commit" != "$head_commit" ]; then
    git -C "$STAGING_DIR" checkout --detach --quiet "$verified_commit"
fi
SRC_DIR="$SOURCES_DIR/$verified_commit"
if [ -d "$SRC_DIR" ]; then
    rm -rf "$STAGING_DIR"
else
    mv "$STAGING_DIR" "$SRC_DIR"
fi
EXPECTED_VERSION=$(sed -n 's/^version = "\([^"]*\)"[[:space:]]*$/\1/p' "$SRC_DIR/pyproject.toml" | head -n 1)
if [ -z "$EXPECTED_VERSION" ]; then
    echo "Could not determine the project version from the verified checkout." >&2
    exit 1
fi

# NVIDIA VRAM detection is dead weight on Mac (no NVIDIA GPUs since 2016) -
# only pull that extra in on other platforms.
if command -v nvidia-smi >/dev/null 2>&1; then
    INSTALL_SPEC="${SRC_DIR}[nvidia]"
else
    INSTALL_SPEC="$SRC_DIR"
fi

echo "Installing omm (editable) from $SRC_DIR ..."
# pipx names the new environment after the distribution (`omm-model`)
# while old Git installs used `omm`. Install and verify the new environment
# first so an install failure leaves the working legacy CLI untouched. pipx
# --force moves the shared `omm` app link to the new environment; current pipx
# preserves that foreign-owned link when the legacy environment is removed.
rollback_failed_new_install() {
    ROLLBACK_STATE=not-needed
    if refresh_pipx_snapshot && pipx_snapshot_has_environment "$PIPX_ENV"; then
        if [ "$NEW_PIPX_PRESENT" = "0" ]; then
            run_pipx uninstall "$PIPX_ENV" >/dev/null 2>&1 || true
        else
            run_pipx reinstall "$PIPX_ENV" >/dev/null 2>&1 || true
        fi
    fi
    if [ "$LEGACY_PIPX_PRESENT" = "1" ]; then
        if run_pipx reinstall "$LEGACY_PIPX_ENV" >/dev/null 2>&1 && \
           verify_exposed_existing_environment "$LEGACY_PIPX_ENV" "$LEGACY_PIPX_ENV" 1; then
            ROLLBACK_STATE=verified
        else
            ROLLBACK_STATE=uncertain
        fi
    elif [ "$NEW_PIPX_PRESENT" = "1" ]; then
        if verify_exposed_existing_environment "$PIPX_ENV" "$PIPX_ENV" 0; then
            ROLLBACK_STATE=verified
        else
            ROLLBACK_STATE=uncertain
        fi
    elif ! refresh_pipx_snapshot || pipx_snapshot_has_environment "$PIPX_ENV"; then
        ROLLBACK_STATE=uncertain
    fi
}
report_failed_install() {
    reason="$1"
    echo "$reason" >&2
    if [ "$ROLLBACK_STATE" = "verified" ]; then
        echo "The pre-existing omm command was restored and verified." >&2
    elif [ "$ROLLBACK_STATE" = "uncertain" ]; then
        echo "The previous environment was not removed, but its omm command could not be verified after rollback; run 'pipx reinstall omm' or 'pipx reinstall omm-model'." >&2
    fi
}
# pipx can upgrade its shared pip *during* `pipx install` and, with more
# than one pipx copy pointing at the same shared dir, leave it half-replaced
# for the very run that needs it. One retry after wiping the shared venv
# (pipx rebuilds it whole) - see the matching block in install.ps1.
pipx_install_with_repair() {
    if run_pipx install --force --editable --python "$PY" "$INSTALL_SPEC"; then
        return 0
    fi
    shared=$(run_pipx environment --value PIPX_SHARED_LIBS 2>/dev/null || true)
    if [ -z "$shared" ] || [ ! -d "$shared" ]; then
        return 1
    fi
    echo "pipx install failed; rebuilding pipx's shared pip and retrying once..."
    rm -rf "$shared"
    run_pipx install --force --editable --python "$PY" "$INSTALL_SPEC"
}
if ! pipx_install_with_repair; then
    rollback_failed_new_install
    report_failed_install "pipx install failed; the legacy environment was not removed."
    exit 1
fi
if ! verify_installed_omm_model; then
    rollback_failed_new_install
    report_failed_install "The new $PIPX_ENV environment or its omm command failed verification; the legacy environment was not removed."
    exit 1
fi
if [ "$LEGACY_PIPX_PRESENT" = "1" ]; then
    echo "Removing verified legacy pipx environment: $LEGACY_PIPX_ENV"
    if ! run_pipx uninstall "$LEGACY_PIPX_ENV"; then
        echo "Could not remove the verified legacy pipx environment." >&2
        if verify_installed_omm_model; then
            echo "The new omm command remains installed and was verified." >&2
        elif run_pipx install --force --editable --python "$PY" "$INSTALL_SPEC" && \
             verify_installed_omm_model; then
            echo "The new omm command was repaired and verified after the pipx failure." >&2
        else
            echo "pipx may have removed the omm command link before failing; repair could not be verified. Run 'pipx reinstall omm-model'." >&2
        fi
        exit 1
    fi
    if ! verify_installed_omm_model; then
        echo "Repairing the new omm command after legacy cleanup ..." >&2
        if ! run_pipx install --force --editable --python "$PY" "$INSTALL_SPEC" || \
           ! verify_installed_omm_model; then
            echo "The new omm command could not be verified after legacy cleanup." >&2
            exit 1
        fi
    fi
fi

# Marks custom OMM_HOME directories as installer-managed. The uninstaller
# requires this marker before removing anything from a non-default home.
printf '%s\n' 'omm installer managed home v1' > "$OMM_HOME/.omm-managed"

# pipx now points at the verified checkout above. Old versioned checkouts are
# no longer active; best-effort cleanup keeps reinstalls from accumulating.
for old_source in "$SOURCES_DIR"/*; do
    if [ "$old_source" != "$SRC_DIR" ]; then
        rm -rf "$old_source" 2>/dev/null || true
    fi
done
rm -rf "$OMM_HOME/src" 2>/dev/null || true

echo
echo "Done. Open a new shell so zsh loads the pipx PATH configured by the installer."
echo "Try:  omm scan"
echo "Tip: run 'omm --install-completion' once (then restart your shell) to enable Tab completion for install/uninstall."
