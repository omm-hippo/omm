from omm.featurize import candidate_active_parameter_count_billions


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
