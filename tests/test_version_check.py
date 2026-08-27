import json
import time

from omm import version_check


def test_cached_remote_head_calls_fetch_on_cold_cache(isolated_omm_home):
    calls = []

    def fetch(ref):
        calls.append(ref)
        return "abc123"

    result = version_check.cached_remote_head(fetch, ref="main")

    assert result == "abc123"
    assert calls == ["main"]


def test_non_object_cache_is_treated_as_empty(isolated_omm_home):
    (isolated_omm_home / "update_check.json").write_text("[]")

    assert version_check.cached_remote_head(lambda ref: "fresh") == "fresh"


def test_future_cache_timestamp_is_not_treated_as_fresh(isolated_omm_home):
    (isolated_omm_home / "update_check.json").write_text(
        json.dumps(
            {
                "main": {
                    "checked_at": time.time() + 3600,
                    "remote_head": "future-value",
                }
            }
        )
    )

    assert version_check.cached_remote_head(lambda ref: "fresh") == "fresh"


def test_cached_remote_head_uses_cache_within_ttl(isolated_omm_home):
    (isolated_omm_home / "update_check.json").write_text(
        json.dumps({"main": {"checked_at": time.time(), "remote_head": "cached_sha"}})
    )

    def fetch(ref):
        raise AssertionError("fetch should not be called while cache is warm")

    result = version_check.cached_remote_head(fetch, ref="main", ttl_seconds=1800)

    assert result == "cached_sha"


def test_cached_remote_head_refetches_after_ttl_expires(isolated_omm_home):
    (isolated_omm_home / "update_check.json").write_text(
        json.dumps({"main": {"checked_at": time.time() - 9999, "remote_head": "old_sha"}})
    )

    result = version_check.cached_remote_head(lambda ref: "new_sha", ref="main", ttl_seconds=1800)

    assert result == "new_sha"


def test_cached_remote_head_caches_none_result_without_refetching(isolated_omm_home):
    calls = []

    def fetch(ref):
        calls.append(ref)
        return None

    first = version_check.cached_remote_head(fetch, ref="main", ttl_seconds=1800)
    second = version_check.cached_remote_head(fetch, ref="main", ttl_seconds=1800)

    assert first is None
    assert second is None
    assert calls == ["main"]


def test_cached_remote_head_keeps_channels_separate(isolated_omm_home):
    """Switching update channel (stable/beta) must never read the other
    channel's cached remote head - they're cached under separate keys."""
    calls = []

    def fetch(ref):
        calls.append(ref)
        return {"main": "main_sha", "beta": "beta_sha"}[ref]

    main_result = version_check.cached_remote_head(fetch, ref="main", ttl_seconds=1800)
    beta_result = version_check.cached_remote_head(fetch, ref="beta", ttl_seconds=1800)
    # Re-reading "main" must still hit its own warm cache, not beta's.
    main_again = version_check.cached_remote_head(fetch, ref="main", ttl_seconds=1800)

    assert main_result == "main_sha"
    assert beta_result == "beta_sha"
    assert main_again == "main_sha"
    assert calls == ["main", "beta"]


def test_cached_remote_head_if_fresh_returns_false_on_cold_cache(isolated_omm_home):
    assert version_check.cached_remote_head_if_fresh() == (False, None, None)


def test_cached_remote_head_if_fresh_returns_cached_value_within_ttl(isolated_omm_home):
    (isolated_omm_home / "update_check.json").write_text(
        json.dumps({"main": {"checked_at": time.time(), "remote_head": "cached_sha", "installed": "local_sha"}})
    )

    assert version_check.cached_remote_head_if_fresh(ttl_seconds=1800) == (True, "cached_sha", "local_sha")


def test_cached_remote_head_if_fresh_returns_none_installed_when_absent(isolated_omm_home):
    (isolated_omm_home / "update_check.json").write_text(
        json.dumps({"main": {"checked_at": time.time(), "remote_head": "cached_sha"}})
    )

    assert version_check.cached_remote_head_if_fresh(ttl_seconds=1800) == (True, "cached_sha", None)


def test_cached_remote_head_if_fresh_returns_false_after_ttl_expires(isolated_omm_home):
    (isolated_omm_home / "update_check.json").write_text(
        json.dumps({"main": {"checked_at": time.time() - 9999, "remote_head": "old_sha"}})
    )

    assert version_check.cached_remote_head_if_fresh(ttl_seconds=1800) == (False, None, None)


