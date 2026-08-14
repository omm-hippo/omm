import questionary
from typer.testing import CliRunner

from omm import cli, linker
from omm.hardware import HardwareInfo
from omm.hub import AmbiguousModelError, ResolvedModel

runner = CliRunner()

_HARDWARE = HardwareInfo(
    os_name="Linux",
    os_version="",
    cpu="",
    ram_total_gb=6.0,
    ram_available_gb=6.0,
    unified_memory=False,
    gpu_name=None,
    vram_total_gb=None,
    vram_free_gb=None,
)


def _stub_download_and_links(monkeypatch):
    def fake_download(url, dest, **_kw):
        dest.write_bytes(b"fake-gguf")

    monkeypatch.setattr(cli, "download_file", fake_download)
    monkeypatch.setattr(cli, "sha256_file", lambda dest: "deadbeef")
    monkeypatch.setattr(linker, "is_lmstudio_installed", lambda: False)
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: False)


def test_install_with_repo_only_prompts_quant_and_recurses_with_choice(isolated_omm_home, monkeypatch):
    _stub_download_and_links(monkeypatch)
    repo_id = "TheBloke/Llama-2-7B-GGUF"
    chosen_filename = "llama-2-7b.Q4_K_M.gguf"
    candidates = ["llama-2-7b.Q2_K.gguf", chosen_filename, "llama-2-7b.Q8_0.gguf"]

    calls = []

    def fake_resolve(name):
        calls.append(name)
        if name == repo_id:
            raise AmbiguousModelError(repo_id, candidates)
        return ResolvedModel(url="https://example.com/x.gguf", filename=chosen_filename, repo_id=repo_id)

    monkeypatch.setattr(cli, "resolve_model", fake_resolve)
    monkeypatch.setattr(cli, "scan_hardware", lambda: _HARDWARE)
    # questionary.select(...) is evaluated eagerly as an argument to
    # _ask_select, so it must be stubbed too - constructing a real Question
    # tries to open a console, which CI runners (esp. Windows) don't have.
    monkeypatch.setattr(questionary, "select", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ask_select", lambda question: chosen_filename)

    result = runner.invoke(cli.app, ["install", repo_id])

    assert result.exit_code == 0, result.stdout
    assert calls == [repo_id, f"huggingface:{repo_id}:{chosen_filename}"]
    assert "Installed" in result.stdout


def test_install_cancels_cleanly_when_quant_prompt_is_escaped(isolated_omm_home, monkeypatch):
    repo_id = "TheBloke/Llama-2-7B-GGUF"
    candidates = ["llama-2-7b.Q4_K_M.gguf"]

    def fake_resolve(name):
        raise AmbiguousModelError(repo_id, candidates)

    monkeypatch.setattr(cli, "resolve_model", fake_resolve)
    monkeypatch.setattr(cli, "scan_hardware", lambda: _HARDWARE)
    monkeypatch.setattr(questionary, "select", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ask_select", lambda question: None)

    result = runner.invoke(cli.app, ["install", repo_id])

    assert result.exit_code == 0
    assert "Cancelled" in result.stderr


def test_quant_picker_marks_predicted_fastest_variant_green(isolated_omm_home, monkeypatch):
    # _HARDWARE has ram_total_gb=6.0 -> model budget is min(6.0*0.8, 6.0-2.0) = 4.0GB
    # (see hardware.calculate_memory_budget). A 3B model at Q4 needs
    # 3 * 4 / 8 * 1.2 = 1.8GB, comfortably under that budget so both variants
    # land in the same "fits" quant_bits=4.0 tier.
    repo_id = "TheBloke/OpenLlama-3B-GGUF"
    candidates = ["openllama-3b.Q4_K_S.gguf", "openllama-3b.Q4_K_M.gguf"]

    def fake_resolve(name):
        raise AmbiguousModelError(repo_id, candidates)

    monkeypatch.setattr(cli, "resolve_model", fake_resolve)
    monkeypatch.setattr(cli, "scan_hardware", lambda: _HARDWARE)
    monkeypatch.setattr(
        cli.predictor, "load_cached_model", lambda: {"trees": ["stub-tree"]}
    )

    def fake_predict_speed(trees, hw, candidate):
        # Q4_K_M "wins" its tier, Q4_K_S loses it.
        return 12.0 if candidate["filename"] == "openllama-3b.Q4_K_M.gguf" else 5.0

    monkeypatch.setattr(cli.predictor, "predict_speed", fake_predict_speed)

    captured_choices = {}

    def fake_select(message, choices):
        captured_choices["choices"] = choices
        return None

    monkeypatch.setattr(questionary, "select", fake_select)
    monkeypatch.setattr(cli, "_ask_select", lambda question: None)

    result = runner.invoke(cli.app, ["install", repo_id])

    assert result.exit_code == 0
    titles_by_filename = {c.value: c.title for c in captured_choices["choices"]}
    fast_title = titles_by_filename["openllama-3b.Q4_K_M.gguf"]
    slow_title = titles_by_filename["openllama-3b.Q4_K_S.gguf"]
    assert fast_title == [
        (
            "fg:green bold",
            "openllama-3b.Q4_K_M.gguf  (✓ fits, ~1.8GB needed, predicted fastest)",
        )
    ]
    assert isinstance(slow_title, str)
    assert "predicted fastest" not in slow_title


def test_quant_picker_passes_repo_parameter_count_to_predictor(isolated_omm_home, monkeypatch):
    repo_id = "org/unparseable"
    candidates = ["artifact.Q4.gguf"]
    received_candidates = []

    monkeypatch.setattr(cli, "resolve_model", lambda name: (_ for _ in ()).throw(
        AmbiguousModelError(repo_id, candidates, param_count_b=3.0)
    ))
    monkeypatch.setattr(cli, "scan_hardware", lambda: _HARDWARE)
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: {"trees": ["stub-tree"]})
    monkeypatch.setattr(
        cli.predictor,
        "predict_speed",
        lambda trees, hw, candidate: received_candidates.append(candidate) or 1.0,
    )
    monkeypatch.setattr(questionary, "select", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ask_select", lambda question: None)

    result = runner.invoke(cli.app, ["install", repo_id])

    assert result.exit_code == 0
    assert received_candidates == [
        {"repo_id": repo_id, "filename": "artifact.Q4.gguf", "parameter_count_b": 3.0}
    ]


def test_quant_picker_no_green_marks_when_predictor_model_uncached(isolated_omm_home, monkeypatch):
    repo_id = "TheBloke/Llama-2-7B-GGUF"
    candidates = ["llama-2-7b.Q4_K_M.gguf"]

    def fake_resolve(name):
        raise AmbiguousModelError(repo_id, candidates)

    monkeypatch.setattr(cli, "resolve_model", fake_resolve)
    monkeypatch.setattr(cli, "scan_hardware", lambda: _HARDWARE)
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)

    captured_choices = {}

    def fake_select(message, choices):
        captured_choices["choices"] = choices
        return None

    monkeypatch.setattr(questionary, "select", fake_select)
    monkeypatch.setattr(cli, "_ask_select", lambda question: None)

    result = runner.invoke(cli.app, ["install", repo_id])

    assert result.exit_code == 0
    (choice,) = captured_choices["choices"]
    assert isinstance(choice.title, str)
    assert "predicted fastest" not in choice.title
