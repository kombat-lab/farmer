from __future__ import annotations

import asyncio
import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from combat_knowledge_namespace import LEGACY_COMBAT_KNOWLEDGE_NAMESPACE
from storage import SCHEMA_VERSION, Storage
from storage_migrations import migrate_combat_knowledge_namespace

LEGACY_ROWS = (
    (400, "2026-09-13T10:00:00+00:00", ' {"version": 1, "incoming": {"моль": [12]}} '),
    (780, "2026-09-14T12:00:00+00:00", '{"incoming":{"фонарщик":[42,43]}}'),
)


def create_legacy_database(path: Path, *, version: int = 1) -> None:
    """Create an on-disk database with the exact production v1 profile schema."""
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("""
            CREATE TABLE combat_knowledge (
                profile_max_hp INTEGER PRIMARY KEY,
                updated_at TEXT NOT NULL,
                knowledge_json TEXT NOT NULL
            )
        """)
        connection.executemany("INSERT INTO combat_knowledge VALUES (?,?,?)", LEGACY_ROWS)
        connection.execute("CREATE TABLE unrelated_payload (value TEXT NOT NULL)")
        connection.execute("INSERT INTO unrelated_payload VALUES ('preserve me')")
        connection.execute(f"PRAGMA user_version={version}")


class CombatKnowledgeStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_database_has_namespaced_composite_key(self) -> None:
        storage = Storage(Path(":memory:"))
        try:
            self.assertEqual(
                storage.connection.execute("PRAGMA user_version").fetchone()[0],
                SCHEMA_VERSION,
            )
            columns = {
                row["name"]: row
                for row in storage.connection.execute("PRAGMA table_info(combat_knowledge)")
            }
            self.assertEqual(columns["namespace"]["notnull"], 1)
            self.assertEqual(columns["profile_max_hp"]["notnull"], 1)
            self.assertEqual(columns["namespace"]["pk"], 1)
            self.assertEqual(columns["profile_max_hp"]["pk"], 2)
            self.assertEqual(await storage.load_combat_knowledge(namespace="new-rules"), {})
        finally:
            await storage.close()

    async def test_v1_and_unversioned_rows_migrate_without_changing_stored_data(self) -> None:
        for version in (0, 1):
            with self.subTest(version=version), TemporaryDirectory() as directory:
                path = Path(directory) / "legacy.sqlite3"
                create_legacy_database(path, version=version)
                storage = Storage(path)
                try:
                    rows = storage.connection.execute(
                        "SELECT namespace,profile_max_hp,updated_at,knowledge_json "
                        "FROM combat_knowledge ORDER BY profile_max_hp"
                    ).fetchall()
                    self.assertEqual(
                        [tuple(row) for row in rows],
                        [(LEGACY_COMBAT_KNOWLEDGE_NAMESPACE, *row) for row in LEGACY_ROWS],
                    )
                    profiles = await storage.load_combat_knowledge(
                        namespace=LEGACY_COMBAT_KNOWLEDGE_NAMESPACE
                    )
                    self.assertEqual(
                        profiles, {hp: json.loads(raw) for hp, _, raw in LEGACY_ROWS}
                    )
                    self.assertEqual(
                        await storage.load_combat_knowledge(namespace="future-rules"), {}
                    )
                    self.assertEqual(
                        storage.connection.execute("SELECT value FROM unrelated_payload")
                        .fetchone()[0],
                        "preserve me",
                    )
                    self.assertEqual(
                        storage.connection.execute("PRAGMA user_version").fetchone()[0],
                        SCHEMA_VERSION,
                    )
                finally:
                    await storage.close()

    async def test_same_hp_profiles_coexist_and_updates_survive_restart(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.sqlite3"
            storage = Storage(path)
            try:
                await asyncio.gather(
                    storage.save_combat_knowledge(
                        780, {"sample": 1}, namespace=LEGACY_COMBAT_KNOWLEDGE_NAMESPACE
                    ),
                    storage.save_combat_knowledge(
                        780, {"sample": 2}, namespace="future:rules-2:model-1"
                    ),
                )
                await storage.save_combat_knowledge(
                    780, {"sample": 3}, namespace="future:rules-2:model-1"
                )
            finally:
                await storage.close()
            reopened = Storage(path)
            try:
                self.assertEqual(
                    await reopened.load_combat_knowledge(
                        namespace=LEGACY_COMBAT_KNOWLEDGE_NAMESPACE
                    ),
                    {780: {"sample": 1}},
                )
                self.assertEqual(
                    await reopened.load_combat_knowledge(namespace="future:rules-2:model-1"),
                    {780: {"sample": 3}},
                )
                self.assertEqual(
                    reopened.connection.execute("SELECT COUNT(*) FROM combat_knowledge")
                    .fetchone()[0],
                    2,
                )
            finally:
                await reopened.close()

    async def test_namespace_is_mandatory_and_blank_values_are_rejected(self) -> None:
        storage = Storage(Path(":memory:"))
        try:
            for namespace in ("", " ", "\t\n"):
                with self.subTest(namespace=namespace):
                    with self.assertRaises(ValueError):
                        await storage.load_combat_knowledge(namespace=namespace)
                    with self.assertRaises(ValueError):
                        await storage.save_combat_knowledge(780, {}, namespace=namespace)
            with self.assertRaises(TypeError):
                await storage.load_combat_knowledge()
            with self.assertRaises(TypeError):
                await storage.save_combat_knowledge(780, {})
            self.assertEqual(
                storage.connection.execute("SELECT COUNT(*) FROM combat_knowledge").fetchone()[0],
                0,
            )
        finally:
            await storage.close()

    async def test_failed_knowledge_update_cannot_leak_into_a_later_commit(self) -> None:
        storage = Storage(Path(":memory:"))
        try:
            await storage.save_combat_knowledge(780, {"sample": 1}, namespace="test-rules")
            storage.connection.executescript("""
                CREATE TRIGGER reject_knowledge AFTER UPDATE ON combat_knowledge
                BEGIN SELECT RAISE(FAIL, 'knowledge update failed'); END;
            """)
            with self.assertRaises(sqlite3.IntegrityError):
                await storage.save_combat_knowledge(780, {"sample": 2}, namespace="test-rules")
            self.assertFalse(storage.connection.in_transaction)
            await storage.add_event("TEST", "later unrelated commit")
            self.assertEqual(
                await storage.load_combat_knowledge(namespace="test-rules"),
                {780: {"sample": 1}},
            )
        finally:
            await storage.close()

    async def test_failed_migration_restores_v1_table_rows_and_version(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "rollback.sqlite3"
            create_legacy_database(path)
            migrate = migrate_combat_knowledge_namespace

            def migrate_then_fail(connection: sqlite3.Connection) -> None:
                migrate(connection)
                raise sqlite3.OperationalError("injected migration failure")

            with patch("storage.migrate_combat_knowledge_namespace", migrate_then_fail):
                with self.assertRaisesRegex(sqlite3.OperationalError, "injected migration failure"):
                    Storage(path)
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
                self.assertEqual(
                    connection.execute("SELECT * FROM combat_knowledge ORDER BY profile_max_hp")
                    .fetchall(),
                    list(LEGACY_ROWS),
                )
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(combat_knowledge)")
                }
                self.assertNotIn("namespace", columns)
                self.assertIsNone(connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='combat_knowledge_v1'"
                ).fetchone())
            recovered = Storage(path)
            try:
                self.assertEqual(
                    len(await recovered.load_combat_knowledge(
                        namespace=LEGACY_COMBAT_KNOWLEDGE_NAMESPACE
                    )),
                    len(LEGACY_ROWS),
                )
            finally:
                await recovered.close()


if __name__ == "__main__":
    unittest.main()