def test_cached_remote_head_if_fresh_ignores_other_channels_cache(isolated_omm_home):
    (isolated_omm_home / "update_check.json").write_text(
        json.dumps({"beta": {"checked_at": time.time(), "remote_head": "beta_sha"}})
    )

    assert version_check.cached_remote_head_if_fresh("main", ttl_seconds=1800) == (False, None, None)


def test_cached_remote_head_records_installed_alongside_remote_head(isolated_omm_home):
    version_check.cached_remote_head(lambda ref: "fresh_sha", ref="main", installed="local_sha")

    cache = json.loads((isolated_omm_home / "update_check.json").read_text())
    assert cache["main"]["remote_head"] == "fresh_sha"
    assert cache["main"]["installed"] == "local_sha"


def test_should_start_check_true_on_cold_cache(isolated_omm_home):
    assert version_check.should_start_check() is True


def test_should_start_check_false_within_ttl(isolated_omm_home):
    (isolated_omm_home / "update_check.json").write_text(
        json.dumps({"main": {"checked_at": time.time(), "remote_head": "cached_sha"}})
    )

    assert version_check.should_start_check(ttl_seconds=1800) is False


def test_should_start_check_false_when_another_check_already_in_flight(isolated_omm_home):
    (isolated_omm_home / "update_check.json").write_text(
        json.dumps({"main": {"checked_at": time.time() - 9999, "checking_since": time.time()}})
    )

    assert version_check.should_start_check(ttl_seconds=1800) is False


def test_should_start_check_true_when_in_flight_marker_is_stale(isolated_omm_home):
    (isolated_omm_home / "update_check.json").write_text(
        json.dumps({"main": {"checked_at": time.time() - 9999, "checking_since": time.time() - 9999}})
    )

    assert version_check.should_start_check(ttl_seconds=1800) is True


def test_mark_checking_sets_timestamp_without_clobbering_checked_at(isolated_omm_home):
    checked_at = time.time() - 9999
    (isolated_omm_home / "update_check.json").write_text(
        json.dumps({"main": {"checked_at": checked_at, "remote_head": "old_sha"}})
    )

    assert version_check.mark_checking() is True

    cache = json.loads((isolated_omm_home / "update_check.json").read_text())
    assert cache["main"]["checked_at"] == checked_at
    assert cache["main"]["remote_head"] == "old_sha"
    assert isinstance(cache["main"]["checking_since"], float)


def test_mark_checking_allows_only_one_fresh_claim(isolated_omm_home):
    assert version_check.mark_checking() is True
    assert version_check.mark_checking() is False


def test_mark_checking_returns_false_when_claim_cannot_be_persisted(
    isolated_omm_home, monkeypatch
):
    monkeypatch.setattr(
        version_check,
        "atomic_write_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    assert version_check.mark_checking() is False


def test_record_keeps_other_channels_untouched(isolated_omm_home):
    (isolated_omm_home / "update_check.json").write_text(
        json.dumps({"beta": {"checked_at": time.time(), "remote_head": "beta_sha"}})
    )

    version_check.record("main_sha", "main")

    cache = json.loads((isolated_omm_home / "update_check.json").read_text())
    assert cache["main"]["remote_head"] == "main_sha"
    assert cache["beta"]["remote_head"] == "beta_sha"


def test_record_stores_installed(isolated_omm_home):
    version_check.record("main_sha", "main", installed="local_sha")

    cache = json.loads((isolated_omm_home / "update_check.json").read_text())
    assert cache["main"]["installed"] == "local_sha"


def test_mark_reconfirming_true_on_cold_cache(isolated_omm_home):
    assert version_check.mark_reconfirming() is True


def test_mark_reconfirming_true_even_when_cache_is_fresh(isolated_omm_home):
    """Unlike `mark_checking`, a reconfirm is warranted precisely because the
    live-read `installed` moved since a still-fresh cache entry was written -
    it must not be blocked by the entry's own TTL freshness."""
    (isolated_omm_home / "update_check.json").write_text(
        json.dumps({"main": {"checked_at": time.time(), "remote_head": "cached_sha", "installed": "older_sha"}})
    )

    assert version_check.mark_reconfirming() is True


def test_mark_reconfirming_allows_only_one_fresh_claim(isolated_omm_home):
    assert version_check.mark_reconfirming() is True
    assert version_check.mark_reconfirming() is False


def test_mark_reconfirming_shares_in_flight_marker_with_mark_checking(isolated_omm_home):
    """Both spawn paths write the same `checking_since` field, so a stale-
    cache check already in flight also blocks a reconfirm spawn for the same
    ref, and vice versa - only one detached `git ls-remote` child at a time."""
    assert version_check.mark_checking() is True
    assert version_check.mark_reconfirming() is False
