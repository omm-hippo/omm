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


def test_deferred_candidate_is_not_history_and_can_be_released(monkeypatch):
    a, b = _candidate("o", "a.gguf"), _candidate("o", "b.gguf")
    monkeypatch.setattr(
        contribute.predictor,
        "rank_candidates",
        lambda artifact, hw: [(a, 40.0), (b, 20.0)],
    )
    queue = contribute.ContributionQueue({}, _hw(), history_refs=set())

    assert queue.next_candidate() is a
    a_ref = contribute.ref(a)
    queue.defer(a_ref)
    assert a_ref in queue.deferred_refs
    assert a_ref not in queue.history_refs
    assert queue.next_candidate() is b

    queue.release_deferred(a_ref)
    assert queue.next_candidate() is a


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


def test_mark_seen_reranks_remaining_queue_from_current_rank_candidates(monkeypatch):
    a, b, c = _candidate("o", "a.gguf"), _candidate("o", "b.gguf"), _candidate("o", "c.gguf")
    call_state = {"recalibrated": False}

    def fake_rank(artifact, hw):
        if not call_state["recalibrated"]:
            return [(a, 50.0), (b, 30.0), (c, 10.0)]
        return [(a, 50.0), (c, 40.0), (b, 30.0)]  # recalibration promotes c above b

    monkeypatch.setattr(contribute.predictor, "rank_candidates", fake_rank)

    queue = contribute.ContributionQueue({}, _hw(), history_refs=set())
    assert queue.next_candidate() is a

    call_state["recalibrated"] = True
    queue.mark_seen(contribute.ref(a))

    # Without re-ranking, the phase A queue built at construction time
    # would still serve b next (its position at construction). Re-ranking
    # must reflect c's promotion above b instead.
    assert queue.next_candidate() is c


def test_boundary_below_tracks_last_phase_a_draw_and_boundary_above_freezes_on_first(monkeypatch):
    a, b = _candidate("o", "a.gguf"), _candidate("o", "b.gguf")
    c, d = _candidate("o", "c.gguf"), _candidate("o", "d.gguf")
    ranked = [(a, 40.0), (b, 20.0), (c, -1.0), (d, -5.0)]
    monkeypatch.setattr(contribute.predictor, "rank_candidates", lambda artifact, hw: ranked)

    queue = contribute.ContributionQueue({}, _hw(), history_refs=set())
    queue.next_candidate()  # phase A: a
    queue.mark_seen(contribute.ref(a))
    queue.next_candidate()  # phase A: b (last/weakest phase-A draw)
    queue.mark_seen(contribute.ref(b))
    queue.next_candidate()  # phase B above: c (first unviable)
    queue.mark_seen(contribute.ref(c))
    queue.next_candidate()  # phase B above: d
    queue.mark_seen(contribute.ref(d))

    assert queue._boundary_below is b
    assert queue._boundary_above is c


def test_phase_c_yields_fetch_siblings_result_after_pools_exhausted(monkeypatch):
    a, b = _candidate("o", "a.gguf"), _candidate("o", "b.gguf")
    c = _candidate("o", "c.gguf")
    ranked = [(a, 40.0), (b, 20.0), (c, -1.0)]
    monkeypatch.setattr(contribute.predictor, "rank_candidates", lambda artifact, hw: ranked)

    queue = contribute.ContributionQueue({}, _hw(), history_refs=set())
    for _ in range(3):
        candidate = queue.next_candidate()
        queue.mark_seen(contribute.ref(candidate))

    assert queue.next_candidate() is None  # phase A/B fully exhausted
    assert queue._boundary_below is b
    assert queue._boundary_above is c

    sibling = _candidate("o", "b-q8.gguf")
    fetched_for = []

    def fake_fetch_siblings(boundary):
        fetched_for.append(boundary)
        return [sibling] if boundary is b else []

    assert queue.next_candidate(fetch_siblings=fake_fetch_siblings) is sibling
    assert fetched_for == [b]  # below tried before above


def test_phase_c_falls_through_to_above_when_below_boundary_absent(monkeypatch):
    c = _candidate("o", "c.gguf")
    ranked = [(c, -1.0)]  # only ever unviable -> phase A empty, below boundary never set
    monkeypatch.setattr(contribute.predictor, "rank_candidates", lambda artifact, hw: ranked)

    queue = contribute.ContributionQueue({}, _hw(), history_refs=set())
    assert queue.next_candidate() is c  # phase B above: only unviable candidate
    queue.mark_seen(contribute.ref(c))
    assert queue.next_candidate() is None
    assert queue._boundary_below is None
    assert queue._boundary_above is c

    sibling = _candidate("o", "c-q2.gguf")
    result = queue.next_candidate(
        fetch_siblings=lambda boundary: [sibling] if boundary is c else []
    )

    assert result is sibling


def test_phase_c_returns_none_and_does_not_call_fetch_siblings_when_not_provided(monkeypatch):
    monkeypatch.setattr(contribute.predictor, "rank_candidates", lambda artifact, hw: [])

    queue = contribute.ContributionQueue({}, _hw(), history_refs=set())

    assert queue.next_candidate() is None


def test_phase_c_does_not_refetch_siblings_twice_for_the_same_side(monkeypatch):
    a = _candidate("o", "a.gguf")
    ranked = [(a, 40.0)]
    monkeypatch.setattr(contribute.predictor, "rank_candidates", lambda artifact, hw: ranked)

    queue = contribute.ContributionQueue({}, _hw(), history_refs=set())
    queue.next_candidate()
    queue.mark_seen(contribute.ref(a))
    assert queue.next_candidate() is None
    assert queue._boundary_below is a

    call_count = {"n": 0}

    def counting_fetch(boundary):
        call_count["n"] += 1
        return []

    assert queue.next_candidate(fetch_siblings=counting_fetch) is None
    assert queue.next_candidate(fetch_siblings=counting_fetch) is None
    assert call_count["n"] == 1  # below side fetched once and cached empty, not re-fetched


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


