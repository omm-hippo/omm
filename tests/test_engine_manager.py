from dataclasses import asdict
import json
import subprocess
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from omm import cli, config, engine_manager as manager
from omm.engines import RuntimeHealth
from omm.engine_packages import operation_lock


def brew_receipt(version="1.0"):
    return manager.PackageReceipt("brew", "/usr/local/bin/brew", "ollama-app", version)


def test_brew_receipt_uses_exact_local_package_identity(monkeypatch):
    monkeypatch.setattr(manager, "platform", SimpleNamespace(system=lambda: "Darwin"))
    monkeypatch.setattr(manager.shutil, "which", lambda name: "/usr/local/bin/brew")
    seen = []
    def query(args):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, "not-ollama-app 9\nollama-app 1.2.3\n", "")
    monkeypatch.setattr(manager, "_query", query)
    assert manager.package_receipt("ollama").version == "1.2.3"
    assert seen == [["/usr/local/bin/brew", "list", "--cask", "--versions", "ollama-app"],
                    ["/usr/local/bin/brew", "list", "--formula", "--versions", "ollama"]]


def test_brew_cask_query_by_name_survives_a_broken_bare_cask_listing(monkeypatch):
    # Regression for a Homebrew regression where bare `brew list --cask --versions`
    # (no name) fails outright, even though the same command with a name works.
    monkeypatch.setattr(manager, "platform", SimpleNamespace(system=lambda: "Darwin"))
    monkeypatch.setattr(manager.shutil, "which", lambda name: "/usr/local/bin/brew")
    def query(args):
        if args[-1] == "ollama-app":
            return subprocess.CompletedProcess(args, 0, "ollama-app 1.2.3\n", "")
        return subprocess.CompletedProcess(args, 1, "", "Error: not installed\n")
    monkeypatch.setattr(manager, "_query", query)
    receipt = manager.package_receipt("ollama")
    assert receipt.kind == "cask" and receipt.version == "1.2.3"


def test_brew_named_query_treats_a_missing_package_as_absent_not_an_error(monkeypatch):
    monkeypatch.setattr(manager, "platform", SimpleNamespace(system=lambda: "Darwin"))
    monkeypatch.setattr(manager.shutil, "which", lambda name: "/usr/local/bin/brew")
    monkeypatch.setattr(manager, "_query", lambda args: subprocess.CompletedProcess(args, 1, "", "Error: not installed\n"))
    assert manager.package_receipt("ollama") is None


def test_brew_formula_install_is_managed_as_a_formula_not_an_app(monkeypatch):
    monkeypatch.setattr(manager, "platform", SimpleNamespace(system=lambda: "Darwin"))
    monkeypatch.setattr(manager.shutil, "which", lambda name: "/brew")
    monkeypatch.setattr(manager, "_query", lambda args: subprocess.CompletedProcess(args, 0, "ollama 0.30.10\n" if "--formula" in args else "", ""))
    receipt = manager.package_receipt("ollama")
    assert receipt.kind == "formula" and receipt.package_id == "ollama"
    assert manager.command_for("update", receipt) == ["/brew", "upgrade", "--formula", "ollama"]


def test_brew_does_not_arbitrarily_choose_between_formula_and_app(monkeypatch):
    monkeypatch.setattr(manager, "platform", SimpleNamespace(system=lambda: "Darwin"))
    monkeypatch.setattr(manager.shutil, "which", lambda name: "/brew")
    monkeypatch.setattr(manager, "_query", lambda args: subprocess.CompletedProcess(args, 0, "ollama 0.30.10\n" if "--formula" in args else "ollama-app 0.30.10\n", ""))
    with pytest.raises(manager.EngineManagementError, match="Both"):
        manager.package_receipt("ollama")


def test_probe_failure_is_not_the_same_as_an_absent_package(monkeypatch):
    monkeypatch.setattr(manager, "platform", SimpleNamespace(system=lambda: "Darwin"))
    monkeypatch.setattr(manager.shutil, "which", lambda name: "/brew")
    monkeypatch.setattr(manager, "_query", lambda args: None)
    with pytest.raises(manager.EngineManagementError, match="Could not read"):
        manager.package_receipt("ollama")


