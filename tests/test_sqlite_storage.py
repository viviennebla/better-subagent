from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from better_subagent.storage import SqliteGatewayStore


class SqliteGatewayStoreTest(unittest.TestCase):
    def test_legacy_json_migrates_once_with_all_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "better-subagent.json"
            database = root / "runtime.sqlite3"
            state = {
                "schemaVersion": 1,
                "sessions": {"session-1": {"sessionId": "session-1", "threadId": "thread-1"}},
                "runs": [{
                    "gatewayRunId": "run-1",
                    "requestId": "request-1",
                    "sessionId": "session-1",
                    "promptHash": "a" * 64,
                    "status": "unknown",
                    "createdAt": "2026-10-06T00:00:00+00:00",
                    "updatedAt": "2026-10-06T00:00:00+00:00",
                }],
                "pendingApprovals": {"approval-1": {"requestId": "approval-1", "status": "pending"}},
            }
            legacy.write_text(json.dumps(state), encoding="utf-8")

            first = SqliteGatewayStore(database, legacy_json_path=legacy)
            self.assertEqual(first.read(), state)

            changed = dict(state)
            changed["sessions"] = {}
            legacy.write_text(json.dumps(changed), encoding="utf-8")
            reopened = SqliteGatewayStore(database, legacy_json_path=legacy)
            self.assertEqual(reopened.read(), state)

            with sqlite3.connect(database) as connection:
                source = connection.execute(
                    "SELECT value FROM metadata WHERE key='legacy_json_source'"
                ).fetchone()
                self.assertEqual(source[0], str(legacy))

    def test_invalid_legacy_json_fails_closed_without_marking_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "better-subagent.json"
            database = root / "runtime.sqlite3"
            legacy.write_text('{"schemaVersion": 999}', encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "schemaVersion"):
                SqliteGatewayStore(database, legacy_json_path=legacy)

            legacy.write_text(json.dumps({
                "schemaVersion": 1, "sessions": {}, "runs": [], "pendingApprovals": {}
            }), encoding="utf-8")
            recovered = SqliteGatewayStore(database, legacy_json_path=legacy)
            self.assertEqual(recovered.read()["runs"], [])

    def test_phase1_database_schema_upgrades_for_device_agent_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "runtime.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    INSERT INTO metadata(key, value) VALUES('schema_version', '1');
                    CREATE TABLE sessions (session_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
                    CREATE TABLE runs (gateway_run_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, session_id TEXT NOT NULL, status TEXT NOT NULL, prompt_hash TEXT, payload_json TEXT NOT NULL);
                    CREATE TABLE pending_approvals (request_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
                    """
                )

            store = SqliteGatewayStore(database)
            store.upsert_device({
                "deviceId": "dev-1", "environment": "dev", "status": "online"
            })
            store.upsert_agent({
                "agentId": "runtime@dev-1", "deviceId": "dev-1", "role": "runtime", "enabled": True
            })

            with sqlite3.connect(database) as connection:
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0]
            self.assertEqual(version, "2")
            self.assertEqual(store.list_devices()[0]["deviceId"], "dev-1")
            self.assertEqual(store.list_agents()[0]["agentId"], "runtime@dev-1")

    def test_request_id_uniqueness_is_enforced_by_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteGatewayStore(Path(directory) / "runtime.sqlite3")
            with self.assertRaises(sqlite3.IntegrityError):
                with store.locked() as state:
                    base = {
                        "sessionId": "session-1",
                        "promptHash": "a" * 64,
                        "status": "starting",
                        "createdAt": "2026-10-06T00:00:00+00:00",
                        "updatedAt": "2026-10-06T00:00:00+00:00",
                    }
                    state["runs"] = [
                        {**base, "gatewayRunId": "run-1", "requestId": "duplicate"},
                        {**base, "gatewayRunId": "run-2", "requestId": "duplicate"},
                    ]
                    store.save(state)
            self.assertEqual(store.read()["runs"], [])


if __name__ == "__main__":
    unittest.main()