def _provider_candidate(repo_id, filename, provider):
    return {"repo_id": repo_id, "filename": filename, "name": filename, "provider": provider}


def test_phase_a_tries_all_huggingface_before_any_modelscope(monkeypatch):
    hf_lo = _provider_candidate("o", "hf_lo.gguf", "huggingface")
    ms_hi = _provider_candidate("o", "ms_hi.gguf", "modelscope")
    # ModelScope candidate outranks the HF one on predicted speed, but HF
    # must still be tried first (ModelScope downloads are far slower - see
    # contribute._prefer_huggingface).
    ranked = [(ms_hi, 90.0), (hf_lo, 10.0)]
    monkeypatch.setattr(contribute.predictor, "rank_candidates", lambda artifact, hw: ranked)

    queue = contribute.ContributionQueue({}, _hw(), history_refs=set())

    assert queue.next_candidate() is hf_lo
    assert queue.next_candidate() is ms_hi


def test_phase_a_preserves_score_order_within_each_provider(monkeypatch):
    hf_hi = _provider_candidate("o", "hf_hi.gguf", "huggingface")
    hf_lo = _provider_candidate("o", "hf_lo.gguf", "huggingface")
    ms = _provider_candidate("o", "ms.gguf", "modelscope")
    ranked = [(hf_hi, 50.0), (ms, 30.0), (hf_lo, 10.0)]
    monkeypatch.setattr(contribute.predictor, "rank_candidates", lambda artifact, hw: ranked)

    queue = contribute.ContributionQueue({}, _hw(), history_refs=set())

    assert queue.next_candidate() is hf_hi
    assert queue.next_candidate() is hf_lo
    assert queue.next_candidate() is ms


def test_phase_b_below_pool_tries_huggingface_before_modelscope(monkeypatch):
    hf_weak = _provider_candidate("o", "hf_weak.gguf", "huggingface")
    ms_strong = _provider_candidate("o", "ms_strong.gguf", "modelscope")
    ranked = [(ms_strong, 90.0), (hf_weak, 10.0)]
    monkeypatch.setattr(contribute.predictor, "rank_candidates", lambda artifact, hw: ranked)

    queue = contribute.ContributionQueue({}, _hw(), history_refs=set())
    queue.next_candidate()  # drains phase A: hf_weak
    queue.next_candidate()  # drains phase A: ms_strong

    # below_pool (weakest-still-viable side) should still exhaust HF first
    assert queue.next_candidate() is hf_weak


def test_phase_b_above_pool_tries_huggingface_before_modelscope(monkeypatch):
    hf_unviable = _provider_candidate("o", "hf_unviable.gguf", "huggingface")
    ms_unviable = _provider_candidate("o", "ms_unviable.gguf", "modelscope")
    # ModelScope candidate is the less-bad unviable one by score, but HF
    # should still be tried first on the above-pool side too.
    ranked = [(ms_unviable, -1.0), (hf_unviable, -5.0)]
    monkeypatch.setattr(contribute.predictor, "rank_candidates", lambda artifact, hw: ranked)

    queue = contribute.ContributionQueue({}, _hw(), history_refs=set())

    assert queue.next_candidate() is hf_unviable  # above, cursor 0


def test_excluded_refs_are_never_offered_by_phase_a_or_phase_b(monkeypatch):
    a, b, c = _candidate("o", "a.gguf"), _candidate("o", "b.gguf"), _candidate("o", "c.gguf")
    ranked = [(a, 50.0), (b, 30.0), (c, -5.0)]
    monkeypatch.setattr(contribute.predictor, "rank_candidates", lambda artifact, hw: ranked)

    queue = contribute.ContributionQueue(
        {}, _hw(), history_refs=set(), excluded_refs={contribute.ref(a), contribute.ref(c)}
    )

    # Phase A hands out b, then phase B keeps re-offering the one candidate
    # that is neither excluded nor viable-and-unseen. a and c never appear.
    offered = [queue.next_candidate() for _ in range(6)]
    assert offered == [b] * 6


def test_excluded_ref_is_not_counted_as_a_covered_candidate(monkeypatch):
    """`history_refs` is what the session reports as candidates it covered.
    A candidate held back by a cooldown was never tried, so it must not
    inflate that count."""
    a = _candidate("o", "a.gguf")
    monkeypatch.setattr(contribute.predictor, "rank_candidates", lambda artifact, hw: [(a, 50.0)])

    queue = contribute.ContributionQueue(
        {}, _hw(), history_refs=set(), excluded_refs={contribute.ref(a)}
    )

    assert queue.history_refs == set()


def test_releasing_deferrals_does_not_release_an_excluded_ref(monkeypatch):
    """The loop releases every bounded deferral after each completed
    attempt. An excluded ref is not a deferral and must survive that."""
    a = _candidate("o", "a.gguf")
    monkeypatch.setattr(contribute.predictor, "rank_candidates", lambda artifact, hw: [(a, 50.0)])
    queue = contribute.ContributionQueue(
        {}, _hw(), history_refs=set(), excluded_refs={contribute.ref(a)}
    )

    queue.release_deferred(contribute.ref(a))

    assert queue.next_candidate() is None