@pytest.mark.parametrize("code", [0x8A150014, -1978335212])
def test_winget_only_treats_documented_missing_package_code_as_absent(code, monkeypatch):
    monkeypatch.setattr(manager, "platform", SimpleNamespace(system=lambda: "Windows"))
    monkeypatch.setattr(manager.shutil, "which", lambda name: "winget.exe")
    monkeypatch.setattr(manager, "_query", lambda args: subprocess.CompletedProcess(args, code, "", ""))
    assert manager.package_receipt("ollama") is None


def test_winget_parses_identity_independently_of_localized_headers(monkeypatch):
    monkeypatch.setattr(manager, "platform", SimpleNamespace(system=lambda: "Windows"))
    monkeypatch.setattr(manager.shutil, "which", lambda name: "winget.exe")
    monkeypatch.setattr(manager, "_query", lambda args: subprocess.CompletedProcess(args, 0, "이름  ID  버전\nOllama  Ollama.Ollama  0.12.3\n", ""))
    receipt = manager.package_receipt("ollama")
    assert receipt.package_id == "Ollama.Ollama" and receipt.version == "0.12.3"


def test_flatpak_refuses_ambiguous_installations(monkeypatch):
    monkeypatch.setattr(manager, "platform", SimpleNamespace(system=lambda: "Linux"))
    monkeypatch.setattr(manager.shutil, "which", lambda name: "/flatpak")
    monkeypatch.setattr(manager, "_query", lambda args: subprocess.CompletedProcess(args, 0, "ai.jan.Jan\t1.0\tuser\nai.jan.Jan\t1.0\tsystem\n", ""))
    with pytest.raises(manager.EngineManagementError, match="Multiple"):
        manager.package_receipt("jan")


@pytest.mark.parametrize("receipt", [brew_receipt(), manager.PackageReceipt("winget", "winget.exe", "Ollama.Ollama", "1"), manager.PackageReceipt("flatpak", "/flatpak", "ai.jan.Jan", "1", "user")])
def test_actions_target_one_exact_package_and_never_purge(receipt):
    for action in ("update", "uninstall"):
        command = manager.command_for(action, receipt)
        assert receipt.package_id in command
        assert not {"--all", "--force", "--purge", "--zap", "--delete-data"} & set(command)


def test_plan_does_not_guess_how_to_remove_an_external_app(monkeypatch):
    monkeypatch.setattr(manager, "package_receipt", lambda key: None)
    monkeypatch.setattr(manager.linker, "is_engine_installed", lambda key: True)
    with pytest.raises(manager.EngineManagementError, match="Cannot identify"):
        manager.plan_action("ollama", "uninstall")


def test_execute_rechecks_package_before_and_after_change(monkeypatch, isolated_omm_home):
    receipts = iter([brew_receipt(), brew_receipt(), brew_receipt("2.0")])
    monkeypatch.setattr(manager, "package_receipt", lambda key: next(receipts))
    monkeypatch.setattr(manager.linker, "is_engine_installed", lambda key: True)
    calls = []
    monkeypatch.setattr(manager, "_run_action", lambda command, on_output: calls.append(command) or 0)
    model = config.MODELS_DIR / "keep.gguf"
    model.write_bytes(b"unchanged")
    plan = manager.plan_action("ollama", "update")
    result = manager.execute_action(plan, on_output=lambda line: None)
    assert result["package"]["version"] == "2.0"
    assert len(calls) == 1
    assert model.read_bytes() == b"unchanged"


def test_package_changed_after_preview_is_not_executed(monkeypatch):
    receipts = iter([brew_receipt(), brew_receipt("2.0")])
    monkeypatch.setattr(manager, "package_receipt", lambda key: next(receipts))
    monkeypatch.setattr(manager, "_run_action", lambda *a: pytest.fail("must not execute"))
    plan = manager.plan_action("ollama", "uninstall")
    with pytest.raises(manager.EngineManagementError, match="changed"):
        manager.execute_action(plan, on_output=lambda line: None)


def test_native_success_without_removal_is_a_failure(monkeypatch):
    monkeypatch.setattr(manager, "package_receipt", lambda key: brew_receipt())
    monkeypatch.setattr(manager.linker, "is_engine_installed", lambda key: True)
    monkeypatch.setattr(manager, "_run_action", lambda *a: 0)
    with pytest.raises(manager.EngineManagementError, match="still installed"):
        manager.execute_action(manager.plan_action("ollama", "uninstall"), on_output=lambda line: None)


