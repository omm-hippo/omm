import json

import pytest
from typer.testing import CliRunner

from omm import cli
from omm.assistant_runtime import AssistantClassification, AssistantRuntimeError


runner = CliRunner()


class _SuccessfulRuntime:
    calls = []

    def classify(
        self,
        question,
        allowed_command_ids,
        catalog_context,
        *,
        model=None,
    ):
        self.calls.append(
            {
                "question": question,
                "allowed": tuple(allowed_command_ids),
                "context": catalog_context,
                "model": model,
            }
        )
        return AssistantClassification(
            command_id="verify",
            reason="untrusted model prose must not become command text",
            model=model or "qwen2.5:1.5b",
        )


def test_ask_local_ai_selects_only_from_candidates_and_renders_trusted_command(
    monkeypatch,
):
    _SuccessfulRuntime.calls.clear()
    monkeypatch.setattr(cli, "OllamaAssistantRuntime", _SuccessfulRuntime)

    result = runner.invoke(
        cli.app,
        ["ask", "설치된 모델이 실제로 답을 생성하는지 확인하고 싶어", "--no-color"],
    )

    assert result.exit_code == 0, result.stdout
    assert "모드: 로컬 AI · qwen2.5:1.5b" in result.stdout
    assert "omm verify <MODEL>" in result.stdout
    assert "untrusted model prose" not in result.stdout
    assert len(_SuccessfulRuntime.calls) == 1
    call = _SuccessfulRuntime.calls[0]
    assert call["allowed"] == ("verify",)
    assert '"commandId":"verify"' in call["context"]
    assert '"commandId":"clarify"' not in call["context"]


