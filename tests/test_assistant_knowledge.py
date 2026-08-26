import json

import pytest
from typer.main import get_command

from omm import cli
from omm.assistant_knowledge import (
    COMMAND_IDS,
    COMMAND_KNOWLEDGE,
    MAX_CANDIDATES,
    MAX_QUESTION_LENGTH,
    QuestionSafetyError,
    RiskLevel,
    SideEffect,
    build_candidate_context,
    detect_secret,
    extract_known_model_target,
    normalize_question,
    rank_command_candidates,
    render_command,
    sanitize_question,
    validate_command_choice,
)


def _top(question: str) -> str | None:
    result = rank_command_candidates(question)
    return result.candidates[0].command_id if result.candidates else None


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("모델을 검색하고 싶어", "search"),
        ("내 컴퓨터 사양에 맞는 모델을 추천해줘", "recommend"),
        ("내 맥 성능에 맞고 코딩을 하기 좋은 AI", "recommend"),
        ("\u00a0내 맥에 맞는 AI\u00a0 추천", "recommend"),
        ("open ai 모델 추천해줘", "search"),
        ("엔트로픽 모델 추천 가능해?", "search"),
        ("Claude 모델을 찾아줘", "search"),
        ("qwen 모델 추천해줘", "search"),
        ("qwen 모델을 설치하고 싶어", "install"),
        ("설치된 qwen이 실제로 답을 생성하는지 확인하고 싶어", "verify"),
        ("설치된 모델 목록을 보여줘", "list"),
        ("모델을 제거하고 싶어", "uninstall"),
        ("중단된 다운로드 찌꺼기를 정리하고 싶어", "cleanup"),
        ("추천 개선용 벤치마크 데이터를 기여하고 싶어", "contribute"),
        ("Ollama 연결이 왜 안 되는지 진단해줘", "doctor"),
        ("램과 GPU 같은 컴퓨터 사양을 확인하고 싶어", "scan"),
        ("how do I find a model?", "search"),
        ("which model should I install for my hardware?", "recommend"),
        ("download a model", "install"),
        ("check whether this model actually generates text", "verify"),
        ("show my installed models", "list"),
        ("remove a model", "uninstall"),
        ("clean partial downloads", "cleanup"),
        ("upload telemetry to improve recommendations", "contribute"),
        ("troubleshoot my OMM installation", "doctor"),
    ],
)
def test_deterministic_fallback_routes_common_korean_and_english_intents(question, expected):
    result = rank_command_candidates(question)

    assert _top(question) == expected
    assert result.clarify is False
    assert result.confidence > 0.5


def test_ambiguous_question_requests_clarification_instead_of_guessing():
    result = rank_command_candidates("모델")

    assert result.clarify is True
    assert result.reason in {"ambiguous", "no_intent_match"}


def test_unknown_intent_requests_clarification_with_no_candidates():
    result = rank_command_candidates("오늘 점심은 뭐가 좋아?")

    assert result.clarify is True
    assert result.reason == "no_intent_match"
    assert result.candidates == ()


def test_candidate_limit_is_bounded():
    result = rank_command_candidates("모델 설치 목록 검색 추천", limit=2)

    assert len(result.candidates) <= 2
    with pytest.raises(ValueError, match="between"):
        rank_command_candidates("모델 검색", limit=MAX_CANDIDATES + 1)


def test_destructive_and_upload_commands_expose_risk_literally():
    for command_id in ("uninstall", "cleanup", "autoremove"):
        record = COMMAND_KNOWLEDGE[command_id]
        assert record.risk is RiskLevel.DESTRUCTIVE
        assert SideEffect.DELETE in record.side_effects

    for command_id in ("benchmark", "contribute"):
        record = COMMAND_KNOWLEDGE[command_id]
        assert record.risk is RiskLevel.EXTERNAL_UPLOAD
        assert SideEffect.UPLOAD in record.side_effects


