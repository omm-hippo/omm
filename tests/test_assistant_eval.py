import json
from pathlib import Path
from time import perf_counter

import pytest

from omm.assistant_knowledge import COMMAND_IDS, rank_command_candidates


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "assistant_intents.json"
EVAL_GROUPS = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
EVAL_CASES = [
    (group["command"], question)
    for group in EVAL_GROUPS
    for question in group["questions"]
]


def test_eval_fixture_covers_every_command_and_at_least_180_paraphrases():
    assert {group["command"] for group in EVAL_GROUPS} == COMMAND_IDS
    assert len(EVAL_CASES) >= 180
    assert all(len(group["questions"]) >= 8 for group in EVAL_GROUPS)
    assert all(
        any("가" <= char <= "힣" for question in group["questions"][:4] for char in question)
        for group in EVAL_GROUPS
    )
    assert all(
        any(question.isascii() for question in group["questions"])
        for group in EVAL_GROUPS
    )


@pytest.mark.parametrize(
    ("expected", "question"),
    EVAL_CASES,
    ids=[f"{expected}-{index}" for index, (expected, _) in enumerate(EVAL_CASES)],
)
def test_deterministic_top1_and_ai_candidate_set(expected, question):
    result = rank_command_candidates(question)
    candidate_ids = [candidate.command_id for candidate in result.candidates]

    assert expected in candidate_ids, (
        f"AI candidate-set miss for {question!r}: expected {expected}, got {candidate_ids}"
    )
    assert result.clarify is False, (
        f"unexpected clarification for {question!r}: {candidate_ids}, {result.reason}"
    )
    assert candidate_ids[0] == expected, (
        f"no-AI top-1 miss for {question!r}: expected {expected}, got {candidate_ids}"
    )


def test_router_performance_is_bounded_for_interactive_cli_use():
    repetitions = 10
    started = perf_counter()
    for _ in range(repetitions):
        for _, question in EVAL_CASES:
            rank_command_candidates(question)
    elapsed = perf_counter() - started
    route_count = repetitions * len(EVAL_CASES)
    average = elapsed / route_count

    # A single route should stay well below a human-perceptible CLI delay. The
    # per-route limit is generous enough for shared runners while still
    # catching accidental whole-corpus fuzzy matching.
    assert average < 0.005, (
        f"{route_count} deterministic routes averaged {average * 1000:.3f}ms"
    )


@pytest.mark.parametrize(
    "question",
    [
        "qwen을 찾아서 설치해줘",
        "모델을 지우고 다시 설치해줘",
        "run the model and then delete it",
        "benchmark qwen and uninstall it",
    ],
)
def test_conflicting_action_requests_are_not_forced_to_one_command(question):
    result = rank_command_candidates(question)

    assert result.clarify is True
    assert result.reason == "conflicting_intents"
    assert len(result.candidates) >= 2


@pytest.mark.parametrize(
    ("question", "reason"),
    [
        ("안녕", "smalltalk"),
        ("감사합니다", "smalltalk"),
        ("thanks", "smalltalk"),
        ("OMM이 뭐야?", "product_question"),
        ("what can OMM do?", "product_question"),
    ],
)
def test_smalltalk_and_product_questions_stay_out_of_command_execution(question, reason):
    result = rank_command_candidates(question)

    assert result.clarify is True
    assert result.candidates == ()
    assert result.reason == reason