def test_ask_passes_exact_model_tag_to_runtime(monkeypatch):
    _SuccessfulRuntime.calls.clear()
    monkeypatch.setattr(cli, "OllamaAssistantRuntime", _SuccessfulRuntime)

    result = runner.invoke(
        cli.app,
        [
            "ask",
            "설치한 모델이 작동하는지 확인하고 싶어",
            "--model",
            "qwen2.5:1.5b-instruct-q4_K_M",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert _SuccessfulRuntime.calls[0]["model"] == "qwen2.5:1.5b-instruct-q4_K_M"
    assert "qwen2.5:1.5b-instruct-q4_K_M" in result.stdout


def test_ask_accepts_an_unquoted_multiword_question_and_options_after_it():
    result = runner.invoke(
        cli.app,
        [
            "ask",
            "내",
            "맥",
            "성능에",
            "맞고",
            "코딩을",
            "하기",
            "좋은",
            "AI",
            "--no-ai",
            "--no-color",
        ],
    )

    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert "omm recommend" in result.stdout
    assert "unexpected extra argument" not in result.stderr.lower()


def test_ask_no_ai_routes_mac_recommendation_with_nonbreaking_spaces():
    result = runner.invoke(
        cli.app,
        ["ask", "\u00a0내 맥에 맞는 AI\u00a0 추천", "--no-ai", "--no-color"],
    )

    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert "모드: 내장 안내" in result.stdout
    assert "omm recommend" in result.stdout
    assert "아직 선택하지 않음" not in result.stdout


def test_ask_no_ai_uses_deterministic_built_in_guidance(monkeypatch):
    class ForbiddenRuntime:
        def __init__(self):
            raise AssertionError("--no-ai must not construct a runtime")

    monkeypatch.setattr(cli, "OllamaAssistantRuntime", ForbiddenRuntime)

    result = runner.invoke(
        cli.app,
        ["ask", "설치된 모델 목록을 보고 싶어", "--no-ai", "--no-color"],
    )

    assert result.exit_code == 0, result.stdout
    assert "모드: 내장 안내" in result.stdout
    assert "omm list" in result.stdout


def test_ask_runtime_failure_falls_back_only_when_match_is_clear(monkeypatch):
    class BrokenRuntime:
        def classify(self, *args, **kwargs):
            raise AssistantRuntimeError("runtime_error", "safe local runtime failure")

    monkeypatch.setattr(cli, "OllamaAssistantRuntime", BrokenRuntime)

    result = runner.invoke(
        cli.app,
        ["ask", "모델 삭제", "--no-color"],
    )

    assert result.exit_code == 0, result.stdout
    assert "모드: 내장 안내" in result.stdout
    assert "omm uninstall <MODEL|all>" in result.stdout
    assert "safe local runtime failure" in result.stderr


def test_ask_invalid_runtime_choice_uses_deterministic_fallback(monkeypatch):
    class InvalidRuntime:
        def classify(self, *args, **kwargs):
            return AssistantClassification("install", "invented", "fake-model")

    monkeypatch.setattr(cli, "OllamaAssistantRuntime", InvalidRuntime)

    result = runner.invoke(
        cli.app,
        ["ask", "설치된 모델이 실제로 답을 생성하는지 확인하고 싶어"],
    )

    assert result.exit_code == 0, result.stdout
    assert "omm verify <MODEL>" in result.stdout
    assert "omm install" not in result.stdout
    assert "invalid selection" in result.stderr


def test_ask_ambiguous_question_requests_clarification_without_calling_runtime(
    monkeypatch,
):
    class ForbiddenRuntime:
        def __init__(self):
            raise AssertionError("no candidates means no model call")

    monkeypatch.setattr(cli, "OllamaAssistantRuntime", ForbiddenRuntime)

    result = runner.invoke(cli.app, ["ask", "도와줘", "--no-color"])

    assert result.exit_code == 0, result.stdout
    assert "아직 선택하지 않음" in result.stdout
    assert "조금 더 구체적으로" in " ".join(result.stdout.split())


def test_ask_greeting_returns_a_friendly_example_without_loading_ai(monkeypatch):
    class ForbiddenRuntime:
        def __init__(self):
            raise AssertionError("a greeting must not load a local model")

    monkeypatch.setattr(cli, "OllamaAssistantRuntime", ForbiddenRuntime)

    result = runner.invoke(cli.app, ["ask", "안녕", "--no-color"])

    assert result.exit_code == 0, result.stdout
    assert "안녕하세요!" in result.stdout
    assert "내 맥에 맞는 코딩 모델" in " ".join(result.stdout.split())


@pytest.mark.parametrize(
    "question,expected",
    [
        ("   ", "must not be empty"),
        ("api_key=abcdefghijklmnop 모델 설치", "credential"),
    ],
)
def test_ask_rejects_invalid_or_secret_input_without_echoing_it(question, expected):
    result = runner.invoke(cli.app, ["ask", question])

    assert result.exit_code == 2
    assert expected in result.stderr
    assert "abcdefghijklmnop" not in result.stdout
    assert "abcdefghijklmnop" not in result.stderr


def test_ask_json_contract_is_stable_and_machine_readable():
    result = runner.invoke(
        cli.app,
        ["--json", "ask", "설치된 모델 목록을 보고 싶어", "--no-ai"],
    )

    assert result.exit_code == 0, result.stdout
    assert "has no effect" not in result.stderr
    payload = json.loads(result.stdout)
    assert list(payload) == [
        "mode",
        "model",
        "commandId",
        "command",
        "summary",
        "sideEffects",
        "docsUrl",
        "needsClarification",
    ]
    assert payload == {
        "mode": "built-in",
        "model": None,
        "commandId": "list",
        "command": "omm list",
        "summary": "OMM으로 설치한 모델과 연결 상태를 나열합니다.",
        "sideEffects": ["inspect"],
        "docsUrl": "https://omm.run/ko/commands/list",
        "needsClarification": False,
    }


def test_ask_help_and_full_command_reference_include_mvp_options():
    command_help = runner.invoke(cli.app, ["help", "ask", "--no-color"])
    all_help = runner.invoke(cli.app, ["help", "--all", "--no-color"])

    assert command_help.exit_code == 0, command_help.stdout
    assert "Map a natural-language question" in command_help.stdout
    assert "--model" in command_help.stdout
    assert "--engine" in command_help.stdout
    assert "--no-ai" in command_help.stdout
    assert all_help.exit_code == 0, all_help.stdout
    assert "omm ask" in all_help.stdout


def test_ask_root_skips_every_unrelated_prelude(monkeypatch):
    monkeypatch.setattr(cli.doctor_mod, "read_theme_read_only", lambda: "dark")

    def forbidden(*args, **kwargs):
        raise AssertionError("ask must not run an unrelated root prelude")

    monkeypatch.setattr(cli, "load_config", forbidden)
    monkeypatch.setattr(cli, "_maybe_start_update_check", forbidden)
    monkeypatch.setattr(cli, "_maybe_run_onboarding", forbidden)
    monkeypatch.setattr(cli, "_maybe_auto_import", forbidden)
    monkeypatch.setattr(cli.telemetry, "flush_pending", forbidden)
    monkeypatch.setattr(cli.error_report, "flush_pending", forbidden)

    result = runner.invoke(
        cli.app,
        ["ask", "설치된 모델 목록을 보고 싶어", "--no-ai"],
    )

    assert result.exit_code == 0, result.stdout
    assert "omm list" in result.stdout


def test_ask_does_not_dispatch_any_existing_command_callback(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("ask recommendations must never execute a command callback")

    patched = 0
    for command_info in cli.app.registered_commands:
        command_name = command_info.name or getattr(command_info.callback, "__name__", "")
        if command_name in {"install", "verify", "run", "uninstall"}:
            monkeypatch.setattr(command_info, "callback", forbidden)
            patched += 1
    assert patched == 4

    result = runner.invoke(
        cli.app,
        ["ask", "설치된 모델이 실제로 답을 생성하는지 확인하고 싶어", "--no-ai"],
    )

    assert result.exit_code == 0, result.stdout
    assert "omm verify <MODEL>" in result.stdout


def test_ask_missing_question_is_usage_error_without_tty():
    result = runner.invoke(cli.app, ["ask"])

    assert result.exit_code == 2
    assert "QUESTION is required" in result.stderr


def test_ask_missing_question_prompts_once_in_tty(monkeypatch):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli.typer, "prompt", lambda prompt: "list installed models")

    result = runner.invoke(cli.app, ["ask", "--no-ai", "--no-color"])

    assert result.exit_code == 0, result.stdout
    assert "omm list" in result.stdout


def test_ask_rejects_unsupported_engine_as_usage_error():
    result = runner.invoke(
        cli.app,
        ["ask", "list installed models", "--engine", "lmstudio", "--no-ai"],
    )

    assert result.exit_code == 2
    assert "must be ollama" in result.stderr


def test_ask_yes_keeps_standard_no_effect_warning():
    result = runner.invoke(
        cli.app,
        ["ask", "list installed models", "--no-ai", "--yes"],
    )

    assert result.exit_code == 0, result.stdout
    assert "--yes has no effect on `omm ask`" in result.stderr
