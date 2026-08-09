from omm import cli
from omm.hub import ModelResolutionError


def _candidate(repo_id="org/repo", filename="model-Q4_K_M.gguf", provider="huggingface"):
    return {"repo_id": repo_id, "filename": filename, "name": "model", "provider": provider}


def test_returns_unseen_siblings_sorted_by_quant_distance(monkeypatch):
    boundary = _candidate(filename="model-Q4_K_M.gguf")
    monkeypatch.setattr(
        cli,
        "fetch_repo_files",
        lambda provider, repo_id: (
            ["model-Q4_K_M.gguf", "model-Q2_K.gguf", "model-Q8_0.gguf", "model-Q5_K_M.gguf"],
            7.0,
        ),
    )
    monkeypatch.setattr(cli, "remote_file_size", lambda provider, repo_id, filename: 1234)

    result = cli._fetch_sibling_candidates(boundary)

    # Q4=4 bits (already tried, excluded); Q5=5 (dist 1) < Q2=2 (dist 2) < Q8=8 (dist 4).
    assert [c["filename"] for c in result] == [
        "model-Q5_K_M.gguf",
        "model-Q2_K.gguf",
        "model-Q8_0.gguf",
    ]
    assert all(c["size_bytes"] == 1234 for c in result)
    assert all(c["repo_id"] == "org/repo" for c in result)
    assert all(c["provider"] == "huggingface" for c in result)


def test_excludes_mmproj_files(monkeypatch):
    boundary = _candidate(filename="model-Q4_K_M.gguf")
    monkeypatch.setattr(
        cli,
        "fetch_repo_files",
        lambda provider, repo_id: (
            ["model-Q4_K_M.gguf", "mmproj-model-f16.gguf", "model-Q5_K_M.gguf"],
            7.0,
        ),
    )
    monkeypatch.setattr(cli, "remote_file_size", lambda provider, repo_id, filename: None)

    result = cli._fetch_sibling_candidates(boundary)

    assert [c["filename"] for c in result] == ["model-Q5_K_M.gguf"]


def test_returns_empty_list_when_repo_lookup_fails(monkeypatch):
    boundary = _candidate()

    def raise_error(provider, repo_id):
        raise ModelResolutionError("not found")

    monkeypatch.setattr(cli, "fetch_repo_files", raise_error)

    assert cli._fetch_sibling_candidates(boundary) == []


def test_returns_empty_list_when_tried_filename_has_no_parseable_quant(monkeypatch):
    boundary = _candidate(filename="model-unknownquant.gguf")

    def fail_if_called(*a):
        raise AssertionError("fetch_repo_files should not be called")

    monkeypatch.setattr(cli, "fetch_repo_files", fail_if_called)

    assert cli._fetch_sibling_candidates(boundary) == []
