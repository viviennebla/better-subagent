"""SQLite persistence for the single-device Coordinator runtime."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class SqliteGatewayStore:
    """Transactional SQLite store preserving the existing Gateway state shape.

    Phase 1 intentionally keeps the Gateway domain service unchanged: callers
    still mutate one in-memory state snapshot inside ``locked()`` and persist it
    with ``save()``. SQLite provides process-safe transactions, durable
    idempotency constraints, and restart recovery without changing HTTP or App
    Server transport behavior.
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: Path, *, legacy_json_path: Path | None = None) -> None:
        self.path = path
        self.legacy_json_path = legacy_json_path
        self._thread_lock = threading.RLock()
        self._local = threading.local()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    gateway_run_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    prompt_hash TEXT,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS runs_session_status_idx
                    ON runs(session_id, status);
                CREATE TABLE IF NOT EXISTS pending_approvals (
                    request_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL
                );
                """
            )
            connection.execute("BEGIN IMMEDIATE")
            try:
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()
                if version is not None and version[0] != str(self.SCHEMA_VERSION):
                    raise RuntimeError(
                        f"better-subagent SQLite schemaVersion 非 {self.SCHEMA_VERSION}"
                    )
                if version is None:
                    connection.execute(
                        "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                        (str(self.SCHEMA_VERSION),),
                    )
                self._migrate_legacy_json_if_needed(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        finally:
            connection.close()

    def _migrate_legacy_json_if_needed(self, connection: sqlite3.Connection) -> None:
        migrated = connection.execute(
            "SELECT value FROM metadata WHERE key = 'legacy_json_migrated'"
        ).fetchone()
        if migrated is not None:
            return
        populated = any(
            connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
            for table in ("sessions", "runs", "pending_approvals")
        )
        source = self.legacy_json_path
        if not populated and source is not None and source.exists():
            state = self._read_legacy_json(source)
            self._replace_state(connection, state)
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('legacy_json_source', ?)",
                (str(source),),
            )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('legacy_json_migrated', '1')"
        )

    @staticmethod
    def _read_legacy_json(path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        SqliteGatewayStore._validate_state(value)
        return value

    @staticmethod
    def _validate_state(value: Any) -> None:
        if not isinstance(value, dict) or value.get("schemaVersion") != 1:
            raise RuntimeError("better-subagent 数据文件 schemaVersion 非 1")
        if not isinstance(value.get("sessions"), dict) or not isinstance(value.get("runs"), list):
            raise RuntimeError("better-subagent 数据文件结构无效")
        pending = value.get("pendingApprovals", {})
        if not isinstance(pending, dict):
            raise RuntimeError("better-subagent pendingApprovals 结构无效")

    @contextmanager
    def locked(self) -> Iterator[dict[str, Any]]:
        with self._thread_lock:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            self._local.connection = connection
            try:
                value = self._read_state(connection)
                yield value
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                self._local.connection = None
                connection.close()

    def read(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            value = self._read_state(connection)
            connection.commit()
            return value
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def save(self, value: dict[str, Any]) -> None:
        self._validate_state(value)
        connection = getattr(self._local, "connection", None)
        if connection is None:
            raise RuntimeError("SqliteGatewayStore.save() 必须在 locked() transaction 内调用")
        self._replace_state(connection, value)

    def _read_state(self, connection: sqlite3.Connection) -> dict[str, Any]:
        sessions = {
            row["session_id"]: json.loads(row["payload_json"])
            for row in connection.execute(
                "SELECT session_id, payload_json FROM sessions ORDER BY session_id"
            )
        }
        runs = [
            json.loads(row["payload_json"])
            for row in connection.execute(
                "SELECT payload_json FROM runs ORDER BY rowid"
            )
        ]
        approvals = {
            row["request_id"]: json.loads(row["payload_json"])
            for row in connection.execute(
                "SELECT request_id, payload_json FROM pending_approvals ORDER BY request_id"
            )
        }
        return {
            "schemaVersion": self.SCHEMA_VERSION,
            "sessions": sessions,
            "runs": runs,
            "pendingApprovals": approvals,
        }

    def _replace_state(self, connection: sqlite3.Connection, value: dict[str, Any]) -> None:
        self._validate_state(value)
        sessions = value["sessions"]
        runs = value["runs"]
        approvals = value.get("pendingApprovals", {})

        connection.execute("DELETE FROM sessions")
        for session_id, payload in sessions.items():
            connection.execute(
                "INSERT INTO sessions(session_id, payload_json) VALUES(?, ?)",
                (str(session_id), json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
            )

        connection.execute("DELETE FROM runs")
        for run in runs:
            if not isinstance(run, dict):
                raise RuntimeError("better-subagent Run 结构无效")
            gateway_run_id = run.get("gatewayRunId")
            request_id = run.get("requestId")
            session_id = run.get("sessionId")
            status = run.get("status")
            if not all(isinstance(item, str) and item for item in (gateway_run_id, request_id, session_id, status)):
                raise RuntimeError("better-subagent Run identity 结构无效")
            connection.execute(
                """INSERT INTO runs(
                       gateway_run_id, request_id, session_id, status, prompt_hash, payload_json
                   ) VALUES(?, ?, ?, ?, ?, ?)""",
                (
                    gateway_run_id,
                    request_id,
                    session_id,
                    status,
                    run.get("promptHash"),
                    json.dumps(run, ensure_ascii=False, separators=(",", ":")),
                ),
            )

        connection.execute("DELETE FROM pending_approvals")
        for request_id, payload in approvals.items():
            connection.execute(
                "INSERT INTO pending_approvals(request_id, payload_json) VALUES(?, ?)",
                (str(request_id), json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
            )


# Source compatibility for existing embedders/tests. Despite the historical
# name this is SQLite-backed; new code should import SqliteGatewayStore.
JsonGatewayStore = SqliteGatewayStore