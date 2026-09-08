import io

from rich.console import Console
from typer.testing import CliRunner

from omm import cli, fit_ui, registry, theme
from omm.hardware import HardwareInfo, calculate_memory_budget

runner = CliRunner()


def _hw(total=15.5, available=9.8):
    return HardwareInfo(
        os_name="Windows", os_version="11", cpu="Intel(R) Core(TM) Ultra 7 155H",
        ram_total_gb=total, ram_available_gb=available, unified_memory=False,
        gpu_name="Intel(R) Arc(TM) Graphics", vram_total_gb=None, vram_free_gb=None,
    )


def _render(hw, size_gb, width=92):
    budget = calculate_memory_budget(hw)
    required = size_gb * 1.2
    console = Console(file=io.StringIO(), width=width, force_terminal=True, highlight=False)
    console.push_theme(theme.build_rich_theme("dark"))
    console.print(fit_ui.render_fit(
        hw=hw, budget=budget, model_label="mistral-7b-instruct-v0.2.Q4_K_M.gguf",
        size_gb=size_gb, required_gb=required, width=width,
    ))
    return console.file.getvalue()


def test_verdict_fits_when_within_live_budget():
    v = fit_ui.verdict(4.9, calculate_memory_budget(_hw()))
    assert v.status == "fits" and v.role == "success"
    assert "to spare" in v.message


def test_verdict_tight_when_within_cap_but_not_live_budget():
    v = fit_ui.verdict(4.9, calculate_memory_budget(_hw(available=1.2)))
    assert v.status == "tight" and v.role == "warning"
    assert "close other apps" in v.message


def test_verdict_too_big_when_over_install_cap():
    v = fit_ui.verdict(13.0, calculate_memory_budget(_hw()))
    assert v.status == "too_big" and v.role == "error"
    assert "install cap" in v.message


def test_card_reproduces_the_site_rows_and_numbers():
    """The omm.run memory card, verbatim rows: 5.7 in use / 1.6 reserved /
    8.2 budget / 12.4 cap for the 15.5 GB machine in design/FACTS.md."""
    out = _render(_hw(), size_gb=4.07)
    assert "RAM 15.5 GB" in out and "INTEL CORE ULTRA 7 155H" in out and "WINDOWS 11" in out
    assert "In use by other apps" in out and "5.7 GB" in out
    assert "Reserved for apps/OS" in out and "1.6 GB+" in out
    assert "Safe model budget" in out and "8.2 GB" in out
    assert "Install cap - 80% of total RAM" in out and "12.4 GB" in out
    assert "4.07 GB MODEL" in out
    assert "Fits now" in out


def test_bar_segments_are_proportional_and_fill_the_width():
    hw = _hw()
    bar = fit_ui._bar(62, hw, calculate_memory_budget(hw), required_gb=4.9).plain
    assert len(bar) == 62
    # 5.7/15.5 of 62 ≈ 23 cells in use, 1.6/15.5 ≈ 6 reserved, 4.9/15.5 ≈ 20 model
    assert bar.count("█") == 23 + 20
    assert bar.count("▓") == 6
    assert "┊" in bar, "install-cap tick is drawn"


def test_legend_never_prints_a_truncated_label():
    hw = _hw(available=1.0)  # 14.5 in use -> reserved segment too narrow for its label
    legend = fit_ui._legend(40, hw, calculate_memory_budget(hw), required_gb=4.9).plain
    assert "rese" not in legend and "reserved" not in legend
    assert "in use" in legend


def test_card_never_overflows_a_narrow_terminal():
    import re

    out = re.sub(r"\x1b\[[0-9;]*m", "", _render(_hw(), size_gb=4.07, width=40))
    assert all(len(line) <= 40 for line in out.splitlines()), out


def test_fit_command_uses_registry_size_for_installed_models(isolated_omm_home, monkeypatch):
    registry.save_registry({"model.gguf": {"size_bytes": 4 * 1024**3, "linked": {}}})
    monkeypatch.setattr(cli, "scan_hardware", _hw)

    result = runner.invoke(cli.app, ["fit", "model.gguf"])

    assert result.exit_code == 0, result.output
    assert "model.gguf" in result.stdout and "4.00 GB MODEL" in result.stdout


