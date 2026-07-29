#!/usr/bin/env sh
# Installs omm (Open source Model Manager) as an isolated CLI command via pipx.
# Usage: curl -fsSL https://raw.githubusercontent.com/omm-hippo/omm/main/install.sh | sh
set -eu

REPO_URL="https://github.com/omm-hippo/omm.git"
SRC_DIR="$HOME/.omm/src"

# Trust anchor for the signature check below - must stay identical to
# src/omm/trust/allowed_signers in the repo (that copy is what `omm
# update` verifies future commits against once installed; this one is
# the TOFU root for a brand new machine, since there's no prior install
# to carry a trusted copy yet).
ALLOWED_SIGNERS_CONTENT="seong381400@gmail.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPh12ERbI3Yx6DPiaROPjCyI2GIQXb9Ihbp9J9L4bnpe"

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

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found. Install Python 3.10+ first: https://www.python.org/downloads/" >&2
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

PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 10) else 0)')
if [ "$PY_OK" != "1" ]; then
    echo "omm requires Python 3.10+, found: $(python3 --version)" >&2
    exit 1
fi

if ! python3 -c "import ensurepip" >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
    echo "python3-venv not found (needed by pipx), installing it via apt..."
    run_apt update -qq && run_apt install -y --no-install-recommends python3-venv python3-pip || true
fi

# Run pipx either as a direct command (brew/apt install, or once PATH
# catches up) or as `python3 -m pipx` (works right after a pip --user
# install, before PATH is refreshed in this shell).
run_pipx() {
    if command -v pipx >/dev/null 2>&1; then
        pipx "$@"
    else
        python3 -m pipx "$@"
    fi
}

if ! command -v pipx >/dev/null 2>&1 && ! python3 -m pipx --version >/dev/null 2>&1; then
    echo "pipx not found, installing it..."
    if command -v brew >/dev/null 2>&1; then
        # Skip brew's implicit `brew update` before installing - it can add
        # tens of seconds to minutes if the formula index is stale, and
        # pipx's version rarely matters enough to need the latest index.
        HOMEBREW_NO_AUTO_UPDATE=1 brew install pipx
    elif command -v apt-get >/dev/null 2>&1 && run_apt update -qq && run_apt install -y --no-install-recommends pipx; then
        # Ubuntu 23.04+/Debian 12+ ship a pipx package that correctly pulls
        # in python3-venv as a dependency - preferred over --user pip when
        # available since it avoids PEP-668 "externally-managed-environment"
        # entirely.
        :
    elif python3 -m pip install --user --quiet pipx 2>/dev/null; then
        :
    else
        # Homebrew/PEP-668 "externally-managed-environment" Pythons refuse
        # plain --user installs; pipx itself is safe to force here since it
        # only manages its own isolated venvs afterward.
        python3 -m pip install --user --quiet --break-system-packages pipx
    fi
    run_pipx ensurepath
fi

echo "Cloning omm source to $SRC_DIR ..."
rm -rf "$SRC_DIR"
git clone --filter=blob:none --quiet "$REPO_URL" "$SRC_DIR"

echo "Verifying commit signature ..."
head_commit=$(git -C "$SRC_DIR" rev-parse HEAD)
# Try the commit itself first - a maintainer can directly SSH-sign a merge
# commit (e.g. syncing one branch into another outside GitHub's PR flow),
# and that signature is trustworthy on its own. Only fall back to
# signing_commit's second-parent heuristic - for GitHub's own "create a
# merge commit" PRs, where GitHub signs the merge with a key this script
# doesn't trust and the real signature is one hop down, on the PR branch
# tip - when the direct check fails.
if verify_commit_signature "$head_commit" "$SRC_DIR" quiet; then
    verified=1
else
    resolved_commit=$(signing_commit "$head_commit" "$SRC_DIR")
    if [ "$resolved_commit" != "$head_commit" ] && verify_commit_signature "$resolved_commit" "$SRC_DIR"; then
        verified=1
    elif [ "$resolved_commit" = "$head_commit" ] && verify_commit_signature "$head_commit" "$SRC_DIR"; then
        verified=1
    else
        verified=0
    fi
fi
if [ "$verified" != "1" ]; then
    rm -rf "$SRC_DIR"
    echo "Signature verification failed - refusing to install untrusted code." >&2
    exit 1
fi

# NVIDIA VRAM detection is dead weight on Mac (no NVIDIA GPUs since 2016) -
# only pull that extra in on other platforms.
if [ "$(uname -s)" = "Darwin" ]; then
    INSTALL_SPEC="$SRC_DIR"
else
    INSTALL_SPEC="$SRC_DIR[nvidia]"
fi

echo "Installing omm (editable) from $SRC_DIR ..."
run_pipx install --force --editable "$INSTALL_SPEC"

# `pipx ensurepath` (via the `userpath` package) writes the PATH line to
# ~/.profile, which login shells source but plain interactive shells don't
# (e.g. many Docker/container terminals, like Kasm's, only source ~/.bashrc
# and never touch ~/.profile) - so a brand new shell still can't find omm.
# Belt-and-suspenders: make sure ~/.bashrc also gets the PATH line.
BASHRC="$HOME/.bashrc"
LOCAL_BIN="$HOME/.local/bin"
if [ -f "$BASHRC" ] && ! grep -qF "$LOCAL_BIN" "$BASHRC" 2>/dev/null; then
    printf '\nexport PATH="%s:$PATH"\n' "$LOCAL_BIN" >> "$BASHRC"
fi

# zsh's default Tab completion just lists matches - it needs
# `menu select` explicitly enabled to let Tab cycle through a grid of
# candidates and Enter pick one, which is what most people expect from
# "Tab completion". Add it once if the user has a ~/.zshrc and doesn't
# already set it.
ZSHRC="$HOME/.zshrc"
if [ -f "$ZSHRC" ] && ! grep -qF "completion:*' menu select" "$ZSHRC" 2>/dev/null; then
    printf "\n# omm: enable interactive Tab-completion menu (zsh)\nzstyle ':completion:*' menu select\n" >> "$ZSHRC"
fi

echo
echo "======================================================================"
echo " Done! Your PATH was just updated, so THIS terminal doesn't see 'omm' yet."
echo " Open a NEW terminal, or run:  source ~/.bashrc   (zsh: source ~/.zshrc)"
echo "======================================================================"
echo
echo "Then try:  omm scan"
echo "Tip: run 'omm --install-completion' once (then restart your shell) to enable Tab completion for install/uninstall."
echo "     (zsh users: a menu-select zstyle was added to ~/.zshrc so Tab cycles through matches instead of just listing them.)"
