import json
from datetime import datetime, timedelta, timezone

from omm import benchmark_history


def test_loaded_refs_empty_when_no_file(isolated_omm_home):
    assert benchmark_history.loaded_refs() == set()


def test_invalid_history_shapes_are_treated_as_empty(isolated_omm_home):
    path = isolated_omm_home / "benchmark_history.json"
    for payload in ([], {"entries": [], "failures": "bad"}):
        path.write_text(json.dumps(payload))
        assert benchmark_history.loaded_refs() == set()
        assert benchmark_history.failure_cooldowns() == {}


def test_has_been_benchmarked_false_for_unknown_ref(isolated_omm_home):
    assert benchmark_history.has_been_benchmarked("org/repo:model.gguf") is False


def test_record_benchmarked_then_has_been_benchmarked_true(isolated_omm_home):
    benchmark_history.record_benchmarked(
        "org/repo:model.gguf",
        repo_id="org/repo",
        filename="model.gguf",
        sha256="deadbeef",
        tokens_per_sec=12.5,
    )

    assert benchmark_history.has_been_benchmarked("org/repo:model.gguf") is True
    assert benchmark_history.loaded_refs() == {"org/repo:model.gguf"}


def test_record_benchmarked_stores_metadata_on_disk(isolated_omm_home):
    benchmark_history.record_benchmarked(
        "org/repo:model.gguf",
        repo_id="org/repo",
        filename="model.gguf",
        sha256="deadbeef",
        tokens_per_sec=12.5,
    )

    data = json.loads((isolated_omm_home / "benchmark_history.json").read_text())
    entry = data["entries"]["org/repo:model.gguf"]
    assert entry["repo_id"] == "org/repo"
    assert entry["filename"] == "model.gguf"
    assert entry["sha256"] == "deadbeef"
    assert entry["tokens_per_sec"] == 12.5
    assert "benchmarked_at" in entry


def test_multiple_records_accumulate(isolated_omm_home):
    benchmark_history.record_benchmarked(
        "a:x.gguf", repo_id="a", filename="x.gguf", sha256="1", tokens_per_sec=1.0
    )
    benchmark_history.record_benchmarked(
        "b:y.gguf", repo_id="b", filename="y.gguf", sha256="2", tokens_per_sec=2.0
    )

    assert benchmark_history.loaded_refs() == {"a:x.gguf", "b:y.gguf"}


def test_recorded_failure_does_not_read_as_a_benchmark(isolated_omm_home):
    benchmark_history.record_benchmark_failure(
        "org/repo:model.gguf",
        repo_id="org/repo",
        filename="model.gguf",
        reason="memory_pressure_cancelled",
        engine="ollama",
    )

    assert benchmark_history.has_been_benchmarked("org/repo:model.gguf") is False
    assert benchmark_history.loaded_refs() == set()
    record = benchmark_history.failure_record("org/repo:model.gguf")
    assert record["reason"] == "memory_pressure_cancelled"
    assert record["outcome"] == "machine_failure"
    assert record["engine"] == "ollama"
    assert record["consecutive_machine_failures"] == 1
    assert record["first_failed_at"] == record["last_failed_at"]


def test_history_file_written_before_failures_existed_still_loads(isolated_omm_home):
    (isolated_omm_home / "benchmark_history.json").write_text(
        json.dumps(
            {
                "entries": {
                    "org/repo:model.gguf": {
                        "repo_id": "org/repo",
                        "filename": "model.gguf",
                        "sha256": "deadbeef",
                        "tokens_per_sec": 12.5,
                        "benchmarked_at": "2026-01-01T00:00:00+00:00",
                    }
                }
            }
        )
    )

    assert benchmark_history.loaded_refs() == {"org/repo:model.gguf"}
    assert benchmark_history.failure_record("org/repo:model.gguf") is None
    assert benchmark_history.failure_cooldowns() == {}

    benchmark_history.record_benchmark_failure(
        "org/other:new.gguf",
        repo_id="org/other",
        filename="new.gguf",
        reason="memory_pressure_cancelled",
    )

    # The pre-existing success survives the first write of the new section.
    assert benchmark_history.loaded_refs() == {"org/repo:model.gguf"}


def test_single_machine_failure_does_not_start_a_cooldown(isolated_omm_home):
    benchmark_history.record_benchmark_failure(
        "org/repo:model.gguf",
        repo_id="org/repo",
        filename="model.gguf",
        reason="memory_pressure_cancelled",
    )

    assert benchmark_history.failure_cooldowns() == {}


