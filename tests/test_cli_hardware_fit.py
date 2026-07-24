from typer.testing import CliRunner

from omm import cli
from omm.hub import ResolvedModel

runner = CliRunner()


def test_search_marks_hardware_unfit_candidates_in_red(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(
        cli.search_mod,
        "local_candidate_pool",
        lambda model_url: [
            {"name": "fits-model", "repo_id": "org/fits", "description": "d"},
            {"name": "too-big-model", "repo_id": "org/big", "description": "d"},
        ],
    )
    monkeypatch.setattr(cli.search_mod, "search_huggingface", lambda query, **kwargs: [])
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: {"trees": [{}]})
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(
        cli.predictor,
        "predict_speed",
        lambda trees, hw, candidate: 0.0 if candidate["name"] == "too-big-model" else 20.0,
    )

    result = runner.invoke(cli.app, ["search", "model"])

    assert result.exit_code == 0, result.stdout
    assert result.stdout.count("predicted not to run on this hardware") == 1
    assert "org/big" in result.stdout
    assert "org/fits" in result.stdout


def test_search_skip_unfit_hides_unfit_candidates(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(
        cli.search_mod,
        "local_candidate_pool",
        lambda model_url: [
            {"name": "llama-fits-model", "repo_id": "org/fits", "description": "d"},
            {"name": "llama-too-big-model", "repo_id": "org/big", "description": "d"},
        ],
    )
    monkeypatch.setattr(cli.search_mod, "search_huggingface", lambda query, **kwargs: [])
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: {"trees": [{}]})
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(
        cli.predictor,
        "predict_speed",
        lambda trees, hw, candidate: 0.0 if candidate["name"] == "llama-too-big-model" else 20.0,
    )

    result = runner.invoke(cli.app, ["search", "model", "--skip-unfit"])

    assert result.exit_code == 0, result.stdout
    assert "org/big" not in result.stdout
    assert "org/fits" in result.stdout
    assert "[1] org/fits" in result.stdout


def test_search_skip_unfit_omits_header_for_all_unfit_family(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(
        cli.search_mod,
        "local_candidate_pool",
        lambda model_url: [
            {"name": "llama-too-big-model", "repo_id": "org/big", "description": "d"},
            {"name": "qwen-fits-model", "repo_id": "org/fits", "description": "d"},
        ],
    )
    monkeypatch.setattr(cli.search_mod, "search_huggingface", lambda query, **kwargs: [])
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: {"trees": [{}]})
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(
        cli.predictor,
        "predict_speed",
        lambda trees, hw, candidate: 0.0 if candidate["name"] == "llama-too-big-model" else 20.0,
    )

    result = runner.invoke(cli.app, ["search", "model", "--skip-unfit"])

    assert result.exit_code == 0, result.stdout
    assert "Llama" not in result.stdout
    assert "==> Qwen" in result.stdout


def test_search_skip_unfit_json_omits_unfit_rows(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(
        cli.search_mod,
        "local_candidate_pool",
        lambda model_url: [
            {"name": "llama-fits-model", "repo_id": "org/fits", "description": "d"},
            {"name": "llama-too-big-model", "repo_id": "org/big", "description": "d"},
        ],
    )
    monkeypatch.setattr(cli.search_mod, "search_huggingface", lambda query, **kwargs: [])
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: {"trees": [{}]})
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(
        cli.predictor,
        "predict_speed",
        lambda trees, hw, candidate: 0.0 if candidate["name"] == "llama-too-big-model" else 20.0,
    )

    result = runner.invoke(cli.app, ["search", "model", "--skip-unfit", "--json"])

    assert result.exit_code == 0, result.stdout
    import json

    rows = json.loads(result.stdout)
    assert len(rows) == 1
    assert rows[0]["ref"].endswith("org/fits") or "org/fits" in rows[0]["ref"]
    assert rows[0]["fits_hardware"] is True


def test_search_skips_hardware_fit_check_without_cached_model(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {"model_url": None})
    monkeypatch.setattr(
        cli.search_mod,
        "local_candidate_pool",
        lambda model_url: [{"name": "some-model", "repo_id": "org/some", "description": "d"}],
    )
    monkeypatch.setattr(cli.search_mod, "search_huggingface", lambda query, **kwargs: [])
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)

    result = runner.invoke(cli.app, ["search", "model"])

    assert result.exit_code == 0, result.stdout
    assert "predicted not to run" not in result.stdout


def _stub_successful_install(monkeypatch, isolated_omm_home):
    filename = "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"
    monkeypatch.setattr(
        cli,
        "resolve_model",
        lambda name: ResolvedModel(url="https://example.com/x.gguf", filename=filename, repo_id="org/repo"),
    )
    monkeypatch.setattr(cli, "download_file", lambda url, dest: dest.write_bytes(b"fake-gguf"))
    monkeypatch.setattr(cli, "sha256_file", lambda dest: "deadbeef")
    monkeypatch.setattr(cli.linker, "is_lmstudio_installed", lambda: False)
    monkeypatch.setattr(cli.linker, "is_ollama_installed", lambda: False)
    return filename


def test_install_warns_and_proceeds_when_user_confirms(isolated_omm_home, monkeypatch):
    _stub_successful_install(monkeypatch, isolated_omm_home)
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: {"trees": [{}]})
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "predict_speed", lambda trees, hw, candidate: 0.0)
    monkeypatch.setattr(
        cli, "_ask_confirm", lambda message, default=False: message.startswith("Install anyway")
    )

    result = runner.invoke(cli.app, ["install", "tinyllama-1.1b-q4"])

    assert result.exit_code == 0, result.stdout
    assert "predicted not to run" in result.stderr
    assert "Installed" in result.stdout


def test_install_aborts_when_declined_after_hardware_warning(isolated_omm_home, monkeypatch):
    _stub_successful_install(monkeypatch, isolated_omm_home)
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: {"trees": [{}]})
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "predict_speed", lambda trees, hw, candidate: 0.0)
    monkeypatch.setattr(cli, "_ask_confirm", lambda message, default=False: False)
    download_calls = []
    monkeypatch.setattr(cli, "download_file", lambda url, dest: download_calls.append(dest))

    result = runner.invoke(cli.app, ["install", "tinyllama-1.1b-q4"])

    assert result.exit_code == 0, result.stdout
    assert "Cancelled" in result.stderr
    assert download_calls == []


def test_install_skip_unfit_flag_bypasses_prompt_without_a_tty(isolated_omm_home, monkeypatch):
    """--skip-unfit must let a script install a batch of models on hardware
    that can't run all of them without ever touching the confirm prompt -
    the prompt would error out immediately without a tty (see P0 fix)."""
    _stub_successful_install(monkeypatch, isolated_omm_home)
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: {"trees": [{}]})
    monkeypatch.setattr(cli, "scan_hardware", lambda: object())
    monkeypatch.setattr(cli.predictor, "predict_speed", lambda trees, hw, candidate: 0.0)
    download_calls = []
    monkeypatch.setattr(cli, "download_file", lambda url, dest: download_calls.append(dest))

    result = runner.invoke(cli.app, ["install", "tinyllama-1.1b-q4", "--skip-unfit"])

    assert result.exit_code == 0, result.stdout
    assert download_calls == []