def test_every_record_has_renderable_bilingual_trusted_metadata():
    for command_id, record in COMMAND_KNOWLEDGE.items():
        assert record.command_id == command_id
        assert render_command(command_id).startswith("omm ")
        assert record.summary_ko
        assert record.summary_en
        assert record.side_effects
        assert record.docs_path == f"/commands/{command_id}"
        assert record.intents_ko
        assert record.intents_en


def test_every_visible_top_level_cli_command_has_knowledge():
    root_command = get_command(cli.app)
    visible = {
        name for name, command in root_command.commands.items() if not command.hidden
    }

    assert visible <= COMMAND_IDS, f"missing command knowledge: {sorted(visible - COMMAND_IDS)}"


def test_candidate_context_is_compact_localized_and_only_uses_known_records():
    result = rank_command_candidates("내 컴퓨터에 맞는 모델을 설치하고 싶어", limit=3)
    context = build_candidate_context(result.candidates, locale="ko")
    payload = json.loads(context)

    assert 1 <= len(payload) <= 3
    assert payload[0]["commandId"] == result.candidates[0].command_id
    assert payload[0]["command"] == render_command(payload[0]["commandId"])
    assert payload[0]["summary"] == COMMAND_KNOWLEDGE[payload[0]["commandId"]].summary_ko
    assert "risk" in payload[0]
    assert "effects" in payload[0]
    assert "\n" not in context  # no pretty-print indentation

    english = json.loads(build_candidate_context(["verify"], locale="en"))
    assert english[0]["summary"] == COMMAND_KNOWLEDGE["verify"].summary_en


def test_candidate_context_rejects_unknown_duplicate_and_excessive_ids():
    with pytest.raises(ValueError, match="unknown"):
        build_candidate_context(["not-a-command"])
    with pytest.raises(ValueError, match="unique"):
        build_candidate_context(["search", "search"])
    with pytest.raises(ValueError, match="at most"):
        build_candidate_context(list(sorted(COMMAND_IDS))[: MAX_CANDIDATES + 1])


def test_ai_choice_must_be_in_the_offered_known_candidates():
    assert validate_command_choice("verify", ["verify", "doctor"])
    assert not validate_command_choice("uninstall", ["verify", "doctor"])
    assert not validate_command_choice("rm -rf /", ["verify", "doctor"])
    with pytest.raises(ValueError, match="unknown"):
        render_command("rm -rf /")


def test_question_normalization_strips_control_characters_and_caps_length():
    assert normalize_question("  모델\x00\n\t검색  ") == "모델 검색"
    assert normalize_question("ＡＩ에게   질문") == "AI에게 질문"

    with pytest.raises(QuestionSafetyError) as exc_info:
        normalize_question("가" * (MAX_QUESTION_LENGTH + 1))
    assert exc_info.value.code == "too_long"


@pytest.mark.parametrize(
    "question",
    [
        "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz1234 모델이 안 돼",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz.123456",
        "password=ThisLooksLikeARealPassword123",
        "-----BEGIN PRIVATE KEY----- secret material",
        "github token ghp_abcdefghijklmnopqrstuvwxyz123456",
    ],
)
def test_secret_shaped_input_is_refused_before_model_routing(question):
    assert detect_secret(question)
    with pytest.raises(QuestionSafetyError) as exc_info:
        sanitize_question(question)
    assert exc_info.value.code == "secret_detected"


def test_security_terms_without_values_are_not_false_positive_secrets():
    question = "API key를 어디에 설정하는지 도움말을 보고 싶어"

    assert not detect_secret(question)
    assert sanitize_question(question) == question


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("open ai 모델 추천해줘", "openai"),
        ("엔트로픽 모델 추천 가능해?", "anthropic"),
        ("Claude 모델 찾아줘", "anthropic"),
        ("Qwen 모델을 찾아줘", "qwen"),
        ("Deep Seek model", "deepseek"),
        ("그냥 모델 추천해줘", None),
    ],
)
def test_known_model_target_extraction_is_allowlisted(question, expected):
    assert extract_known_model_target(question) == expected