def test_two_machine_failures_in_a_row_start_a_cooldown(isolated_omm_home):
    for _ in range(2):
        benchmark_history.record_benchmark_failure(
            "org/repo:model.gguf",
            repo_id="org/repo",
            filename="model.gguf",
            reason="memory_pressure_cancelled",
        )

    assert benchmark_history.machine_failure_streak("org/repo:model.gguf") == 2
    assert set(benchmark_history.failure_cooldowns()) == {"org/repo:model.gguf"}


def test_cooldown_lapses_once_its_window_has_passed(isolated_omm_home):
    for _ in range(2):
        benchmark_history.record_benchmark_failure(
            "org/repo:model.gguf",
            repo_id="org/repo",
            filename="model.gguf",
            reason="memory_pressure_cancelled",
        )
    later = datetime.now(timezone.utc) + timedelta(
        hours=benchmark_history.MACHINE_FAILURE_COOLDOWN_HOURS + 1
    )

    assert benchmark_history.failure_cooldowns(now=later) == {}
    # The streak itself is kept: it is history, not a suppression switch.
    assert benchmark_history.machine_failure_streak("org/repo:model.gguf") == 2


def test_repeated_non_machine_failures_never_start_a_cooldown(isolated_omm_home):
    for _ in range(5):
        benchmark_history.record_benchmark_failure(
            "org/repo:model.gguf",
            repo_id="org/repo",
            filename="model.gguf",
            reason="generation_timeout",
        )

    assert benchmark_history.machine_failure_streak("org/repo:model.gguf") == 0
    assert benchmark_history.failure_cooldowns() == {}
    assert benchmark_history.failure_record("org/repo:model.gguf")["outcome"] == "other_failure"


def test_non_machine_failure_does_not_extend_a_running_cooldown(isolated_omm_home):
    for _ in range(2):
        benchmark_history.record_benchmark_failure(
            "org/repo:model.gguf",
            repo_id="org/repo",
            filename="model.gguf",
            reason="memory_pressure_cancelled",
        )
    machine_clock = benchmark_history.failure_record("org/repo:model.gguf")[
        "last_machine_failure_at"
    ]

    benchmark_history.record_benchmark_failure(
        "org/repo:model.gguf",
        repo_id="org/repo",
        filename="model.gguf",
        reason="generation_timeout",
    )
    record = benchmark_history.failure_record("org/repo:model.gguf")

    assert record["last_machine_failure_at"] == machine_clock
    assert record["last_failed_at"] != machine_clock


def test_successful_benchmark_clears_the_failure_streak(isolated_omm_home):
    for _ in range(2):
        benchmark_history.record_benchmark_failure(
            "org/repo:model.gguf",
            repo_id="org/repo",
            filename="model.gguf",
            reason="memory_pressure_cancelled",
        )

    benchmark_history.record_benchmarked(
        "org/repo:model.gguf",
        repo_id="org/repo",
        filename="model.gguf",
        sha256="deadbeef",
        tokens_per_sec=12.5,
    )

    assert benchmark_history.failure_record("org/repo:model.gguf") is None
    assert benchmark_history.machine_failure_streak("org/repo:model.gguf") == 0
    assert benchmark_history.failure_cooldowns() == {}


def test_unreadable_cooldown_timestamp_never_suppresses_forever(isolated_omm_home):
    for _ in range(2):
        benchmark_history.record_benchmark_failure(
            "org/repo:model.gguf",
            repo_id="org/repo",
            filename="model.gguf",
            reason="memory_pressure_cancelled",
        )
    path = isolated_omm_home / "benchmark_history.json"
    data = json.loads(path.read_text())
    data["failures"]["org/repo:model.gguf"]["last_machine_failure_at"] = "not-a-timestamp"
    path.write_text(json.dumps(data))

    assert benchmark_history.failure_cooldowns() == {}


def test_corrupt_history_is_backed_up_before_being_reset(isolated_omm_home):
    """Like config.json/models.json: an unreadable file is preserved, not
    silently replaced by the next record with an empty history."""
    path = isolated_omm_home / "benchmark_history.json"
    path.write_text("{not json")

    assert benchmark_history.loaded_refs() == set()

    backups = list(isolated_omm_home.glob("benchmark_history.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == "{not json"
