from __future__ import annotations

import sqlite3

from bounded_values import require_int64
from combat_knowledge_namespace import LEGACY_COMBAT_KNOWLEDGE_NAMESPACE
from message_snapshot import legacy_v4_message_source_event_id


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def migrate_battle_source_identity(connection: sqlite3.Connection) -> None:
    """Rebuild the v4 battle ledger around opaque source revision identities."""
    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(battles)")
    }
    if "source_event_id" in columns:
        if "source_message_id" not in columns or "telegram_message_id" in columns:
            raise RuntimeError("Unsupported battles source identity schema")
        return
    if "telegram_message_id" not in columns:
        raise RuntimeError("Cannot migrate battles without the v4 message identity")
    if _table_exists(connection, "battles_v5"):
        raise RuntimeError("Unexpected battles_v5 table before migration")

    original_ids = tuple(
        require_int64(row["id"], "legacy battle id", minimum=1)
        for row in connection.execute("SELECT id FROM battles ORDER BY id")
    )
    sequence_row = connection.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='battles'"
    ).fetchone()
    original_sequence = (
        require_int64(sequence_row["seq"], "legacy battle sequence", minimum=0)
        if sequence_row is not None
        else 0
    )

    connection.execute("""
        CREATE TABLE battles_v5 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_event_id TEXT NOT NULL UNIQUE
                CHECK(typeof(source_event_id)='text'
                      AND length(CAST(source_event_id AS BLOB)) BETWEEN 1 AND 255
                      AND source_event_id=trim(source_event_id)),
            source_message_id INTEGER
                CHECK(source_message_id IS NULL
                      OR (typeof(source_message_id)='integer' AND source_message_id > 0)),
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
    for row in connection.execute("SELECT * FROM battles ORDER BY id").fetchall():
        legacy_id = legacy_v4_message_source_event_id(row["telegram_message_id"])
        connection.execute(
            """
            INSERT INTO battles_v5(
                id,source_event_id,source_message_id,session_id,happened_at,
                target_name,result,xp,dust,position_x,position_y
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row["id"],
                legacy_id.value,
                row["telegram_message_id"],
                row["session_id"],
                row["happened_at"],
                row["target_name"],
                row["result"],
                row["xp"],
                row["dust"],
                row["position_x"],
                row["position_y"],
            ),
        )
    copied_ids = tuple(
        require_int64(row["id"], "migrated battle id", minimum=1)
        for row in connection.execute("SELECT id FROM battles_v5 ORDER BY id")
    )
    if copied_ids != original_ids:
        raise RuntimeError("Battle source identity migration did not preserve row IDs")

    connection.execute("DROP TABLE battles")
    connection.execute("ALTER TABLE battles_v5 RENAME TO battles")
    connection.execute(
        "DELETE FROM sqlite_sequence WHERE name IN ('battles', 'battles_v5')"
    )
    preserved_sequence = max(original_sequence, original_ids[-1] if original_ids else 0)
    if preserved_sequence:
        connection.execute(
            "INSERT INTO sqlite_sequence(name,seq) VALUES ('battles',?)",
            (preserved_sequence,),
        )


def validate_battle_references(connection: sqlite3.Connection) -> None:
    """Reject a migration that loses any declared battle reference."""
    violation = connection.execute("PRAGMA foreign_key_check").fetchone()
    if violation is not None:
        raise RuntimeError(f"Battle migration left a foreign-key violation: {tuple(violation)!r}")


def migrate_combat_knowledge_namespace(connection: sqlite3.Connection) -> None:
    """Move v1/unversioned profiles only into their explicit historical namespace."""
    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(combat_knowledge)")
    }
    legacy_table = bool(columns) and "namespace" not in columns
    if legacy_table:
        connection.execute(
            "ALTER TABLE combat_knowledge RENAME TO combat_knowledge_v1"
        )
    connection.execute("""
        CREATE TABLE IF NOT EXISTS combat_knowledge (
            namespace TEXT NOT NULL,
            profile_max_hp INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            knowledge_json TEXT NOT NULL,
            PRIMARY KEY(namespace, profile_max_hp)
        )
    """)
    if legacy_table:
        connection.execute(
            """
            INSERT INTO combat_knowledge(
                namespace,profile_max_hp,updated_at,knowledge_json
            )
            SELECT ?,profile_max_hp,updated_at,knowledge_json
            FROM combat_knowledge_v1
            """,
            (LEGACY_COMBAT_KNOWLEDGE_NAMESPACE,),
        )
        connection.execute("DROP TABLE combat_knowledge_v1")
