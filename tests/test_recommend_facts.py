import json
import time
from copy import deepcopy

import pytest
import requests

from omm import config, predictor, recommend_facts as facts, recommend_metadata
from omm.hardware import HardwareInfo


def candidate(provider="huggingface", filename="Qwen2.5-7B-Instruct-Q4_K_M.gguf"):
    return {"provider": provider, "repo_id": "bartowski/Qwen2.5-7B-Instruct-GGUF", "filename": filename}


def test_exact_file_size_corrects_minimal_budget_without_mutating_signed_catalog(monkeypatch):
    c = candidate()
    original = {"candidates": [c], "trees": [{"leaf": True, "value": 20}]}
    before = deepcopy(original)
    key = facts._key(c)
    records = {key: {"metadata": {"pipeline_tag": "text-generation", "tags": ["chat"]},
                     "files": {c["filename"]: 4683074240}}}
    enriched = facts.apply(original, records)
    updated = enriched["candidates"][0]
    hw = HardwareInfo("macOS", "", "Apple M5", 24, 8, True, "Apple M5", 24, 8)
    assert predictor.estimate_required_memory_gb(c) == pytest.approx(4.62)
    assert predictor.estimate_required_memory_gb(updated) > 5.23
    assert predictor.filter_by_profile([(updated, 20)], hw, "minimal") == []
    labels = recommend_metadata.classify(updated)
    assert (labels.model_type, labels.use_case, labels.type_source) == ("LLM", "General", "Provider metadata cache")
    assert original == before


def test_facts_never_cross_provider_or_filename():
    c = candidate()
    records = {facts._key(c): {"metadata": {"pipeline_tag": "text-generation"}, "files": {c["filename"]: 4683074240}}}
    for other in [candidate("modelscope"), candidate(filename="other-Q4_K_M.gguf")]:
        assert facts.apply({"candidates": [other]}, records)["candidates"] == [other]


def test_huggingface_fetch_keeps_only_requested_files_and_rejects_wrong_repo(monkeypatch):
    c = candidate()
    payload = {"id": c["repo_id"], "pipeline_tag": "text-generation", "siblings": [
        {"rfilename": c["filename"], "size": 4683074240},
        {"rfilename": "different-Q8_0.gguf", "size": 8100000000},
    ]}
    monkeypatch.setattr(facts, "_get", lambda *args, **kwargs: payload)
    record = facts.fetch("huggingface", c["repo_id"], {c["filename"]}, time.monotonic()+5)
    assert record["files"] == {c["filename"]: 4683074240}
    payload["id"] = "wrong/repository"
    with pytest.raises(ValueError, match="different repository"):
        facts.fetch("huggingface", c["repo_id"], {c["filename"]}, time.monotonic()+5)


def test_modelscope_tasks_reach_vlm_label(monkeypatch):
    calls = []
    def get(url, *args, **kwargs):
        calls.append(url)
        if url.endswith("/repo/files"):
            return {"Data": {"Files": [{"Path": "vlm-Q4_K_M.gguf", "Size": 5100000000}]}}
        return {"Success": True, "Data": {"Tags": ["gguf", "multimodal"], "Tasks": [{"Name": "image-text-to-text"}]}}
    monkeypatch.setattr(facts, "_get", get)
    record = facts.fetch("modelscope", "org/vlm", {"vlm-Q4_K_M.gguf"}, time.monotonic()+5)
    assert len(calls) == 2
    labels = recommend_metadata.classify(record["metadata"])
    assert (labels.model_type, labels.use_case) == ("VLM", "—")


def test_cache_is_separate_bounded_and_expires(isolated_omm_home):
    config.RECOMMEND_MODEL_PATH.write_bytes(b"signed bytes remain unchanged")
    facts._path().write_text(json.dumps({"version": 1, "repos": {
        "fresh": {"fetched_at": time.time(), "files": {}},
        "old": {"fetched_at": time.time()-facts.TTL_SECONDS-1},
        "bad": {"fetched_at": True},
    }}))
    assert set(facts.load()) == {"fresh", "old"}
    assert config.RECOMMEND_MODEL_PATH.read_bytes() == b"signed bytes remain unchanged"
    facts._path().write_text("[1]")
    assert facts.load() == {}


def test_old_facts_keep_known_size_with_stale_label():
    c = candidate()
    record = {"fetched_at": time.time()-facts.TTL_SECONDS-1,
              "metadata": {"pipeline_tag": "text-generation"},
              "files": {c["filename"]: 4683074240}}
    [updated] = facts.apply({"candidates": [c]}, {facts._key(c): record})["candidates"]
    assert updated["size_bytes"] == 4683074240
    assert recommend_metadata.classify(updated).type_source == "Provider metadata cache (stale)"


def test_refresh_caps_requests_and_does_not_refetch_fresh_repos(monkeypatch, isolated_omm_home):
    candidates = [{"repo_id": f"org/model{i}", "filename": "model.gguf"} for i in range(30)]
    artifact = {"candidates": candidates}
    monkeypatch.setattr(facts, "_preferred", lambda artifact, hw: artifact["candidates"])
    calls = []
    def fetch(provider, repo, filenames, deadline):
        calls.append(repo)
        return {"fetched_at": time.time(), "files": {"model.gguf": 100}, "metadata": {}}
    monkeypatch.setattr(facts, "fetch", fetch)
    assert facts.refresh(artifact, None, max_repos=3)["fetched_repos"] == 3
    assert calls == ["org/model0", "org/model1", "org/model2"]
    facts.refresh(artifact, None, max_repos=1)
    assert calls[-1] == "org/model3"


def test_rate_limit_stops_whole_refresh_without_retry(monkeypatch, isolated_omm_home):
    candidates = [{"repo_id": f"org/model{i}", "filename": "model.gguf"} for i in range(3)]
    monkeypatch.setattr(facts, "_preferred", lambda artifact, hw: candidates)
    calls = []
    def fetch(*args):
        calls.append(args)
        response = requests.Response(); response.status_code = 429
        raise requests.HTTPError(response=response)
    monkeypatch.setattr(facts, "fetch", fetch)
    result = facts.refresh({"candidates": candidates}, None)
    assert len(calls) == 1
    assert result["error"] == "provider HTTP 429"


@pytest.mark.parametrize("repo", ["../bad", "org/../../bad", "org/repo?token=x", "https://other/repo"])
def test_invalid_repo_cannot_form_a_provider_request(repo):
    assert facts._key({"repo_id": repo}) is None
