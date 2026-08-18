from omm.featurize import (
    candidate_active_parameter_count_billions,
    parse_chip_score,
    resolve_active_parameter_count_billions,
)


def test_historical_gpt_oss_total_is_repaired_to_published_active_count():
    historical_row = {
        "model_installed": "gpt-oss:20b",
        "parameter_count_b": 20.9,
        "active_parameter_count_b": 20.0,
    }

    assert candidate_active_parameter_count_billions(historical_row) == 3.6


def test_tensor_derived_gpt_oss_active_count_is_preserved():
    corrected_row = {
        "model_installed": "gpt-oss:20b",
        "parameter_count_b": 20.9,
        "active_parameter_count_b": 3.608307264,
    }

    assert candidate_active_parameter_count_billions(corrected_row) == 3.608307264


def test_parse_chip_score_handles_gpu_names_the_same_way_as_cpu_names():
    assert parse_chip_score("NVIDIA GeForce RTX 4090") == (4090.0, 0.0)
    assert parse_chip_score("NVIDIA GeForce RTX 3080 Ti") == (3080.0, 1.0)
    assert parse_chip_score("Apple M2 Max") == (2.0, 2.0)
    assert parse_chip_score("") == (0.0, 0.0)


def _gguf_named_1b():
    return {
        "name": "Gemma-3-1B-it-GLM-4.7-Flash-Heretic-Uncensored-Thinking_Q4_k_m.gguf",
        "filename": "Gemma-3-1B-it-GLM-4.7-Flash-Heretic-Uncensored-Thinking_Q4_k_m.gguf",
        "repo_id": "Andycurrent/Gemma-3-1B-it-GLM-4.7-Flash-Heretic-Uncensored-Thinking-GGUF",
        "is_moe": False,
    }


def test_measured_total_outranks_the_rounded_name_for_a_dense_model():
    """A "-1B-" name parses to 1.0; the tensor count said 0.99989.

    Telemetry validation requires active <= total, so emitting the rounded
    name-derived value got the entire benchmark rejected server-side.
    """
    candidate = _gguf_named_1b()

    assert candidate_active_parameter_count_billions(candidate) == 1.0
    assert resolve_active_parameter_count_billions(candidate, 0.99989) == 0.99989


def test_resolved_active_count_never_exceeds_the_total_it_was_given():
    dense = _gguf_named_1b()
    moe = {
        "name": "Qwen3-30B-A3B-Q4_K_M.gguf",
        "filename": "Qwen3-30B-A3B-Q4_K_M.gguf",
        "repo_id": None,
        "is_moe": True,
    }

    for total in (0.5, 0.99989, 1.0, 2.0):
        assert resolve_active_parameter_count_billions(dense, total) <= total
    # An MoE's published active count survives when it is genuinely smaller.
    assert resolve_active_parameter_count_billions(moe, 30.5) == 3.0
    assert resolve_active_parameter_count_billions(moe, 2.0) == 2.0


def test_unknown_total_and_unknown_moe_active_are_left_alone():
    dense = _gguf_named_1b()
    opaque_moe = {"name": "mystery-Q4_K_M.gguf", "filename": "mystery-Q4_K_M.gguf",
                  "repo_id": None, "is_moe": True}

    # No measured total: the name-derived value is still the best available.
    assert resolve_active_parameter_count_billions(dense, None) == 1.0
    # An MoE whose name hides its active count must stay None so the caller
    # skips the upload instead of inventing a number.
    assert resolve_active_parameter_count_billions(opaque_moe, 30.0) is None
