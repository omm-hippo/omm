from copy import deepcopy

import pytest

from omm.recommend_selection import model_label, quantization_label, shortlist, variant_warning


def candidate(model, *, uploader="org", filename=None, **extra):
    return {
        "repo_id": f"{uploader}/{model}-GGUF",
        "filename": filename or f"{model}-Q4_K_M.gguf",
        **extra,
    }


def test_mirrors_are_deduped_before_limit_and_families_share_the_shortlist():
    qwen = candidate("Qwen3.8-27B", filename="Qwen3.8-27B-UD-Q4_K_M.gguf")
    mirrors = [candidate("Qwen3.8-27B", uploader=str(i)) for i in range(12)]
    qwen14 = candidate("Qwen3-14B")
    gpt = candidate("gpt-oss-20b")
    mistral = candidate("Mistral-7B-Instruct-v0.2")
    ranked = [(c, 6.0) for c in [qwen, *mirrors, gpt, qwen14, mistral]]
    before = deepcopy(ranked)

    actual = shortlist(ranked, limit=4)

    assert [c for c, _ in actual] == [qwen, gpt, qwen14, mistral]
    assert ranked == before


@pytest.mark.parametrize("marker", ["Uncensored", "abliterated", "Heretic", "NSFW", "MTP", "AD", "DFlash2", "EAGLE3", "Drafter"])
def test_specialized_variants_do_not_outrank_normal_candidates(marker):
    special = candidate(f"Qwen3.8-27B-{marker}")
    normal = candidate("Mistral-7B-Instruct-v0.2")
    ranked = [(special, 30.0), (normal, 10.0)]

    assert shortlist(ranked) == [(normal, 10.0), (special, 30.0)]
    assert shortlist(ranked, limit=1) == [(normal, 10.0)]
    assert variant_warning(special)
    assert variant_warning(normal) is None


def test_role_size_version_and_repo_only_finetunes_remain_distinct():
    models = [
        candidate("Qwen3.8-27B"),
        candidate("Qwen3.6-27B"),
        candidate("Qwen3.8-14B"),
        candidate("Qwen3.8-27B-Coder"),
        candidate("Qwen3.8-27B-MTP", filename="Qwen3.8-27B-Q4_K_M.gguf"),
        candidate("Qwen3.8-27B-Custom", filename="Qwen3.8-27B-Q4_K_M.gguf"),
    ]
    result = shortlist([(c, 20.0) for c in models])
    assert len(result) == len(models)
    assert result[-1][0] == models[4]


def test_generic_filenames_do_not_merge_unrelated_models_or_sizes():
    models = [candidate("glm-edge-1.5b-chat", filename="ggml-model-Q4_K_M.gguf"),
              candidate("glm-edge-4b-chat", filename="ggml-model-Q4_K_M.gguf")]
    assert len(shortlist([(c, 10.0) for c in models])) == 2


def test_quant_and_provider_mirrors_keep_first_ranked_exact_candidate():
    first = candidate("Qwen3.8-27B", provider="modelscope")
    other = candidate("Qwen3.8-27B-Q8_0", uploader="mirror",
                      filename="Qwen3.8-27B-Q8_0.gguf", provider="huggingface")
    assert shortlist([(first, 9.0), (other, 6.0)]) == [(first, 9.0)]


def test_known_and_unknown_families_keep_available_alternatives():
    models = [candidate("Meta-Llama-3.1-8B"), candidate("Llama-3.2-3B"),
              candidate("Llama-3.2-1B"), candidate("NewModel-8B"), candidate("DifferentModel-7B")]
    result = shortlist([(c, 8.0) for c in models], limit=4)
    assert [c for c, _ in result] == [models[0], models[1], models[3], models[4]]


def test_family_overflow_fills_empty_slots_without_duplicates():
    models = [candidate(f"Qwen3-{size}B") for size in (14, 8, 4, 1.7)]
    ranked = [(c, 10.0) for c in models]
    assert shortlist(ranked) == ranked


def test_no_specialized_false_positive_from_substrings():
    assert variant_warning(candidate("Shadow-7B", uploader="adapted-models")) is None
    assert variant_warning(candidate("Qwen3-8B", uploader="ad")) is None
    assert variant_warning(candidate("Qwen3-8B", uploader="uncensored-models")) is None


def test_custom_suffix_after_quantization_is_not_erased_or_merged():
    base = candidate("Qwen3-8B")
    custom = candidate("Qwen3-8B", filename="Qwen3-8B-Q4_K_M-Custom.gguf")
    assert len(shortlist([(base, 10.0), (custom, 10.0)])) == 2
    assert model_label(custom["filename"]).endswith("Custom")


@pytest.mark.parametrize("quant", ["Q4_K_M", "UD-Q4_K_M", "IQ2_XXS", "Q8_0", "BF16"])
def test_sharded_packages_preserve_quantization_and_dedupe_with_single_file(quant):
    single = candidate("Qwen3-8B", filename=f"Qwen3-8B-{quant}.gguf")
    shard = candidate("Qwen3-8B", filename=f"Qwen3-8B-{quant}-00001-of-00003.gguf")
    assert model_label(shard["filename"]) == "Qwen3 8B"
    assert quantization_label(shard) == quant
    assert shortlist([(single, 10.0), (shard, 10.0)]) == [(single, 10.0)]


def test_empty_and_zero_limit():
    assert shortlist([]) == []
    assert shortlist([(candidate("Qwen3-8B"), 2.0)], limit=0) == []
