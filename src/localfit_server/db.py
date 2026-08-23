"""Small SQLite persistence layer with no cloud dependency."""

from __future__ import annotations

import json
import hashlib
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS benchmarks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    recorded_at TEXT NOT NULL,
    engine TEXT NOT NULL,
    benchmark_version INTEGER NOT NULL,
    ram_gb REAL NOT NULL,
    vram_gb REAL,
    unified_memory INTEGER NOT NULL,
    gpu_tflops REAL,
    model_installed TEXT NOT NULL,
    model_repo_id TEXT,
    model_size_bytes INTEGER,
    tokens_per_sec REAL NOT NULL,
    sample_count INTEGER,
    tokens_per_sec_min REAL,
    tokens_per_sec_max REAL,
    runtime_profile TEXT,
    context_length INTEGER,
    gpu_offload_percent INTEGER,
    cpu_threads INTEGER,
    num_batch INTEGER,
    event_hash TEXT,
    event_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_benchmarks_recorded_at ON benchmarks(recorded_at);
CREATE INDEX IF NOT EXISTS idx_benchmarks_engine ON benchmarks(engine);
"""


@dataclass(frozen=True)
class InsertResult:
    id: int
    created: bool


class BenchmarkStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._session() as connection:
            # journal_mode=WAL is persisted in the database file itself, so
            # setting it once here is enough - every later connection opens
            # in WAL mode automatically without needing to reissue the pragma.
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(benchmarks)").fetchall()
            }
            if "event_hash" not in columns:
                connection.execute("ALTER TABLE benchmarks ADD COLUMN event_hash TEXT")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_benchmarks_event_hash "
                "ON benchmarks(event_hash) WHERE event_hash IS NOT NULL"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _session(self):
        # sqlite3.Connection's own context manager only commits/rolls back;
        # it does not close the connection. Close explicitly so this doesn't
        # depend on refcount-triggered GC to release the file handle.
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def insert(self, event: dict[str, Any]) -> InsertResult:
        fields = [
            "recorded_at",
            "engine",
            "benchmark_version",
            "ram_gb",
            "vram_gb",
            "unified_memory",
            "gpu_tflops",
            "model_installed",
            "model_repo_id",
            "model_size_bytes",
            "tokens_per_sec",
            "sample_count",
            "tokens_per_sec_min",
            "tokens_per_sec_max",
            "runtime_profile",
            "context_length",
            "gpu_offload_percent",
            "cpu_threads",
            "num_batch",
        ]
        event_json = json.dumps(event, separators=(",", ":"), sort_keys=True)
        event_hash = hashlib.sha256(event_json.encode()).hexdigest()
        fields.append("event_hash")
        values = [event.get(field) for field in fields[:-1]]
        # The original schema predates v7 failure telemetry and keeps this
        # materialized column NOT NULL. Failure rows intentionally omit speed;
        # store a non-exported sentinel in the legacy column while event_json
        # preserves the exact contract (with no fabricated tokens_per_sec).
        tokens_index = fields.index("tokens_per_sec")
        if values[tokens_index] is None:
            values[tokens_index] = 0
        values.append(event_hash)
        values.append(event_json)
        placeholders = ", ".join("?" for _ in values)
        with self._session() as connection:
            cursor = connection.execute(
                f"INSERT OR IGNORE INTO benchmarks ({', '.join(fields)}, event_json) "
                f"VALUES ({placeholders})",
                values,
            )
            if cursor.rowcount == 1:
                return InsertResult(int(cursor.lastrowid), True)
            row = connection.execute(
                "SELECT id FROM benchmarks WHERE event_hash = ?", (event_hash,)
            ).fetchone()
            return InsertResult(int(row["id"]), False)

    def count(self) -> int:
        with self._session() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM benchmarks").fetchone()
        return int(row["count"])

    def engine_counts(self) -> dict[str, int]:
        with self._session() as connection:
            rows = connection.execute(
                "SELECT engine, COUNT(*) AS count FROM benchmarks GROUP BY engine"
            ).fetchall()
        return {str(row["engine"]): int(row["count"]) for row in rows}

    def export(self, limit: int = 100_000) -> list[dict[str, Any]]:
        with self._session() as connection:
            rows = connection.execute(
                "SELECT event_json FROM benchmarks ORDER BY id ASC LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(row["event_json"]) for row in rows]
