from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import legacy_combat_diagnostics as legacy_diagnostics
from battle_records import BattleOutcome, SourceEventId
from legacy_combat_diagnostics import LegacyCombatDiagnostics
from message_snapshot import legacy_v4_message_source_event_id
from storage import SCHEMA_VERSION, Storage


async def create_v4_database(
    path: Path,
    *,
    orphan_declared_reference: bool = False,
    orphan_analysis_reference: bool = False,
) -> None:
    store = Storage(path)
    await store.close()

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            legacy_diagnostics._execute_schema(
                connection, legacy_diagnostics._DIAGNOSTIC_SCHEMA_V1
            )
            connection.execute("""
                CREATE TABLE legacy_combat_diagnostics_meta (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    schema_version INTEGER NOT NULL,
                    schema_fingerprint TEXT NOT NULL
                )
            """)
            connection.execute(
                "INSERT INTO legacy_combat_diagnostics_meta VALUES (1,1,?)",
                (legacy_diagnostics._SCHEMA_FINGERPRINT_V1,),
            )
            connection.execute("""
                CREATE TABLE battles_v4 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_message_id INTEGER NOT NULL UNIQUE,
                    session_id INTEGER,
                    happened_at TEXT NOT NULL,
                    target_name TEXT NOT NULL,
                    result TEXT NOT NULL,
                    xp INTEGER NOT NULL DEFAULT 0,
                    dust INTEGER NOT NULL DEFAULT 0,
                    position_x INTEGER,
                    position_y INTEGER,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                )
            """)
            connection.execute(
                """
                INSERT INTO sessions(
                    id,started_at,status,wins,defeats,xp,dust,runtime_seconds
                ) VALUES (3,?,'STOPPED',1,0,5,3,10)
                """,
                ("2026-09-14T10:00:00+00:00",),
            )
            connection.execute(
                """
                INSERT INTO battles_v4(
                    id,telegram_message_id,session_id,happened_at,target_name,
                    result,xp,dust,position_x,position_y
                ) VALUES (7,500,3,?,'Moth','VICTORY',5,3,4,6)
                """,
                ("2026-09-14T10:05:00+00:00",),
            )
            connection.execute("DROP TABLE battles")
            connection.execute("ALTER TABLE battles_v4 RENAME TO battles")
            connection.execute(
                "CREATE INDEX idx_battles_happened_at ON battles(happened_at)"
            )
            connection.execute(
                "CREATE INDEX idx_battles_session_id ON battles(session_id)"
            )
            connection.execute(
                "DELETE FROM sqlite_sequence WHERE name IN ('battles','battles_v4')"
            )
            connection.execute(
                "INSERT INTO sqlite_sequence(name,seq) VALUES ('battles',41)"
            )
            connection.execute(
                "INSERT INTO drops(id,battle_id,item_name,quantity,is_card) "
                "VALUES (11,7,'Moth card',2,1)"
            )
            connection.execute(
                "INSERT INTO battle_currencies(battle_id,currency_code,amount) "
                "VALUES (7,'mist_crystals',4)"
            )
            connection.execute(
                """
                INSERT INTO battle_outbox(
                    id,battle_id,namespace,idempotency_key,event_type,schema_version,
                    payload_json,created_at,acknowledged_at,attempts,next_attempt_at,last_error
                ) VALUES (13,7,'notifications','pending','card',1,'{"card":"Moth"}',
                          '2026-09-14T10:06:00+00:00',NULL,3,
                          '2026-09-14T10:10:00+00:00','network')
                """
            )
            connection.execute(
                """
                INSERT INTO battle_outbox(
                    id,battle_id,namespace,idempotency_key,event_type,schema_version,
                    payload_json,created_at,acknowledged_at,attempts,next_attempt_at,last_error
                ) VALUES (14,7,'notifications','acked','card',1,'{"card":"Moth"}',
                          '2026-09-14T10:06:00+00:00',
                          '2026-09-14T10:07:00+00:00',1,NULL,NULL)
                """
            )
            connection.execute(
                """
                INSERT INTO combat_decisions(
                    id,battle_id,sequence_number,created_at,telegram_message_id,
                    target_name,round_number,chosen_skill,chosen_target,reason,urgent,trace_json
                ) VALUES (17,7,1,'2026-09-14T10:04:00+00:00',499,
                          'Moth',2,'attack','enemy','test',0,'{}')
                """
            )
            connection.execute(
                """
                INSERT INTO combat_battle_analysis(
                    battle_id,target_name,result,happened_at,policy_key,created_at
                ) VALUES (7,'Moth','VICTORY','2026-09-14T10:05:00+00:00',
                          'legacy','2026-09-14T10:06:00+00:00')
                """
            )
            if orphan_declared_reference:
                connection.execute(
                    "INSERT INTO drops(id,battle_id,item_name,quantity,is_card) "
                    "VALUES (12,999,'orphan',1,0)"
                )
            if orphan_analysis_reference:
                connection.execute(
                    """
                    INSERT INTO combat_battle_analysis(
                        battle_id,target_name,result,happened_at,policy_key,created_at
                    ) VALUES (999,'orphan','DEFEAT','2026-09-14T10:05:00+00:00',
                              'legacy','2026-09-14T10:06:00+00:00')
                    """
                )
            connection.execute("PRAGMA user_version=4")
    finally:
        connection.close()


class BattleSourceMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_persisted_source_identity_is_decoded_without_coercion(self) -> None:
        store = Storage(Path(":memory:"))
        try:
            recorded = await store.record_battle_outcome(
                BattleOutcome(
                    source_event_id=SourceEventId("test:strict-decoder"),
                    source_message_id=1,
                    session_id=None,
                    target_name="Moth",
                    result="VICTORY",
                )
            )
            store.connection.execute(
                "UPDATE battles SET source_event_id=? WHERE id=?",
                ("corrupt\nidentity", recorded.battle_id),
            )
            store.connection.commit()
            with self.assertRaisesRegex(ValueError, "control characters"):
                await store.get_battle_outcome(recorded.battle_id)
        finally:
            await store.close()

    async def test_v4_migration_preserves_ids_children_diagnostics_and_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "v4.sqlite3"
            await create_v4_database(path)

            store = Storage(path)
            try:
                self.assertEqual(
                    store.connection.execute("PRAGMA user_version").fetchone()[0],
                    SCHEMA_VERSION,
                )
                battle = store.connection.execute("SELECT * FROM battles").fetchone()
                self.assertEqual(battle["id"], 7)
                self.assertEqual(battle["source_message_id"], 500)
                self.assertEqual(
                    battle["source_event_id"],
                    legacy_v4_message_source_event_id(500).value,
                )
                self.assertNotIn(
                    "telegram_message_id",
                    {row["name"] for row in store.connection.execute("PRAGMA table_info(battles)")},
                )
                self.assertEqual(
                    tuple(store.connection.execute("SELECT * FROM drops").fetchone()),
                    (11, 7, "Moth card", 2, 1),
                )
                self.assertEqual(
                    tuple(store.connection.execute("SELECT * FROM battle_currencies").fetchone()),
                    (7, "mist_crystals", 4),
                )
                outbox = store.connection.execute(
                    """
                    SELECT id,battle_id,acknowledged_at,attempts,next_attempt_at,last_error
                    FROM battle_outbox ORDER BY id
                    """
                ).fetchall()
                self.assertEqual(
                    [tuple(row) for row in outbox],
                    [
                        (
                            13,
                            7,
                            None,
                            3,
                            "2026-09-14T10:10:00+00:00",
                            "network",
                        ),
                        (14, 7, "2026-09-14T10:07:00+00:00", 1, None, None),
                    ],
                )
                self.assertEqual(
                    tuple(
                        store.connection.execute(
                            "SELECT battle_id,telegram_message_id FROM combat_decisions"
                        ).fetchone()
                    ),
                    (7, 499),
                )
                self.assertEqual(
                    store.connection.execute(
                        "SELECT battle_id FROM combat_battle_analysis"
                    ).fetchone()[0],
                    7,
                )
                self.assertEqual(
                    store.connection.execute("PRAGMA foreign_key_check").fetchall(), []
                )
                indexes = {
                    row["name"]
                    for row in store.connection.execute("PRAGMA index_list(battles)")
                }
                self.assertTrue(
                    {
                        "idx_battles_happened_at",
                        "idx_battles_session_id",
                        "idx_battles_source_message_id",
                    }.issubset(indexes)
                )
                await LegacyCombatDiagnostics(store).initialize()
                session = store.connection.execute(
                    "SELECT wins,xp,dust FROM sessions WHERE id=3"
                ).fetchone()
                self.assertEqual(tuple(session), (1, 5, 3))

                inserted = await store.record_battle_outcome(
                    BattleOutcome(
                        source_event_id=SourceEventId("test:post-migration"),
                        source_message_id=500,
                        session_id=None,
                        target_name="Moth",
                        result="DEFEAT",
                    )
                )
                self.assertEqual(inserted.battle_id, 42)
            finally:
                await store.close()

    async def test_late_migration_failure_rolls_back_and_restores_fk_enforcement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fault.sqlite3"
            await create_v4_database(path)
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            storage = object.__new__(Storage)
            storage.connection = connection
            try:
                with patch(
                    "storage.validate_battle_references",
                    side_effect=RuntimeError("late migration fault"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "late migration fault"):
                        storage._create_schema()
                self.assertFalse(connection.in_transaction)
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)
                columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(battles)")
                }
                self.assertIn("telegram_message_id", columns)
                self.assertNotIn("source_event_id", columns)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM drops").fetchone()[0], 1)
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM combat_decisions").fetchone()[0], 1
                )
            finally:
                connection.close()

    async def test_declared_foreign_key_violation_rejects_and_rolls_back_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orphan.sqlite3"
            await create_v4_database(path, orphan_declared_reference=True)
            with self.assertRaisesRegex(RuntimeError, "foreign-key violation"):
                Storage(path)
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)
                self.assertIn(
                    "telegram_message_id",
                    {row[1] for row in connection.execute("PRAGMA table_info(battles)")},
                )
            finally:
                connection.close()

    async def test_legacy_analysis_owner_resolves_orphans_after_core_migration(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analysis-orphan.sqlite3"
            await create_v4_database(path, orphan_analysis_reference=True)

            store = Storage(path)
            try:
                self.assertEqual(
                    store.connection.execute("PRAGMA user_version").fetchone()[0],
                    SCHEMA_VERSION,
                )
                self.assertEqual(
                    store.connection.execute(
                        "SELECT COUNT(*) FROM combat_battle_analysis"
                    ).fetchone()[0],
                    2,
                )

                await LegacyCombatDiagnostics(store).initialize()

                self.assertEqual(
                    [
                        row[0]
                        for row in store.connection.execute(
                            "SELECT battle_id FROM combat_battle_analysis ORDER BY battle_id"
                        )
                    ],
                    [7],
                )
                self.assertEqual(
                    store.connection.execute(
                        "PRAGMA foreign_key_check(combat_battle_analysis)"
                    ).fetchall(),
                    [],
                )
            finally:
                await store.close()


class ConcurrentBattleSourceMigrationTests(unittest.TestCase):
    def test_two_initializers_serialize_the_v4_to_v5_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "concurrent.sqlite3"
            asyncio.run(create_v4_database(path))
            barrier = threading.Barrier(2)

            def initialize() -> int:
                barrier.wait(timeout=5)
                store = Storage(path)
                try:
                    return int(store.connection.execute("PRAGMA user_version").fetchone()[0])
                finally:
                    store.connection.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                versions = tuple(pool.map(lambda _index: initialize(), range(2)))
            self.assertEqual(versions, (SCHEMA_VERSION, SCHEMA_VERSION))
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM battles").fetchone()[0], 1
                )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
