import importlib.util
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_trusted_head.py"


def _run(args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, timeout=10, **kwargs)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _load_verifier():
    spec = importlib.util.spec_from_file_location("trusted_head_verifier", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_verifier_is_stdlib_only_and_accepts_a_trusted_signed_commit(tmp_path):
    key = tmp_path / "id_ed25519"
    _run(["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q"])
    anchor = tmp_path / "allowed_signers"
    anchor.write_text(f"test@example.com {key.with_suffix('.pub').read_text().strip()}\n")

    repo = tmp_path / "candidate"
    repo.mkdir()
    _run(["git", "init", "-q"], cwd=repo)
    _run(["git", "config", "user.email", "test@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "test"], cwd=repo)
    _run(["git", "config", "gpg.format", "ssh"], cwd=repo)
    _run(["git", "config", "user.signingkey", str(key.with_suffix(".pub"))], cwd=repo)
    (repo / "file.txt").write_text("trusted\n")
    _run(["git", "add", "file.txt"], cwd=repo)
    _run(["git", "commit", "-q", "-S", "-m", "trusted"], cwd=repo)
    commit = _run(["git", "rev-parse", "HEAD"], cwd=repo).strip()

    verifier = _load_verifier()
    script_text = SCRIPT.read_text(encoding="utf-8")
    assert "from omm" not in script_text
    assert "import omm" not in script_text
    assert "sys.path.insert" not in script_text
    assert verifier.verify(repo, commit, anchor)[0]


def test_verifier_accepts_relative_repo_and_anchor_from_parent_cwd(tmp_path, monkeypatch):
    parent = tmp_path / "parent"
    repo = parent / "verifier"
    anchor = repo / "src" / "omm" / "trust" / "allowed_signers"
    key = tmp_path / "id_ed25519"
    parent.mkdir()
    _run(["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q"])
    anchor.parent.mkdir(parents=True)
    anchor.write_text(f"test@example.com {key.with_suffix('.pub').read_text().strip()}\n")
    repo.mkdir(exist_ok=True)
    _run(["git", "init", "-q"], cwd=repo)
    _run(["git", "config", "user.email", "test@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "test"], cwd=repo)
    _run(["git", "config", "gpg.format", "ssh"], cwd=repo)
    _run(["git", "config", "user.signingkey", str(key.with_suffix(".pub"))], cwd=repo)
    (repo / "file.txt").write_text("trusted\n")
    _run(["git", "add", "file.txt"], cwd=repo)
    _run(["git", "commit", "-q", "-S", "-m", "trusted"], cwd=repo)
    commit = _run(["git", "rev-parse", "HEAD"], cwd=repo).strip()

    monkeypatch.chdir(parent)
    ok, message = _load_verifier().verify(
        Path("verifier"), commit, Path("verifier/src/omm/trust/allowed_signers")
    )

    assert ok, message


def test_verifier_rejects_unsigned_merge_with_trusted_second_parent(tmp_path):
    key = tmp_path / "id_ed25519"
    _run(["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q"])
    anchor = tmp_path / "allowed_signers"
    anchor.write_text(f"test@example.com {key.with_suffix('.pub').read_text().strip()}\n")
    repo = tmp_path / "candidate"
    repo.mkdir()
    _run(["git", "init", "-q"], cwd=repo)
    _run(["git", "config", "user.email", "test@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "test"], cwd=repo)
    _run(["git", "config", "gpg.format", "ssh"], cwd=repo)
    (repo / "file.txt").write_text("base\n")
    _run(["git", "add", "file.txt"], cwd=repo)
    _run(["git", "commit", "-q", "-m", "base"], cwd=repo)
    branch = _run(["git", "branch", "--show-current"], cwd=repo).strip()
    _run(["git", "checkout", "-q", "-b", "trusted"], cwd=repo)
    _run(["git", "config", "user.signingkey", str(key.with_suffix(".pub"))], cwd=repo)
    (repo / "trusted.txt").write_text("trusted\n")
    _run(["git", "add", "trusted.txt"], cwd=repo)
    _run(["git", "commit", "-q", "-S", "-m", "trusted"], cwd=repo)
    _run(["git", "checkout", "-q", branch], cwd=repo)
    (repo / "malicious.txt").write_text("malicious\n")
    _run(["git", "add", "malicious.txt"], cwd=repo)
    _run(["git", "commit", "-q", "-m", "malicious"], cwd=repo)
    _run(["git", "merge", "-q", "--no-ff", "--no-gpg-sign", "-m", "unsigned merge", "trusted"], cwd=repo)
    merge = _run(["git", "rev-parse", "HEAD"], cwd=repo).strip()

    ok, message = _load_verifier().verify(repo, merge, anchor)

    assert not ok
    assert merge[:7] in message


def test_verifier_rejects_symbolic_commit_name_before_git_resolution(tmp_path):
    verifier = _load_verifier()

    target, error = verifier._resolve_commit(tmp_path, "HEAD")

    assert target is None
    assert "exact 40- or 64-hex" in error
