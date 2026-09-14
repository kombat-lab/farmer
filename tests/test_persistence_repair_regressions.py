from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import legacy_combat_diagnostics as legacy_diagnostics
from battle_records import BattleOutcome, SourceEventId
from bounded_values import INT64_MAX, INT64_MIN
from storage import Storage
from tests.test_legacy_combat_diagnostics import legacy_trace


class LegacyDiagnosticsMigrationRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = Storage(Path(":memory:"))
        self.diagnostics = legacy_diagnostics.LegacyCombatDiagnostics(self.store)
        self.outcome = BattleOutcome(
            source_event_id=SourceEventId("audit:diagnostics-migration"),
            source_message_id=41,
            session_id=None,
            target_name="Моль",
            result="VICTORY",
        )

    async def asyncTearDown(self) -> None:
        await self.store.close()

    def install_v1_analysis_schema(
        self,
        *,
        battle_id: int,
        include_orphan: bool = True,
        with_metadata: bool = False,
    ) -> None:
        legacy_diagnostics._execute_schema(
            self.store.connection, legacy_diagnostics._DIAGNOSTIC_SCHEMA_V1
        )
        if with_metadata:
            self.store.connection.execute(
                """
                CREATE TABLE legacy_combat_diagnostics_meta (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    schema_version INTEGER NOT NULL,
                    schema_fingerprint TEXT NOT NULL
                )
                """
            )
            self.store.connection.execute(
                "INSERT INTO legacy_combat_diagnostics_meta VALUES (1,1,?)",
                (legacy_diagnostics._SCHEMA_FINGERPRINT_V1,),
            )
        rows = [(battle_id, "Моль")]
        if include_orphan:
            rows.append((999, "Сирота"))
        self.store.connection.executemany(
            """
            INSERT INTO combat_battle_analysis(
                battle_id,target_name,result,happened_at,policy_key,created_at
            ) VALUES (?,?,'VICTORY','2020-01-01T00:00:00+00:00',
                      'legacy','2020-01-01T00:00:01+00:00')
            """,
            rows,
        )
        self.store.connection.commit()

    async def test_versionless_v1_migration_preserves_valid_rows_and_drops_orphans(
        self,
    ) -> None:
        recorded = await self.store.record_battle_outcome(self.outcome)
        self.install_v1_analysis_schema(battle_id=recorded.battle_id)

        await self.diagnostics.initialize()

        rows = self.store.connection.execute(
            "SELECT battle_id,target_name FROM combat_battle_analysis ORDER BY battle_id"
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [(recorded.battle_id, "Моль")])
        foreign_keys = self.store.connection.execute(
            "PRAGMA foreign_key_list(combat_battle_analysis)"
        ).fetchall()
        self.assertEqual(
            [(row[2], row[3], row[4], row[6]) for row in foreign_keys],
            [("battles", "battle_id", "id", "CASCADE")],
        )
        indexes = {
            row[1]
            for row in self.store.connection.execute(
                "PRAGMA index_list(combat_battle_analysis)"
            )
        }
        self.assertTrue(
            {
                "idx_combat_analysis_profile_target",
                "idx_combat_analysis_happened_at",
            }.issubset(indexes)
        )
        metadata = self.store.connection.execute(
            "SELECT schema_version,schema_fingerprint "
            "FROM legacy_combat_diagnostics_meta WHERE singleton=1"
        ).fetchone()
        self.assertEqual(
            tuple(metadata),
            (
                legacy_diagnostics.LEGACY_DIAGNOSTICS_SCHEMA_VERSION,
                legacy_diagnostics._SCHEMA_FINGERPRINT,
            ),
        )
        self.assertEqual(
            self.store.connection.execute(
                "PRAGMA foreign_key_check(combat_battle_analysis)"
            ).fetchall(),
            [],
        )

    async def test_late_v1_migration_failure_restores_table_rows_and_metadata(
        self,
    ) -> None:
        recorded = await self.store.record_battle_outcome(self.outcome)
        self.install_v1_analysis_schema(
            battle_id=recorded.battle_id, with_metadata=True
        )

        with patch(
            "legacy_combat_diagnostics._validate_analysis_references",
            side_effect=RuntimeError("late diagnostics migration fault"),
        ):
            with self.assertRaisesRegex(RuntimeError, "late diagnostics migration fault"):
                await self.diagnostics.initialize()

        metadata = self.store.connection.execute(
            "SELECT schema_version,schema_fingerprint "
            "FROM legacy_combat_diagnostics_meta WHERE singleton=1"
        ).fetchone()
        self.assertEqual(
            tuple(metadata), (1, legacy_diagnostics._SCHEMA_FINGERPRINT_V1)
        )
        self.assertEqual(
            self.store.connection.execute(
                "PRAGMA foreign_key_list(combat_battle_analysis)"
            ).fetchall(),
            [],
        )
        self.assertEqual(
            [
                row[0]
                for row in self.store.connection.execute(
                    "SELECT battle_id FROM combat_battle_analysis ORDER BY battle_id"
                )
            ],
            [recorded.battle_id, 999],
        )
        self.assertIsNone(
            self.store.connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='combat_battle_analysis_v2'"
            ).fetchone()
        )
        self.assertFalse(self.store.connection.in_transaction)

        await self.diagnostics.initialize()
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM combat_battle_analysis"
            ).fetchone()[0],
            1,
        )

    async def test_unknown_v1_fingerprint_is_rejected_without_mutation(self) -> None:
        recorded = await self.store.record_battle_outcome(self.outcome)
        self.install_v1_analysis_schema(
            battle_id=recorded.battle_id,
            include_orphan=False,
            with_metadata=True,
        )
        self.store.connection.execute(
            "ALTER TABLE combat_battle_analysis ADD COLUMN foreign_value TEXT"
        )
        self.store.connection.commit()

        with self.assertRaisesRegex(
            legacy_diagnostics.LegacyDiagnosticsSchemaError,
            "Unrecognized legacy table schema",
        ):
            await self.diagnostics.initialize()

        self.assertEqual(
            self.store.connection.execute(
                "SELECT schema_version FROM legacy_combat_diagnostics_meta"
            ).fetchone()[0],
            1,
        )
        self.assertIn(
            "foreign_value",
            {
                row[1]
                for row in self.store.connection.execute(
                    "PRAGMA table_info(combat_battle_analysis)"
                )
            },
        )
        self.assertFalse(self.store.connection.in_transaction)

    async def test_retention_cascades_owned_analysis_projection(self) -> None:
        old_outcome = replace(
            self.outcome, happened_at=datetime(2020, 1, 1, tzinfo=UTC)
        )
        await self.diagnostics.record_payloads(
            old_outcome, combat_decisions=(legacy_trace(),)
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM combat_battle_analysis"
            ).fetchone()[0],
            1,
        )

        cleanup = await self.store.cleanup_old_data(retention_days=1)

        self.assertEqual(cleanup["battles"], 1)
        for table in ("battles", "combat_decisions", "combat_battle_analysis"):
            with self.subTest(table=table):
                self.assertEqual(
                    self.store.connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0],
                    0,
                )


class BarrierRepairRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "barrier.sqlite3"
        self.store = Storage(self.path)

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self.directory.cleanup()

    def corrupt_barrier(self, namespace: str = "notifications") -> None:
        self.store.connection.execute(
            "INSERT INTO battle_outbox_barriers(namespace,blocked_until) VALUES (?,?)",
            (namespace, "not-an-aware-timestamp"),
        )
        self.store.connection.commit()

    async def test_read_repairs_corrupt_barrier_with_durable_bounded_deadline(
        self,
    ) -> None:
        self.corrupt_barrier()
        before = datetime.now(UTC)

        repaired = await self.store.get_battle_event_barrier(
            namespace=" notifications "
        )

        assert repaired is not None
        self.assertGreaterEqual(repaired, before + timedelta(seconds=59))
        self.assertLessEqual(repaired, datetime.now(UTC) + timedelta(seconds=61))
        row = self.store.connection.execute(
            "SELECT blocked_until FROM battle_outbox_barriers WHERE namespace='notifications'"
        ).fetchone()
        self.assertEqual(row[0], repaired.isoformat())
        audit = self.store.connection.execute(
            "SELECT level,event_type,payload_json FROM events "
            "WHERE event_type='BATTLE_OUTBOX_BARRIER_REPAIRED'"
        ).fetchone()
        self.assertEqual((audit[0], audit[1]), ("WARNING", "BATTLE_OUTBOX_BARRIER_REPAIRED"))
        self.assertEqual(json.loads(audit[2])["namespace"], "notifications")

        await self.store.close()
        self.store = Storage(self.path)
        self.assertEqual(
            await self.store.get_battle_event_barrier(namespace="notifications"),
            repaired,
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM events "
                "WHERE event_type='BATTLE_OUTBOX_BARRIER_REPAIRED'"
            ).fetchone()[0],
            1,
        )

    async def test_extend_replaces_corrupt_value_with_requested_deadline(self) -> None:
        self.corrupt_barrier()
        requested = datetime.now(UTC) + timedelta(hours=2)

        repaired = await self.store.extend_battle_event_barrier(
            namespace="notifications", blocked_until=requested
        )

        self.assertEqual(repaired, requested)
        self.assertEqual(
            await self.store.get_battle_event_barrier(namespace="notifications"),
            requested,
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM events "
                "WHERE event_type='BATTLE_OUTBOX_BARRIER_REPAIRED'"
            ).fetchone()[0],
            1,
        )

    async def test_repair_and_warning_roll_back_together(self) -> None:
        self.corrupt_barrier()
        self.store.connection.executescript(
            """
            CREATE TRIGGER reject_barrier_repair_audit
            BEFORE INSERT ON events
            WHEN NEW.event_type='BATTLE_OUTBOX_BARRIER_REPAIRED'
            BEGIN SELECT RAISE(FAIL, 'audit rejected'); END;
            """
        )

        with self.assertRaises(sqlite3.IntegrityError):
            await self.store.get_battle_event_barrier(namespace="notifications")

        self.assertEqual(
            self.store.connection.execute(
                "SELECT blocked_until FROM battle_outbox_barriers"
            ).fetchone()[0],
            "not-an-aware-timestamp",
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM events "
                "WHERE event_type='BATTLE_OUTBOX_BARRIER_REPAIRED'"
            ).fetchone()[0],
            0,
        )
        self.assertFalse(self.store.connection.in_transaction)


class JsonBoundaryRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = Storage(Path(":memory:"))

    async def asyncTearDown(self) -> None:
        await self.store.close()

    async def test_add_event_canonicalizes_empty_and_nested_valid_json(self) -> None:
        await self.store.add_event("EMPTY", "empty", payload={})
        await self.store.add_event(
            "BOUNDARIES",
            "valid",
            payload={
                "z": [INT64_MIN, INT64_MAX, True, False, None],
                "a": {"finite": 1.5},
            },
        )
        rows = self.store.connection.execute(
            "SELECT payload_json,json_valid(payload_json) FROM events ORDER BY id"
        ).fetchall()
        self.assertEqual(rows[0][0], "{}")
        self.assertEqual(
            rows[1][0],
            '{"a":{"finite":1.5},"z":['
            f'{INT64_MIN},{INT64_MAX},true,false,null]}}',
        )
        self.assertEqual([row[1] for row in rows], [1, 1])

    async def test_add_event_rejects_nonfinite_unbounded_and_untyped_json(self) -> None:
        invalid_payloads: tuple[object, ...] = (
            {"nested": {"value": float("nan")}},
            {"nested": [float("inf")]},
            {"nested": [-float("inf")]},
            {"nested": [INT64_MAX + 1]},
            {"nested": [INT64_MIN - 1]},
            {"nested": {1: "non-string-key"}},
            {"nested": object()},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                await self.store.add_event(
                    "INVALID",
                    "invalid",
                    payload=payload,  # type: ignore[arg-type]
                )
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            0,
        )
        self.assertFalse(self.store.connection.in_transaction)


if __name__ == "__main__":
    unittest.main()