def test_fit_command_json(isolated_omm_home, monkeypatch):
    import json

    registry.save_registry({"model.gguf": {"size_bytes": 4 * 1024**3, "linked": {}}})
    monkeypatch.setattr(cli, "scan_hardware", _hw)

    result = runner.invoke(cli.app, ["--json", "fit", "model.gguf"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["model"] == "model.gguf" and data["status"] == "fits"
    assert data["install_cap_gb"] == 12.4 and data["model_budget_gb"] == 8.25


def test_fit_command_resolves_uninstalled_models_via_remote_size(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "scan_hardware", _hw)
    monkeypatch.setattr(cli, "remote_file_size", lambda provider, repo, filename: 4_368_439_584)

    result = runner.invoke(cli.app, ["fit", "mistral-7b-instruct-q4"])

    assert result.exit_code == 0, result.output
    assert "mistral-7b-instruct-v0.2.Q4_K_M.gguf" in result.stdout
    assert "4.07 GB MODEL" in result.stdout


def test_fit_command_prompts_quant_picker_for_ambiguous_repo(isolated_omm_home, monkeypatch):
    import questionary

    from omm.hub import AmbiguousModelError, ResolvedModel

    monkeypatch.setattr(cli, "scan_hardware", _hw)
    monkeypatch.setattr(cli, "remote_file_size", lambda provider, repo, filename: 4_368_439_584)

    repo_id = "TheBloke/Llama-2-7B-GGUF"
    chosen_filename = "llama-2-7b.Q4_K_M.gguf"
    candidates = ["llama-2-7b.Q2_K.gguf", chosen_filename, "llama-2-7b.Q8_0.gguf"]

    calls = []

    def fake_resolve(name):
        calls.append(name)
        if name == repo_id:
            raise AmbiguousModelError(repo_id, candidates)
        return ResolvedModel(
            url="https://example.com/x.gguf", filename=chosen_filename, repo_id=repo_id, provider="huggingface"
        )

    monkeypatch.setattr(cli, "resolve_model", fake_resolve)
    monkeypatch.setattr(questionary, "select", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ask_select", lambda question: chosen_filename)

    result = runner.invoke(cli.app, ["fit", repo_id])

    assert result.exit_code == 0, result.stdout
    assert calls == [repo_id, f"huggingface:{repo_id}:{chosen_filename}"]
    assert chosen_filename in result.stdout


def test_fit_command_prompts_provider_picker_for_multi_provider_repo(
    isolated_omm_home, monkeypatch
):
    """`omm search` prints bare `org/repo` names (and caches them for numbered
    refs), so `omm fit <that name>` has to survive the repo existing on both
    hubs the same way `omm install` does, instead of exiting 1."""
    import questionary

    from omm.hub import AmbiguousProviderError, ResolvedModel

    monkeypatch.setattr(cli, "scan_hardware", _hw)
    monkeypatch.setattr(cli, "remote_file_size", lambda provider, repo, filename: 4_368_439_584)

    repo_id = "Qwen/Qwen3-8B-GGUF"
    filename = "Qwen3-8B-Q4_K_M.gguf"
    calls = []

    def fake_resolve(name):
        calls.append(name)
        if name == repo_id:
            raise AmbiguousProviderError(repo_id, ["huggingface", "modelscope"])
        return ResolvedModel(
            url="https://example.com/x.gguf",
            filename=filename,
            repo_id=repo_id,
            provider="modelscope",
        )

    monkeypatch.setattr(cli, "resolve_model", fake_resolve)
    monkeypatch.setattr(questionary, "select", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ask_select", lambda question: "modelscope")

    result = runner.invoke(cli.app, ["fit", repo_id])

    assert result.exit_code == 0, result.stdout
    assert calls == [repo_id, f"modelscope:{repo_id}"]
    assert filename in result.stdout


def test_fit_command_walks_provider_picker_then_quant_picker(isolated_omm_home, monkeypatch):
    """Picking a provider can surface a second choice - which quant - and both
    have to resolve in one run of the command."""
    import questionary

    from omm.hub import AmbiguousModelError, AmbiguousProviderError, ResolvedModel

    monkeypatch.setattr(cli, "scan_hardware", _hw)
    monkeypatch.setattr(cli, "remote_file_size", lambda provider, repo, filename: 4_368_439_584)

    repo_id = "Qwen/Qwen3-8B-GGUF"
    chosen_filename = "Qwen3-8B-Q4_K_M.gguf"
    calls = []

    def fake_resolve(name):
        calls.append(name)
        if name == repo_id:
            raise AmbiguousProviderError(repo_id, ["huggingface", "modelscope"])
        if name == f"modelscope:{repo_id}":
            raise AmbiguousModelError(
                repo_id, [chosen_filename, "Qwen3-8B-Q8_0.gguf"], provider="modelscope"
            )
        return ResolvedModel(
            url="https://example.com/x.gguf",
            filename=chosen_filename,
            repo_id=repo_id,
            provider="modelscope",
        )

    monkeypatch.setattr(cli, "resolve_model", fake_resolve)
    monkeypatch.setattr(questionary, "select", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ask_select", lambda question: "modelscope")
    monkeypatch.setattr(cli, "_pick_quant_variant", lambda error: chosen_filename)

    result = runner.invoke(cli.app, ["fit", repo_id])

    assert result.exit_code == 0, result.stdout
    assert calls == [
        repo_id,
        f"modelscope:{repo_id}",
        f"modelscope:{repo_id}:{chosen_filename}",
    ]


def test_fit_command_cancels_cleanly_when_provider_picker_is_escaped(
    isolated_omm_home, monkeypatch
):
    import questionary

    from omm.hub import AmbiguousProviderError

    repo_id = "Qwen/Qwen3-8B-GGUF"

    def fake_resolve(name):
        raise AmbiguousProviderError(repo_id, ["huggingface", "modelscope"])

    monkeypatch.setattr(cli, "resolve_model", fake_resolve)
    monkeypatch.setattr(questionary, "select", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ask_select", lambda question: None)

    result = runner.invoke(cli.app, ["fit", repo_id])

    assert result.exit_code == 0
    assert "Cancelled" in result.stderr


def test_info_points_at_the_card_instead_of_printing_it(isolated_omm_home, monkeypatch):
    """`omm info` used to end in this card, which left the two commands
    saying nearly the same thing. It now describes the model and defers the
    memory verdict to `omm fit` (see tests/test_cli_info.py)."""
    registry.save_registry({"model.gguf": {"size_bytes": 4 * 1024**3, "linked": {}, "sha256": "abc"}})
    monkeypatch.setattr(cli, "scan_hardware", _hw)
    monkeypatch.setattr(cli.linker, "is_engine_installed", lambda key: False)

    result = runner.invoke(cli.app, ["info", "model.gguf"])

    assert result.exit_code == 0, result.output
    assert "Safe model budget" not in result.stdout
    assert "omm fit model.gguf" in result.stdout
