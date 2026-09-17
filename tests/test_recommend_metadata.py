from __future__ import annotations

import pytest

from omm import hub, recommend_metadata


@pytest.mark.parametrize(("metadata", "model_type", "purpose"), [
    ({"pipeline_tag": "text-generation"}, "LLM", "General"),
    ({"pipeline_tag": "text-generation", "tags": ["coding"]}, "LLM", "Coding"),
    ({"pipeline_tag": "text-generation", "tags": ["math"]}, "LLM", "Reasoning"),
    ({"pipeline_tag": "text-generation", "tags": ["roleplay"]}, "LLM", "Writing"),
    ({"pipeline_tag": "translation"}, "LLM", "Translation"),
    ({"pipeline_tag": "summarization"}, "LLM", "Documents"),
    ({"pipeline_tag": "question-answering"}, "LLM", "Documents"),
    ({"pipeline_tag": "image-text-to-text"}, "VLM", "—"),
    ({"pipeline_tag": "document-question-answering"}, "VLM", "Documents"),
    ({"pipeline_tag": "text-generation", "tags": ["vision"]}, "VLM", "General"),
    ({"pipeline_tag": "feature-extraction", "tags": ["coding"]}, "Embedding", "—"),
    ({"model_type": "Embedding", "use_case": "general"}, "Embedding", "—"),
    ({"pipeline_tag": "text-to-image"}, "Unknown", "—"),
    ({"model_type": "LLM", "use_case": "long_document"}, "LLM", "Documents"),
    ({"tags": ["task:text-generation", "translation"]}, "LLM", "Translation"),
    ({"model_type": "VLM", "use_case": "Writing", "tags": ["coding"]}, "VLM", "Writing"),
])
def test_declared_type_and_six_use_cases(metadata, model_type, purpose):
    labels = recommend_metadata.classify(metadata)
    assert (labels.model_type, labels.use_case) == (model_type, purpose)
    assert labels.type_source == ("Unknown" if model_type == "Unknown" else "Catalog metadata")
    assert labels.use_case_source == ("Unknown" if purpose == "—" else "Catalog metadata")


def test_names_downloads_and_quantization_do_not_establish_type_or_quality():
    candidate = {
        "name": "Best-Coder-Vision-Embedding-7B",
        "repo_id": "coding/General-Chat-Reasoning-Model",
        "filename": "Translation-Instruct-Q4_K_M.gguf",
        "description": "10,000,000 downloads; best for writing",
    }
    assert recommend_metadata.classify(candidate) == recommend_metadata.ModelLabels()


@pytest.mark.parametrize("bad", [None, True, 17, {}, "coding", [None, {}, 5, True, "x" * 101]])
def test_malformed_metadata_is_unknown_without_crashing(bad):
    candidate = {key: bad for key in ("tags", "capabilities", "pipeline_tag", "model_type", "use_case")}
    # A scalar string is not a valid tag array, but a recognized explicit
    # use_case is meaningful; test invalid scalars separately from lists.
    if bad == "coding":
        candidate["pipeline_tag"] = candidate["use_case"] = "invalid"
    assert recommend_metadata.classify(candidate) == recommend_metadata.ModelLabels()


def test_conflicting_specialized_types_do_not_default_to_llm():
    labels = recommend_metadata.classify({"pipeline_tag": "text-generation", "tags": ["vision", "embedding"]})
    assert labels.model_type == "Unknown"
    assert labels.use_case == "—"


def test_tool_use_is_a_declared_feature_not_a_seventh_purpose():
    labels = recommend_metadata.classify({"model_type": "LLM", "capabilities": ["tools"]})
    assert labels.features == ("Tool use",)
    assert labels.use_case == "—"


def test_curated_fallback_matches_exact_artifacts_and_static_rule_aliases():
    assert recommend_metadata._CURATED == hub.CURATED_INDEX
    for name, (repo_id, filename) in hub.CURATED_INDEX.items():
        for candidate in ({"name": name}, {"repo_id": repo_id, "filename": filename}):
            labels = recommend_metadata.classify(candidate)
            assert (labels.model_type, labels.use_case) == ("LLM", "General")
            assert labels.type_source == labels.use_case_source == "Curated model card"
        for candidate in (
            {"name": name, "repo_id": "another/repo"},
            {"repo_id": repo_id, "filename": "another.gguf"},
            {"repo_id": repo_id, "filename": filename, "provider": "modelscope"},
        ):
            assert recommend_metadata.classify(candidate) == recommend_metadata.ModelLabels()


def test_catalog_metadata_keeps_only_bounded_existing_task_fields():
    assert recommend_metadata.catalog_metadata({"pipeline_tag": {}, "tags": "coding"}) == {}
    assert recommend_metadata.catalog_metadata({
        "pipeline_tag": "text-generation", "tags": [None, 1, "coding", "x" * 101],
        "description": "private or unrelated data",
    }) == {"pipeline_tag": "text-generation", "tags": ["coding"]}
    assert len(recommend_metadata.catalog_metadata({"tags": ["coding"] * 100})["tags"]) == 64
