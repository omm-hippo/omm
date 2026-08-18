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
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

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
    return subprocess.run(args, capture_output=True, text=True, timeout=30)


def verify_ollama(gguf_path: Path, ollama_tag: str) -> None:
    result = _run(["ollama", "list"])
    if result.returncode != 0:
        _fail(f"`ollama list` failed: {result.stderr}")
    if ollama_tag not in result.stdout:
        _fail(f"`ollama list` does not show {ollama_tag!r}:\n{result.stdout}")
    result = _run(["ollama", "show", ollama_tag])
    if result.returncode != 0:
        _fail(f"`ollama show {ollama_tag}` failed: {result.stderr}")
    print(f"OK: ollama recognizes {ollama_tag!r} ({result.stdout.splitlines()[0]!r})")


def verify_lmstudio(gguf_path: Path, ollama_tag: str) -> None:
    result = _run(["lms", "ls", "--json"])
    if result.returncode != 0:
        _fail(f"`lms ls --json` failed: {result.stderr}")
    try:
        models = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        _fail(f"`lms ls --json` did not return JSON: {e}\n{result.stdout}")
    paths = [m.get("path") or m.get("modelKey") or "" for m in models]
    if not any(gguf_path.name in p or str(gguf_path) in p for p in paths):
        _fail(f"`lms ls` does not list {gguf_path.name!r} among: {paths}")
    print(f"OK: lms recognizes {gguf_path.name!r}")


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
    manifest = json.loads(manifest_path.read_text())
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

    for _ in range(3):
        if _run(["lms", "ls", "--json"]).returncode == 0:
            return
        time.sleep(1)
    subprocess.Popen(["lms", "server", "start", "--no-gui"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        if _run(["lms", "ls", "--json"]).returncode == 0:
            return
        time.sleep(1)
    _fail("lms CLI never became ready")


ENSURE_READY = {
    "ollama": _ensure_ollama_ready,
    "lmstudio": _ensure_lmstudio_ready,
}


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

    with tempfile.TemporaryDirectory() as tmp:
        gguf_path = Path(tmp) / "omm-ci-test-model.gguf"
        build_minimal_gguf(gguf_path)
        ollama_tag = "omm-ci-test"

        try:
            warning = linker.link_engine(
                args.engine, gguf_path, repo_id="omm-ci/test-model", ollama_tag=ollama_tag, force=True
            )
        except linker.LinkError as e:
            _fail(f"link_engine({args.engine!r}) raised: {e}")
        if warning:
            print(f"warning during link: {warning}")

        ENGINE_VERIFIERS[args.engine](gguf_path, ollama_tag)


if __name__ == "__main__":
    main()
