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


def test_install_force_flag_reaches_install_impl_through_ambiguous_quant_recursion(
    isolated_omm_home, monkeypatch
):
    # `install --force` on an ambiguous repo re-enters install() recursively
    # once the quant is picked (see the `install(...)` call inside the
    # AmbiguousModelError branch) - force has to survive that re-entry and
    # still reach _install_impl. Only _install_impl's direct-call test
    # covered force=True before this (see #81).
    repo_id = "TheBloke/Llama-2-7B-GGUF"
    chosen_filename = "llama-2-7b.Q4_K_M.gguf"
    candidates = ["llama-2-7b.Q2_K.gguf", chosen_filename]

    def fake_resolve(name):
        if name == repo_id:
            raise AmbiguousModelError(repo_id, candidates)
        return ResolvedModel(url="https://example.com/x.gguf", filename=chosen_filename, repo_id=repo_id)

    monkeypatch.setattr(cli, "resolve_model", fake_resolve)
    monkeypatch.setattr(cli, "scan_hardware", lambda: _HARDWARE)
    monkeypatch.setattr(questionary, "select", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ask_select", lambda question: chosen_filename)

    install_impl_calls = []

    def fake_install_impl(resolved, **kwargs):
        install_impl_calls.append(kwargs)
        return cli.InstallOutcome(filename=resolved.filename, repo_id=resolved.repo_id, linked={})

    monkeypatch.setattr(cli, "_install_impl", fake_install_impl)

    result = runner.invoke(cli.app, ["install", repo_id, "--force"])

    assert result.exit_code == 0, result.stdout
    assert len(install_impl_calls) == 1
    assert install_impl_calls[0]["force"] is True


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
    titles_by_filename = {
        c.value: c.title
        for c in captured_choices["choices"]
        if not isinstance(c, questionary.Separator)
    }
    fast_title = titles_by_filename["openllama-3b.Q4_K_M.gguf"]
    slow_title = titles_by_filename["openllama-3b.Q4_K_S.gguf"]
    assert fast_title == [
        # "~1.8GB" padded to the "Memory needed" header's width, then the tag.
        ("fg:green bold", "Q4_K_M  ✓ fits  " + "~1.8GB".ljust(len("Memory needed")) + "  predicted fastest")
    ]
    assert isinstance(slow_title, str)
    assert "predicted fastest" not in slow_title


def test_quant_picker_shows_quant_label_columns_under_a_header(isolated_omm_home, monkeypatch):
    # #364: the picker used to repeat the full model filename on every row
    # followed by a prose note. Rows are now just the quant label and
    # aligned fit / memory columns, under one non-selectable header row.
    repo_id = "TheBloke/Llama-2-7B-GGUF"
    candidates = ["llama-2-7b.Q2_K.gguf", "llama-2-7b.Q4_K_M.gguf", "llama-2-7b.Q8_0.gguf"]

    monkeypatch.setattr(
        cli, "resolve_model", lambda name: (_ for _ in ()).throw(AmbiguousModelError(repo_id, candidates))
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: _HARDWARE)
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    captured = {}

    def fake_select(message, choices):
        captured["message"] = message
        captured["choices"] = choices
        return None

    monkeypatch.setattr(questionary, "select", fake_select)
    monkeypatch.setattr(cli, "_ask_select", lambda question: None)

    result = runner.invoke(cli.app, ["install", repo_id])

    assert result.exit_code == 0
    header, *choices = captured["choices"]
    assert isinstance(header, questionary.Separator)
    assert header.line.split() == ["Quant", "Fit", "Memory", "needed"]
    assert repo_id in captured["message"]
    titles = [c.title for c in choices]
    # Best-fitting, highest-quality first (the default cursor position);
    # the 7B Q8_0 needs ~8.4GB of a 4GB budget so it sorts last.
    assert [c.value for c in choices] == [
        "llama-2-7b.Q4_K_M.gguf",
        "llama-2-7b.Q2_K.gguf",
        "llama-2-7b.Q8_0.gguf",
    ]
    assert [t.split()[0] for t in titles] == ["Q4_K_M", "Q2_K", "Q8_0"]
    assert all("llama-2-7b" not in t for t in titles)
    # Columns line up: every fit cell and memory cell starts at the same
    # offset as its header.
    fit_col = header.line.index("Fit")
    mem_col = header.line.index("Memory")
    for title in titles:
        assert title[fit_col - 2 : fit_col] == "  "
        assert title[fit_col] != " "
        assert title[mem_col] == "~"


def test_quant_picker_keeps_filename_when_label_is_ambiguous(isolated_omm_home, monkeypatch):
    # Two files sharing one quant, or a file with no recognizable quant,
    # would be indistinguishable by label alone - those keep the filename.
    repo_id = "org/repo"
    candidates = ["base.Q4_K_M.gguf", "instruct.Q4_K_M.gguf", "mystery.gguf", "base.Q8_0.gguf"]

    monkeypatch.setattr(
        cli, "resolve_model", lambda name: (_ for _ in ()).throw(AmbiguousModelError(repo_id, candidates))
    )
    monkeypatch.setattr(cli, "scan_hardware", lambda: _HARDWARE)
    monkeypatch.setattr(cli.predictor, "load_cached_model", lambda: None)
    monkeypatch.setattr(cli, "remote_file_size", lambda *a, **k: None)
    captured = {}

    def fake_select(message, choices):
        captured["choices"] = choices
        return None

    monkeypatch.setattr(questionary, "select", fake_select)
    monkeypatch.setattr(cli, "_ask_select", lambda question: None)

    result = runner.invoke(cli.app, ["install", repo_id])

    assert result.exit_code == 0
    first_cell = {
        c.value: c.title.split()[0]
        for c in captured["choices"]
        if not isinstance(c, questionary.Separator)
    }
    assert first_cell == {
        "base.Q4_K_M.gguf": "base.Q4_K_M.gguf",
        "instruct.Q4_K_M.gguf": "instruct.Q4_K_M.gguf",
        "mystery.gguf": "mystery.gguf",
        "base.Q8_0.gguf": "Q8_0",
    }


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
    header, choice = captured_choices["choices"]
    assert isinstance(header, questionary.Separator)
    assert isinstance(choice.title, str)
    assert "predicted fastest" not in choice.title
