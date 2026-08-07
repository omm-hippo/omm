from omm.featurize import candidate_active_parameter_count_billions, parse_chip_score


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
