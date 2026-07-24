from omm import contribute


def _hw():
    return object()  # rank_candidates is monkeypatched in every test, hw is opaque here


def _candidate(repo_id, filename):
    return {"repo_id": repo_id, "filename": filename, "name": filename}


def test_ref_formats_as_repo_id_colon_filename():
    c = _candidate("org/repo", "model.gguf")
    assert contribute.ref(c) == "huggingface:org/repo:model.gguf"


def test_phase_a_yields_full_viable_pool_in_ranked_order(monkeypatch):
    a, b, c_unviable = _candidate("o", "a.gguf"), _candidate("o", "b.gguf"), _candidate("o", "c.gguf")
    ranked = [(a, 50.0), (b, 30.0), (c_unviable, -5.0)]
    monkeypatch.setattr(contribute.predictor, "rank_candidates", lambda artifact, hw: ranked)

    queue = contribute.ContributionQueue({}, _hw(), history_refs=set())

    assert queue.next_candidate() is a
    assert queue.next_candidate() is b


def test_phase_a_skips_candidates_already_in_history(monkeypatch):
    a, b = _candidate("o", "a.gguf"), _candidate("o", "b.gguf")
    ranked = [(a, 50.0), (b, 30.0)]
    monkeypatch.setattr(contribute.predictor, "rank_candidates", lambda artifact, hw: ranked)

    queue = contribute.ContributionQueue({}, _hw(), history_refs={contribute.ref(a)})

    assert queue.next_candidate() is b


def test_phase_b_alternates_below_and_above_once_phase_a_exhausted(monkeypatch):
    a, b = _candidate("o", "a.gguf"), _candidate("o", "b.gguf")
    c, d = _candidate("o", "c.gguf"), _candidate("o", "d.gguf")
    ranked = [(a, 40.0), (b, 20.0), (c, -1.0), (d, -5.0)]
    monkeypatch.setattr(contribute.predictor, "rank_candidates", lambda artifact, hw: ranked)

    queue = contribute.ContributionQueue({}, _hw(), history_refs=set())
    queue.next_candidate()  # drains phase A: a
    queue.next_candidate()  # drains phase A: b

    # below_pool = reversed(viable) = [b, a]; above_pool = unviable = [c, d]
    assert queue.next_candidate() is b  # below, cursor 0
    assert queue.next_candidate() is c  # above, cursor 0
    assert queue.next_candidate() is a  # below, cursor 1 (wraps within pool of 2)
    assert queue.next_candidate() is d  # above, cursor 1


def test_phase_b_falls_through_to_other_side_when_one_side_fully_seen(monkeypatch):
    a, b = _candidate("o", "a.gguf"), _candidate("o", "b.gguf")
    c = _candidate("o", "c.gguf")
    ranked = [(a, 40.0), (b, 20.0), (c, -1.0)]
    monkeypatch.setattr(contribute.predictor, "rank_candidates", lambda artifact, hw: ranked)

    # a and b already benchmarked -> phase A empty, below_pool entirely seen
    queue = contribute.ContributionQueue(
        {}, _hw(), history_refs={contribute.ref(a), contribute.ref(b)}
    )

    assert queue.next_candidate() is c


def test_mark_seen_excludes_candidate_from_future_picks(monkeypatch):
    a, b = _candidate("o", "a.gguf"), _candidate("o", "b.gguf")
    ranked = [(a, 40.0), (b, 20.0)]
    monkeypatch.setattr(contribute.predictor, "rank_candidates", lambda artifact, hw: ranked)

    queue = contribute.ContributionQueue({}, _hw(), history_refs=set())
    queue.next_candidate()  # a
    queue.next_candidate()  # b
    queue.mark_seen(contribute.ref(b))
    queue.mark_seen(contribute.ref(a))

    # both viable candidates now seen, no unviable ones exist -> exhausted
    assert queue.next_candidate() is None


def test_returns_none_when_pools_exhausted_and_no_refetch_given(monkeypatch):
    monkeypatch.setattr(contribute.predictor, "rank_candidates", lambda artifact, hw: [])

    queue = contribute.ContributionQueue({}, _hw(), history_refs=set())

    assert queue.next_candidate() is None


def test_refetch_rebuilds_queue_with_new_candidates_when_changed(monkeypatch):
    old_artifact = {"v": 1}
    new_artifact = {"v": 2}
    new_candidate = _candidate("o", "new.gguf")

    def fake_rank(artifact, hw):
        if artifact is new_artifact:
            return [(new_candidate, 10.0)]
        return []

    monkeypatch.setattr(contribute.predictor, "rank_candidates", fake_rank)

    queue = contribute.ContributionQueue(old_artifact, _hw(), history_refs=set())
    assert queue.next_candidate() is None  # exhausted before refetch

    result = queue.next_candidate(refetch=lambda: (new_artifact, True))

    assert result is new_candidate


def test_refetch_returns_none_when_no_change_reported(monkeypatch):
    monkeypatch.setattr(contribute.predictor, "rank_candidates", lambda artifact, hw: [])

    queue = contribute.ContributionQueue({}, _hw(), history_refs=set())

    result = queue.next_candidate(refetch=lambda: ({}, False))

    assert result is None


def test_ref_includes_provider_prefix():
    candidate = {"repo_id": "org/repo", "filename": "model.gguf", "provider": "modelscope"}
    assert contribute.ref(candidate) == "modelscope:org/repo:model.gguf"


def test_ref_defaults_to_huggingface_when_provider_missing():
    candidate = {"repo_id": "org/repo", "filename": "model.gguf"}
    assert contribute.ref(candidate) == "huggingface:org/repo:model.gguf"


def test_matches_history_accepts_legacy_unprefixed_hf_ref():
    candidate = {"repo_id": "org/repo", "filename": "model.gguf", "provider": "huggingface"}
    legacy_history = {"org/repo:model.gguf"}
    assert contribute.matches_history(candidate, legacy_history) is True


def test_matches_history_rejects_legacy_ref_for_non_hf_provider():
    candidate = {"repo_id": "org/repo", "filename": "model.gguf", "provider": "modelscope"}
    legacy_history = {"org/repo:model.gguf"}
    assert contribute.matches_history(candidate, legacy_history) is False
