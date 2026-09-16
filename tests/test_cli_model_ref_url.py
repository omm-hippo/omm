"""CLI-level cover for issue #340: a HuggingFace/ModelScope page URL pasted
into any command that takes a model name, and the message a safetensors-only
repo gets instead of "not found"."""

from typer.testing import CliRunner

from omm import cli, search as search_mod
from omm.providers import huggingface, modelscope
from omm.providers.base import ModelResolutionError

runner = CliRunner()

MINICPM_URL = "https://huggingface.co/openbmb/MiniCPM5-2B"
GGUF_BUILDS = ["bartowski/MiniCPM5-2B-GGUF", "mradermacher/MiniCPM5-2B-i1-GGUF"]


def _repo_exists_without_gguf(monkeypatch, *, quantizations=()):
    """openbmb/MiniCPM5-2B as HF really serves it: a real repo, safetensors
    only, with a quantization tree hanging off it."""
    monkeypatch.setattr(huggingface, "fetch_repo_files", lambda repo_id: ([], None))
    monkeypatch.setattr(
        huggingface, "fetch_gguf_quantizations", lambda repo_id, limit=3: list(quantizations)
    )

    def _missing(repo_id):
        raise ModelResolutionError(f"not found: {repo_id}", kind="not_found")

    monkeypatch.setattr(modelscope, "fetch_repo_files", _missing)


def _repo_with_one_gguf(monkeypatch):
    monkeypatch.setattr(
        huggingface, "fetch_repo_files", lambda repo_id: (["model-Q4_K_M.gguf"], None)
    )


def test_install_from_a_pasted_url_reports_the_missing_gguf_not_a_typo(
    isolated_omm_home, monkeypatch
):
    _repo_exists_without_gguf(monkeypatch, quantizations=GGUF_BUILDS)

    result = runner.invoke(cli.app, ["install", MINICPM_URL])

    assert result.exit_code == 1
    assert "'openbmb/MiniCPM5-2B' exists on HuggingFace but has no .gguf file." in result.stderr
    assert "not found on HuggingFace or ModelScope" not in result.stderr
    assert "safe relative path ending in .gguf" not in result.stderr
    assert "Did you mean one of these?" in result.stderr
    for build in GGUF_BUILDS:
        assert build in result.stderr


def test_install_of_a_bare_repo_without_gguf_falls_back_to_a_search_hint(
    isolated_omm_home, monkeypatch
):
    _repo_exists_without_gguf(monkeypatch)
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(search_mod, "local_candidate_pool", lambda model_url, **kwargs: [])
    monkeypatch.setattr(search_mod, "search_huggingface", lambda query, **kwargs: [])

    result = runner.invoke(cli.app, ["install", "openbmb/MiniCPM5-2B"])

    assert result.exit_code == 1
    assert "has no .gguf file." in result.stderr
    stderr_flat = " ".join(result.stderr.split())
    assert "omm search MiniCPM5-2B GGUF" in stderr_flat


def test_info_from_a_pasted_url_names_the_repo_instead_of_not_installed(
    isolated_omm_home, monkeypatch
):
    _repo_exists_without_gguf(monkeypatch, quantizations=GGUF_BUILDS)

    result = runner.invoke(cli.app, ["info", MINICPM_URL])

    assert result.exit_code == 1
    assert "is not installed via omm" not in result.stderr
    assert "'openbmb/MiniCPM5-2B' exists on HuggingFace but has no .gguf file." in result.stderr
    assert "bartowski/MiniCPM5-2B-GGUF" in result.stderr


def test_info_still_says_not_installed_for_a_reference_nothing_can_resolve(
    isolated_omm_home, monkeypatch
):
    _repo_with_one_gguf(monkeypatch)

    result = runner.invoke(cli.app, ["info", "nothing-here.gguf"])

    assert result.exit_code == 1
    assert "is not installed via omm" in result.stderr


def test_info_from_a_pasted_url_shows_the_remote_repo(isolated_omm_home, monkeypatch):
    _repo_with_one_gguf(monkeypatch)
    monkeypatch.setattr(cli, "fetch_repo_metadata", lambda provider, repo_id: {"author": "org"})

    result = runner.invoke(cli.app, ["--json", "info", "https://huggingface.co/org/repo"])

    assert result.exit_code == 0, result.stderr
    assert '"repo_id": "org/repo"' in result.stdout
    assert '"filename": "model-Q4_K_M.gguf"' in result.stdout


def test_fit_from_a_pasted_url_reports_the_missing_gguf(isolated_omm_home, monkeypatch):
    _repo_exists_without_gguf(monkeypatch, quantizations=GGUF_BUILDS)

    result = runner.invoke(cli.app, ["fit", MINICPM_URL])

    assert result.exit_code == 1
    assert "'openbmb/MiniCPM5-2B' exists on HuggingFace but has no .gguf file." in result.stderr
    assert "safe relative path ending in .gguf" not in result.stderr
    assert "bartowski/MiniCPM5-2B-GGUF" in result.stderr


def test_search_from_a_pasted_url_searches_for_the_repo_name(monkeypatch):
    seen: dict = {}

    def _record(query, **kwargs):
        seen["query"] = query
        return [{"name": "bartowski/MiniCPM5-2B-GGUF", "repo_id": "bartowski/MiniCPM5-2B-GGUF"}]

    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(search_mod, "local_candidate_pool", lambda model_url, **kwargs: [])
    monkeypatch.setattr(search_mod, "search_huggingface", _record)
    monkeypatch.setattr(search_mod, "search_modelscope", lambda query, **kwargs: [])

    result = runner.invoke(cli.app, ["search", MINICPM_URL])

    assert result.exit_code == 0, result.stderr
    assert seen["query"] == "MiniCPM5-2B"
    assert "bartowski/MiniCPM5-2B-GGUF" in result.stdout
