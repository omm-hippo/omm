#!/usr/bin/env sh
# Installs omm (Open source Model Manager) as an isolated CLI command via pipx.
# Usage: curl -fsSL https://raw.githubusercontent.com/omm-hippo/omm/main/install.sh | sh
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
fakeminjun7321@gmail.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIL4gaNZPEizBHr81LObieqSxd6HExCPK7UKupsTniJ8s"

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

# run_apt() runs as root directly, or via sudo if available and needed -
# bare Docker containers are usually root already (no sudo binary at all).
run_apt() {
    if [ "$(id -u)" = "0" ]; then
        apt-get "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo apt-get "$@"
    else
        return 1
    fi
}

# Minimal Debian/Ubuntu images (e.g. a bare `docker run -it ubuntu bash`)
# often ship without python3 at all, and even when python3 is present,
# python3-venv (which provides ensurepip) is a separate package that's
# easy to miss - without it, pipx's own venv creation fails with a
# cryptic "ensurepip is not available" error. Bootstrap both upfront
# when we're clearly on such a system.
if ! command -v python3 >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
    echo "python3 not found, installing it via apt..."
    run_apt update -qq && run_apt install -y --no-install-recommends python3 python3-venv python3-pip || true
fi

PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        PY=$(command -v "$candidate")
        break
    fi
done
if [ -z "$PY" ]; then
    echo "Python 3.10+ not found: https://www.python.org/downloads/" >&2
    exit 1
fi

# omm is installed from a local git clone (below), so we need the actual
# `git` binary - bare Debian/Ubuntu images (and Docker's official `python`
# images) don't ship it by default.
if ! command -v git >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
    echo "git not found, installing it via apt..."
    run_apt update -qq && run_apt install -y --no-install-recommends git ca-certificates || true
fi

if ! command -v git >/dev/null 2>&1; then
    echo "git not found. Install git first (needed to fetch omm from GitHub)." >&2
    exit 1
fi

if ! "$PY" -c "import ensurepip" >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
    echo "python3-venv not found (needed by pipx), installing it via apt..."
    run_apt update -qq && run_apt install -y --no-install-recommends python3-venv python3-pip || true
fi

# Always run pipx through the exact Python interpreter validated above.
# A random `pipx` executable on PATH may belong to an older Python.
run_pipx() {
    "$PY" -m pipx "$@"
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
    run_pipx ensurepath
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
    verified=1
else
    resolved_commit=$(signing_commit "$head_commit" "$STAGING_DIR")
    if [ "$resolved_commit" != "$head_commit" ] && verify_commit_signature "$resolved_commit" "$STAGING_DIR"; then
        verified=1
    elif [ "$resolved_commit" = "$head_commit" ] && verify_commit_signature "$head_commit" "$STAGING_DIR"; then
        verified=1
    else
        verified=0
    fi
fi
if [ "$verified" != "1" ]; then
    rm -rf "$STAGING_DIR"
    echo "Signature verification failed - refusing to install untrusted code." >&2
    exit 1
fi
SRC_DIR="$SOURCES_DIR/$head_commit"
if [ -d "$SRC_DIR" ]; then
    rm -rf "$STAGING_DIR"
else
    mv "$STAGING_DIR" "$SRC_DIR"
fi

# NVIDIA VRAM detection is dead weight on Mac (no NVIDIA GPUs since 2016) -
# only pull that extra in on other platforms.
if command -v nvidia-smi >/dev/null 2>&1; then
    INSTALL_SPEC="$SRC_DIR[nvidia]"
else
    INSTALL_SPEC="$SRC_DIR"
fi

echo "Installing omm (editable) from $SRC_DIR ..."
run_pipx install --force --editable --python "$PY" "$INSTALL_SPEC"

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
echo "Done. If 'omm' isn't found, open a new shell (pipx just updated your PATH)."
echo "Try:  omm scan"
echo "Tip: run 'omm --install-completion' once (then restart your shell) to enable Tab completion for install/uninstall."