def test_shared_lock_blocks_install_while_another_change_is_in_progress(monkeypatch):
    monkeypatch.setattr(manager.linker, "_install_engine_unlocked", lambda *a, **k: pytest.fail("must not install"))
    with operation_lock("ollama"):
        result = manager.linker.install_engine("ollama")
    assert result.status == "failed"


def test_winget_no_update_is_success_only_after_state_is_rechecked(monkeypatch):
    receipt = manager.PackageReceipt("winget", "winget.exe", "Ollama.Ollama", "1.0")
    monkeypatch.setattr(manager, "package_receipt", lambda key: receipt)
    monkeypatch.setattr(manager.linker, "is_engine_installed", lambda key: True)
    monkeypatch.setattr(manager, "_run_action", lambda *a: 0x8A15002B)
    result = manager.execute_action(manager.plan_action("ollama", "update"), on_output=lambda line: None)
    assert result["status"] == "unchanged"


def test_failure_to_query_after_uninstall_does_not_claim_success(monkeypatch):
    calls = []
    def receipt(key):
        calls.append(key)
        if len(calls) == 3:
            raise manager.EngineManagementError("verification unavailable")
        return brew_receipt()
    monkeypatch.setattr(manager, "package_receipt", receipt)
    monkeypatch.setattr(manager, "_run_action", lambda *a: 0)
    with pytest.raises(manager.EngineManagementError, match="verification unavailable"):
        manager.execute_action(manager.plan_action("ollama", "uninstall"), on_output=lambda line: None)


def test_installer_failure_reports_detected_application_honestly(monkeypatch):
    monkeypatch.setattr(manager.linker, "platform", SimpleNamespace(system=lambda: "Darwin"))
    monkeypatch.setattr(manager.linker.shutil, "which", lambda name: "/brew")
    monkeypatch.setattr(manager.linker, "_stream_subprocess", lambda *a: 1)
    monkeypatch.setattr(manager.linker, "is_ollama_installed", lambda: True)
    result = manager.linker.install_engine("ollama")
    assert result.status == "failed"
    assert "still detected" in result.message


def test_status_separates_installed_app_from_stopped_api(monkeypatch):
    monkeypatch.setattr(manager, "package_receipt", lambda key: None)
    monkeypatch.setattr(manager.linker, "is_engine_installed", lambda key: True)
    monkeypatch.setattr(manager, "OllamaAdapter", lambda: SimpleNamespace(health=lambda: RuntimeHealth(False, failure_reason="server_unavailable")))
    result = manager.inspect_engine("ollama")
    assert result["installed"] is True
    assert result["api_status"] == "server_unavailable"


def test_package_manageable_is_false_only_for_engines_with_no_package_identity(monkeypatch):
    monkeypatch.setattr(manager, "package_receipt", lambda key: None)
    monkeypatch.setattr(manager.linker, "is_engine_installed", lambda key: True)
    assert manager.inspect_engine("ollama", check_api=False)["package_manageable"] is True
    assert manager.inspect_engine("koboldcpp", check_api=False)["package_manageable"] is False
    assert manager.inspect_engine("textgenwebui", check_api=False)["package_manageable"] is False


def test_cli_dry_run_json_never_executes_or_prompts(monkeypatch):
    monkeypatch.setattr(manager, "package_receipt", lambda key: brew_receipt())
    monkeypatch.setattr(manager, "execute_action", lambda *a, **k: pytest.fail("dry run must not execute"))
    monkeypatch.setattr(cli, "_ask_confirm", lambda *a, **k: pytest.fail("must not prompt"))
    result = CliRunner().invoke(cli.app, ["engine", "uninstall", "ollama", "--dry-run", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["dry_run"] is True


def test_engine_status_has_no_general_prelude_side_effects(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OMM_HOME", tmp_path / "not-created")
    monkeypatch.setattr(manager, "package_receipt", lambda key: None)
    monkeypatch.setattr(manager.linker, "is_engine_installed", lambda key: False)
    for name in ("_maybe_start_update_check", "_maybe_auto_import", "_maybe_run_onboarding"):
        monkeypatch.setattr(cli, name, lambda *a: pytest.fail("read-only command must skip prelude"))
    result = CliRunner().invoke(cli.app, ["engine", "status", "ollama", "--no-api", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[0]["installed"] is False
    assert not config.OMM_HOME.exists()
