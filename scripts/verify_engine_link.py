"""Live, single-engine link/recognition check run from CI (see the
`.github/workflows/ci-engine-*.yml` workflows spawned by issue #94).

This is deliberately outside the mocked unit test suite - it exercises the
real `linker.link_engine()` against a *real* installed engine on a *real*
filesystem/daemon, on whatever runner OS a given engine's workflow uses.
Each engine's own recognition logic decides what "recognized" means here:
Ollama and LM Studio actually query their running daemon/CLI; the rest
(Jan, AnythingLLM, Msty, KoboldCpp, text-generation-webui) never need a
live daemon, but still can't reuse `scan_import.py`'s scan_*() functions
to check - those are built to find *unmanaged* models to adopt, so they
deliberately skip anything that's a symlink, which is exactly what
`link_engine` just created. So this recomputes each engine's own expected
path/manifest shape directly instead (Jan's is the one exception: its
model.yml points at the original file, never a symlink, so scan_jan()
works unmodified).

Usage: `python scripts/verify_engine_link.py <engine-key>` - exits nonzero
with a message on any failure, prints a one-line success summary otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Not a TTY under CI, so stdout defaults to block-buffered - every
# on_output=print line from the installer subprocess (and this script's own
# diagnostics) would otherwise sit in the buffer and only appear, all at
# once and out of order relative to stderr, when the process exits. Real-time,
# correctly ordered logs are the only way to diagnose a run that fails
# intermittently, since a failed run can't be reproduced after the fact.
sys.stdout.reconfigure(line_buffering=True)

from omm import linker, scan_import  # noqa: E402


def _pack_gguf_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return struct.pack("<Q", len(encoded)) + encoded


def build_minimal_gguf(path: Path, *, architecture: str = "llama") -> None:
    """Writes the smallest file `omm.gguf.read_gguf_metadata` accepts as
    valid: magic + version + a zero tensor count + one KV pair
    (general.architecture). No tensor data - the reader stops before it,
    and so does everything in `linker.py` that inspects a GGUF (only
    `link_ollama`, for the ollama-format engines, actually parses this;
    every other engine here treats the file as opaque bytes)."""
    kv_pairs = [("general.architecture", architecture)]
    body = b""
    for key, value in kv_pairs:
        body += _pack_gguf_string(key)
        body += struct.pack("<I", 8)  # GGUF value type 8 = string
        body += _pack_gguf_string(value)
    header = b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0) + struct.pack("<Q", len(kv_pairs))
    path.write_bytes(header + body)


def _fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def _run(args: list[str]) -> subprocess.CompletedProcess:
    # Engine CLIs (ollama, lms) write UTF-8 to a pipe on every platform; the
    # interpreter default would be cp949 on Korean Windows and crash this
    # script on the first non-ASCII byte in a model name or path.
    return subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30
    )


def verify_ollama(gguf_path: Path, ollama_tag: str) -> None:
    """`ollama list` re-scans its manifest store, but link_ollama() writes
    the manifest/blob directly rather than going through `ollama create` -
    a real run of this workflow showed the same lag `verify_lmstudio` below
    already retries around: the first call right after linking didn't show
    the tag yet. Retries instead of trusting a single call."""
    import time

    manifest_path = (
        linker.ollama_models_dir()
        / "manifests" / "registry.ollama.ai" / "library" / ollama_tag / "latest"
    )
    print(
        f"verify_ollama: expecting manifest at {manifest_path} "
        f"(exists={manifest_path.exists()})"
    )

    list_output = ""
    for attempt in range(10):
        result = _run(["ollama", "list"])
        if result.returncode != 0:
            _fail(f"`ollama list` failed: {result.stderr}")
        list_output = result.stdout
        if ollama_tag in list_output:
            break
        # Diagnostic only: does `ollama show` (a direct per-tag lookup)
        # see the manifest when `ollama list` (a full-store listing)
        # doesn't? Tells us whether `list` specifically is cached/lagged
        # or whether the daemon isn't picking up the direct write at all.
        show_probe = _run(["ollama", "show", ollama_tag])
        print(
            f"attempt {attempt + 1}: not in `ollama list` yet; "
            f"`ollama show {ollama_tag}` returncode={show_probe.returncode} "
            f"stderr={show_probe.stderr.strip()!r}"
        )
        time.sleep(2)
    else:
        _fail(
            f"`ollama list` never showed {ollama_tag!r} after 10 tries "
            f"(manifest on disk now: exists={manifest_path.exists()}):\n{list_output}"
        )

    result = _run(["ollama", "show", ollama_tag])
    if result.returncode != 0:
        _fail(f"`ollama show {ollama_tag}` failed: {result.stderr}")
    print(f"OK: ollama recognizes {ollama_tag!r} ({result.stdout.splitlines()[0]!r})")


def _lms_path() -> str:
    """`lms` right after a fresh headless install is often not on PATH in
    this same non-interactive process yet (the installer only wires up
    interactive shell rc files) - reuse omm's own resolver, which already
    knows to fall back to the well-known bootstrap location."""
    path = linker._lms_cli_path()
    if path is None:
        _fail("lms CLI not found (neither on PATH nor at <lmstudio_home>/bin/lms) after install")
    return path


def verify_lmstudio(gguf_path: Path, ollama_tag: str) -> None:
    """`lms ls` re-scans LM Studio's models dir, but a real run of this
    workflow showed it can take a few seconds to pick up a file that just
    landed - the first call right after linking only showed llmster's own
    bundled default model, not ours. Retries instead of trusting a single
    call."""
    import time

    lms_path = _lms_path()
    paths: list[str] = []
    for attempt in range(10):
        result = _run([lms_path, "ls", "--json"])
        if result.returncode != 0:
            _fail(f"`lms ls --json` failed: {result.stderr}")
        try:
            models = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            _fail(f"`lms ls --json` did not return JSON: {e}\n{result.stdout}")
        paths = [m.get("path") or m.get("modelKey") or "" for m in models]
        if any(gguf_path.name in p or str(gguf_path) in p for p in paths):
            print(f"OK: lms recognizes {gguf_path.name!r} (after {attempt + 1} `lms ls` call(s))")
            return
        time.sleep(2)
    _fail(f"`lms ls` never listed {gguf_path.name!r} after 10 tries. Last seen: {paths}")


def _verify_via_path(dest: Path, gguf_path: Path, engine_label: str) -> None:
    """Shared shape for the flat-directory engines (KoboldCpp,
    text-generation-webui, Msty). `link_engine` doesn't hand back the
    destination it wrote to, so this recomputes it exactly the way
    `link_custom_directory` does: `<engine's models dir>/<gguf filename>`.

    Deliberately does NOT reuse `scan_import.py`'s scan_koboldcpp() etc.
    for this - those scans intentionally skip symlinks (their whole job
    is finding *unmanaged* external models to adopt, see the module
    docstring), and `link_file` creates exactly a symlink on Linux/macOS.
    Re-running the adopt-scan after linking would always find nothing and
    look like a false failure."""
    if not dest.exists():
        _fail(f"{engine_label} does not see the linked model - expected a file at {dest}")
    if dest.resolve() != gguf_path.resolve():
        _fail(f"{engine_label}'s file at {dest} does not resolve back to our model")
    print(f"OK: {engine_label} recognizes the linked model at {dest}")


def _verify_jan(gguf_path: Path, _ollama_tag: str) -> None:
    """Jan's manifest (`model.yml`) points straight at the original GGUF
    path rather than a copy under Jan's own tree, so - unlike the flat-dir
    engines above - `scan_jan()`'s own symlink-skip never triggers here:
    `gguf_path` itself is a plain file, never a symlink."""
    from omm.hashutil import sha256_file

    expected_sha = sha256_file(gguf_path)
    found = scan_import.scan_jan()
    matches = [f for f in found if f.sha256 == expected_sha]
    if not matches:
        _fail(f"jan's model.yml scan does not see our linked model. Found instead: {[str(f.path) for f in found]}")
    print(f"OK: jan recognizes the linked model via {matches[0].display_name}")


_OLLAMA_MODEL_LAYER = "application/vnd.ollama.image.model"


def _verify_ollama_format_manifest(models_dir: Path, ollama_tag: str, gguf_path: Path, engine_label: str) -> None:
    """Direct manifest+blob check for the other ollama-format engine
    (AnythingLLM's bundled instance) that, unlike real Ollama, has no CLI
    of its own to ask - and, like the flat-dir engines, the blob
    `link_engine` wrote is a symlink, so `scan_anythingllm()` (which
    exists to find *unmanaged* blobs) would always report it missing."""
    from omm.hashutil import sha256_file

    manifest_path = models_dir / "manifests" / "registry.ollama.ai" / "library" / ollama_tag / "latest"
    if not manifest_path.is_file():
        _fail(f"{engine_label} manifest not found at {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_sha = sha256_file(gguf_path)
    digests = [
        layer["digest"].removeprefix("sha256:")
        for layer in manifest.get("layers", [])
        if layer.get("mediaType") == _OLLAMA_MODEL_LAYER
    ]
    if expected_sha not in digests:
        _fail(f"{engine_label} manifest layer digest(s) {digests} do not include our model {expected_sha}")
    blob = models_dir / "blobs" / f"sha256-{expected_sha}"
    if not blob.exists():
        _fail(f"{engine_label} blob missing at {blob}")
    print(f"OK: {engine_label} manifest+blob recognize the linked model ({ollama_tag})")


ENGINE_VERIFIERS = {
    "ollama": verify_ollama,
    "lmstudio": verify_lmstudio,
    "jan": _verify_jan,
    "anythingllm": lambda gguf_path, tag: _verify_ollama_format_manifest(
        linker.anythingllm_ollama_models_dir(), tag, gguf_path, "anythingllm"
    ),
    "mstystudio": lambda gguf_path, _tag: _verify_via_path(
        linker.mstystudio_models_dir() / gguf_path.name, gguf_path, "mstystudio"
    ),
    "koboldcpp": lambda gguf_path, _tag: _verify_via_path(
        linker.koboldcpp_models_dir() / gguf_path.name, gguf_path, "koboldcpp"
    ),
    "textgenwebui": lambda gguf_path, _tag: _verify_via_path(
        linker.textgenwebui_models_dir() / gguf_path.name, gguf_path, "textgenwebui"
    ),
}


def _ensure_ollama_ready() -> None:
    """`ollama` needs its daemon up before `ollama list`/`show` (or
    link_engine's own compat probe) mean anything. The Linux installer
    normally starts this via systemd on its own - poll first, and only
    start it ourselves as a fallback for a runner image where that
    didn't happen."""
    import time

    for _ in range(5):
        if _run(["ollama", "list"]).returncode == 0:
            return
        time.sleep(1)
    subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        if _run(["ollama", "list"]).returncode == 0:
            return
        time.sleep(1)
    _fail("ollama daemon never became ready (`ollama list` kept failing)")


def _ensure_lmstudio_ready() -> None:
    """Same idea as `_ensure_ollama_ready` for llmster/`lms`."""
    import time

    lms_path = _lms_path()
    for _ in range(3):
        if _run([lms_path, "ls", "--json"]).returncode == 0:
            return
        time.sleep(1)
    subprocess.Popen([lms_path, "server", "start", "--no-gui"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        if _run([lms_path, "ls", "--json"]).returncode == 0:
            return
        time.sleep(1)
    _fail("lms CLI never became ready")


ENSURE_READY = {
    "ollama": _ensure_ollama_ready,
    "lmstudio": _ensure_lmstudio_ready,
}


def _ensure_ollama_dir_writable() -> None:
    """CI-only. The real Linux installer runs Ollama under systemd as a
    dedicated `ollama` system user, whose models dir this runner account
    can't write into (issue #117) even once linker.ollama_models_dir()
    resolves it correctly. omm's product code copes with that by falling
    back to native `ollama create`, but that path needs a real GGUF the
    quantizer accepts - this check's placeholder file can never pass that,
    so it would never actually exercise the fast hand-rolled path this
    workflow exists to validate. Grant this runner write access instead,
    the same way a real user would (e.g. `sudo usermod -aG ollama`, or
    just running as ollama themselves)."""
    if platform.system() != "Linux":
        return
    models_dir = linker.ollama_models_dir()
    print(
        f"ollama_models_dir() resolved to {models_dir} "
        f"(exists={models_dir.exists()}, writable={os.access(models_dir, os.W_OK)})"
    )
    if not models_dir.exists() or os.access(models_dir, os.W_OK):
        return
    result = subprocess.run(
        ["sudo", "chmod", "-R", "a+rwX", str(models_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    print(
        f"sudo chmod -R a+rwX {models_dir}: returncode={result.returncode} "
        f"stderr={result.stderr.strip()!r}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("engine", choices=sorted(ENGINE_VERIFIERS))
    args = parser.parse_args()

    if not linker.is_engine_installed(args.engine):
        print(f"{args.engine} not detected yet - installing...")
        result = linker.install_engine(args.engine, on_output=print)
        if result.status != "installed":
            _fail(f"install_engine({args.engine!r}) did not succeed: status={result.status}, {result.message}")

    ENSURE_READY.get(args.engine, lambda: None)()
    if args.engine == "ollama":
        _ensure_ollama_dir_writable()

    with tempfile.TemporaryDirectory() as tmp:
        if platform.system() != "Windows":
            # tempfile.mkdtemp() defaults to mode 0700 (owner only). Ollama's
            # blob is a symlink back to this directory, not a copy - under a
            # systemd-managed install the daemon (its own dedicated user)
            # needs to traverse into here to stat/read the real file, same
            # underlying issue as the manifest permission fix above.
            os.chmod(tmp, 0o755)
        gguf_path = Path(tmp) / "omm-ci-test-model.gguf"
        build_minimal_gguf(gguf_path)
        ollama_tag = "omm-ci-test"

        try:
            if args.engine == "ollama":
                # link_engine() always runs with verify_compat=True for
                # real Ollama, which - if this Ollama version rejects
                # omm's hand-rolled manifest - falls back to native
                # `ollama create`. That path runs llama-quantize's real
                # model validation, which our placeholder GGUF (a header
                # and one KV pair, no tensor data at all) can never pass;
                # a real run of this workflow hit exactly that (Ollama's
                # acceptance of the hand-rolled format isn't guaranteed
                # stable across its own releases). That compat/fallback
                # machinery is what issue #91 already covers - what #94
                # actually asks is "does omm's linked file get
                # recognized," so this calls link_ollama() directly with
                # verify_compat=False rather than trying to also make a
                # placeholder file survive llama-quantize.
                linker.link_ollama(gguf_path, ollama_tag, force=True, verify_compat=False)
                warning = None
            else:
                warning = linker.link_engine(
                    args.engine, gguf_path, repo_id="omm-ci/test-model", ollama_tag=ollama_tag, force=True
                )
        except linker.LinkError as e:
            _fail(f"linking {args.engine!r} raised: {e}")
        if warning:
            print(f"warning during link: {warning}")

        ENGINE_VERIFIERS[args.engine](gguf_path, ollama_tag)


if __name__ == "__main__":
    main()
