from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Unpack, cast

from battle_outbox import BattleEvent, BattleOutboxEnvelope, InvalidBattleOutboxEntry
from battle_records import (
    MIST_CRYSTAL_CODE,
    BattleOutcome,
    BattleResult,
    IdempotencyConflict,
    ItemDrop,
    RecordBattleResult,
    RewardBundle,
    SourceEventId,
)
from bounded_values import INT64_MAX, require_int64
from json_types import canonical_json_object, canonical_json_value
from runtime_state import PROCESS_STATUS_NAMES, require_phase_name
from storage_migrations import (
    migrate_battle_source_identity,
    migrate_combat_knowledge_namespace,
    validate_battle_references,
)
from storage_types import (
    BattleTotals,
    DropSummary,
    DropTotals,
    EventSummary,
    FarmerState,
    FarmerStatePatch,
    JsonValue,
    SessionSummary,
    StatisticsDashboard,
    TargetTotals,
    TelegramActivityDay,
)

SCHEMA_VERSION = 5
CORRUPT_BARRIER_FALLBACK_SECONDS = 60


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        try:
            self._create_schema()
            # journal_mode is persistent; change it only after accepting the schema.
            self.connection.execute("PRAGMA journal_mode=WAL").fetchone()
        except Exception:
            self.connection.close()
            raise

    def _create_schema(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            status TEXT NOT NULL,
            stop_reason TEXT,
            wins INTEGER NOT NULL DEFAULT 0,
            defeats INTEGER NOT NULL DEFAULT 0,
            xp INTEGER NOT NULL DEFAULT 0,
            dust INTEGER NOT NULL DEFAULT 0,
            runtime_seconds INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS battles (
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
        );
        CREATE INDEX IF NOT EXISTS idx_battles_happened_at
            ON battles(happened_at);
        CREATE INDEX IF NOT EXISTS idx_battles_session_id
            ON battles(session_id);

        CREATE TABLE IF NOT EXISTS drops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            battle_id INTEGER NOT NULL,
            item_name TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            is_card INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(battle_id) REFERENCES battles(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_drops_battle_id
            ON drops(battle_id);

        CREATE TABLE IF NOT EXISTS battle_currencies (
            battle_id INTEGER NOT NULL,
            currency_code TEXT NOT NULL,
            amount INTEGER NOT NULL DEFAULT 0 CHECK(amount >= 0),
            PRIMARY KEY(battle_id, currency_code),
            FOREIGN KEY(battle_id) REFERENCES battles(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_battle_currencies_code
            ON battle_currencies(currency_code, battle_id);

        CREATE TABLE IF NOT EXISTS battle_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            battle_id INTEGER NOT NULL,
            namespace TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            event_type TEXT NOT NULL,
            schema_version INTEGER NOT NULL CHECK(schema_version > 0),
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            acknowledged_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            last_error TEXT,
            UNIQUE(battle_id, namespace, idempotency_key),
            FOREIGN KEY(battle_id) REFERENCES battles(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_battle_outbox_pending
            ON battle_outbox(namespace, acknowledged_at, id);

        CREATE TABLE IF NOT EXISTS battle_outbox_barriers (
            namespace TEXT PRIMARY KEY,
            blocked_until TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS farmer_state (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            process_status TEXT NOT NULL DEFAULT 'STOPPED',
            game_state TEXT NOT NULL DEFAULT 'STOPPED',
            position_x INTEGER,
            position_y INTEGER,
            current_hp INTEGER,
            max_hp INTEGER,
            active_target TEXT,
            moves INTEGER NOT NULL DEFAULT 0,
            last_action TEXT,
            last_progress_at TEXT,
            last_error TEXT,
            session_id INTEGER,
            current_cycle INTEGER NOT NULL DEFAULT 1,
            cycles_count INTEGER NOT NULL DEFAULT 1,
            moves_in_cycle INTEGER NOT NULL DEFAULT 0,
            moves_per_cycle INTEGER NOT NULL DEFAULT 80,
            rest_until TEXT,
            pause_requested INTEGER NOT NULL DEFAULT 0
        );
        INSERT OR IGNORE INTO farmer_state(singleton) VALUES (1);

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            level TEXT NOT NULL,
            event_type TEXT NOT NULL,
            message TEXT NOT NULL,
            payload_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_events_created_at
            ON events(created_at);

        CREATE INDEX IF NOT EXISTS idx_sessions_status_ended_at
            ON sessions(status, ended_at);

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS map_obstacles (
            location_name TEXT NOT NULL,
            position_x INTEGER NOT NULL,
            position_y INTEGER NOT NULL,
            discovered_at TEXT NOT NULL,
            PRIMARY KEY(location_name, position_x, position_y)
        );

        CREATE TABLE IF NOT EXISTS telegram_activity_hourly (
            bucket_start TEXT PRIMARY KEY,
            outgoing_total INTEGER NOT NULL DEFAULT 0,
            inline_callbacks INTEGER NOT NULL DEFAULT 0,
            map_requests INTEGER NOT NULL DEFAULT 0,
            peak_actions_1m INTEGER NOT NULL DEFAULT 0,
            peak_actions_10m INTEGER NOT NULL DEFAULT 0,
            incoming_new_messages INTEGER NOT NULL DEFAULT 0,
            incoming_message_edits INTEGER NOT NULL DEFAULT 0,
            incoming_semantic_states INTEGER NOT NULL DEFAULT 0,
            callback_successes INTEGER NOT NULL DEFAULT 0,
            callback_timeouts INTEGER NOT NULL DEFAULT 0,
            flood_waits INTEGER NOT NULL DEFAULT 0,
            flood_wait_seconds INTEGER NOT NULL DEFAULT 0,
            recovery_attempts INTEGER NOT NULL DEFAULT 0,
            silent_stalls INTEGER NOT NULL DEFAULT 0,
            manual_restriction_marks INTEGER NOT NULL DEFAULT 0,
            rpc_errors INTEGER NOT NULL DEFAULT 0
        );

        """
        # Rebuilding a referenced parent requires FK enforcement to be disabled
        # before BEGIN. The IMMEDIATE transaction protects version inspection,
        # DDL, copied rows, integrity checks, and user_version publication.
        self.connection.execute("PRAGMA foreign_keys=OFF")
        try:
            with self.connection:
                self.connection.execute("BEGIN IMMEDIATE")
                current_version = require_int64(
                    self.connection.execute("PRAGMA user_version").fetchone()[0],
                    "SQLite schema version",
                    minimum=0,
                )
                if current_version > SCHEMA_VERSION:
                    raise RuntimeError(
                        "Версия схемы SQLite новее поддерживаемой: "
                        f"{current_version} > {SCHEMA_VERSION}"
                    )
                for statement in schema.split(";"):
                    if statement.strip():
                        self.connection.execute(statement)
                if current_version < 4:
                    # Existing deferrals predate the explicit namespace barrier. On
                    # upgrade, conservatively preserve their longest valid deadline.
                    rows = self.connection.execute(
                        "SELECT namespace,next_attempt_at FROM battle_outbox "
                        "WHERE acknowledged_at IS NULL AND next_attempt_at IS NOT NULL"
                    ).fetchall()
                    for row in rows:
                        try:
                            until = self._aware_utc(row["next_attempt_at"])
                        except (TypeError, ValueError):
                            continue  # Corrupt rows remain visible for quarantine.
                        self._extend_battle_event_barrier_unlocked(row["namespace"], until)
                migrate_combat_knowledge_namespace(self.connection)
                if current_version < 5:
                    migrate_battle_source_identity(self.connection)
                for statement in (
                    "CREATE INDEX IF NOT EXISTS idx_battles_happened_at "
                    "ON battles(happened_at)",
                    "CREATE INDEX IF NOT EXISTS idx_battles_session_id "
                    "ON battles(session_id)",
                    "CREATE INDEX IF NOT EXISTS idx_battles_source_message_id "
                    "ON battles(source_message_id)",
                ):
                    self.connection.execute(statement)
                if current_version < 5:
                    validate_battle_references(self.connection)
                self.connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        finally:
            self.connection.execute("PRAGMA foreign_keys=ON")
            enabled = self.connection.execute("PRAGMA foreign_keys").fetchone()[0]
            if enabled != 1:
                raise RuntimeError("SQLite foreign-key enforcement could not be restored")

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[None]:
        """Serializes a write unit and rolls it back completely on failure."""
        async with self.lock:
            with self.connection:
                self.connection.execute("BEGIN IMMEDIATE")
                yield

    @asynccontextmanager
    async def diagnostics_reader(self) -> AsyncIterator[sqlite3.Connection]:
        """Give an extension repository synchronized read access to its own tables."""
        async with self.lock:
            yield self.connection

    @asynccontextmanager
    async def diagnostics_transaction(self) -> AsyncIterator[sqlite3.Connection]:
        """Give an extension repository an independent transaction, including DDL."""
        async with self._transaction():
            yield self.connection

    def _close_abandoned_sessions(self) -> int:
        """Closes sessions left RUNNING by a killed container or an old defect."""
        rows = self.connection.execute(
            """
            SELECT s.id,s.started_at,s.runtime_seconds,
                   MAX(b.happened_at) last_battle_at,
                   MAX(f.last_progress_at) last_progress_at
            FROM sessions s
            LEFT JOIN battles b ON b.session_id=s.id
            LEFT JOIN farmer_state f ON f.session_id=s.id
            WHERE s.status='RUNNING'
            GROUP BY s.id
            """
        ).fetchall()

        for row in rows:
            # The last confirmed battle is a more honest end point than the
            # current restart time. Empty abandoned sessions therefore get a
            # zero runtime instead of several artificial days.
            effective_end = str(
                row["last_progress_at"] or row["last_battle_at"] or row["started_at"]
            )
            try:
                started = datetime.fromisoformat(str(row["started_at"]))
                finished = datetime.fromisoformat(effective_end)
                runtime = max(0, int((finished - started).total_seconds()))
            except ValueError:
                runtime = max(0, int(row["runtime_seconds"]))

            self.connection.execute(
                """
                UPDATE sessions SET ended_at=?, status='INTERRUPTED',
                    stop_reason=COALESCE(stop_reason, ?), runtime_seconds=?
                WHERE id=? AND status='RUNNING'
                """,
                (
                    effective_end,
                    "предыдущий процесс завершился без корректной остановки",
                    max(runtime, int(row["runtime_seconds"])),
                    int(row["id"]),
                ),
            )
        return len(rows)

    async def cleanup_old_data(
        self, retention_days: int = 7, *, event_types_to_delete: tuple[str, ...] = ()
    ) -> dict[str, int]:
        """Apply generic retention and an application-supplied event deletion policy."""
        cutoff = (datetime.now(UTC) - timedelta(days=max(1, retention_days))).isoformat()
        async with self._transaction():
            event_filter = "created_at < ?"
            if event_types_to_delete:
                event_placeholders = ",".join("?" for _ in event_types_to_delete)
                event_filter += f" OR event_type IN ({event_placeholders})"
            deleted_events = self.connection.execute(
                f"DELETE FROM events WHERE {event_filter}",
                (cutoff, *event_types_to_delete),
            ).rowcount
            old_battle_ids = [
                int(row["id"])
                for row in self.connection.execute(
                    """SELECT id FROM battles WHERE happened_at < ? AND NOT EXISTS (
                        SELECT 1 FROM battle_outbox o
                        WHERE o.battle_id=battles.id AND o.acknowledged_at IS NULL
                    )""", (cutoff,)
                ).fetchall()
            ]
            deleted_drops = 0
            deleted_battles = 0
            if old_battle_ids:
                placeholders = ",".join("?" for _ in old_battle_ids)
                deleted_drops = self.connection.execute(
                    f"DELETE FROM drops WHERE battle_id IN ({placeholders})",
                    old_battle_ids,
                ).rowcount
                deleted_battles = self.connection.execute(
                    f"DELETE FROM battles WHERE id IN ({placeholders})",
                    old_battle_ids,
                ).rowcount

            deleted_sessions = self.connection.execute(
                """DELETE FROM sessions
                   WHERE status != 'RUNNING'
                     AND COALESCE(ended_at, started_at) < ?
                     AND id NOT IN (
                         SELECT DISTINCT session_id FROM battles
                         WHERE session_id IS NOT NULL
                     )
                     AND id NOT IN (
                         SELECT session_id FROM farmer_state
                         WHERE session_id IS NOT NULL
                     )""",
                (cutoff,),
            ).rowcount
            telemetry_cutoff = (
                datetime.now(UTC) - timedelta(days=90)
            ).replace(minute=0, second=0, microsecond=0).isoformat()
            self.connection.execute(
                "DELETE FROM telegram_activity_hourly WHERE bucket_start < ?",
                (telemetry_cutoff,),
            )
            return {
                "events": max(0, deleted_events),
                "drops": max(0, deleted_drops),
                "battles": max(0, deleted_battles),
                "sessions": max(0, deleted_sessions),
            }

    async def increment_telegram_activity(
        self,
        bucket_start: str,
        metrics: dict[str, int],
    ) -> None:
        """Atomically merges one buffered hourly Telegram telemetry batch."""
        columns = {
            "outgoing_total",
            "inline_callbacks",
            "map_requests",
            "peak_actions_1m",
            "peak_actions_10m",
            "incoming_new_messages",
            "incoming_message_edits",
            "incoming_semantic_states",
            "callback_successes",
            "callback_timeouts",
            "flood_waits",
            "flood_wait_seconds",
            "recovery_attempts",
            "silent_stalls",
            "manual_restriction_marks",
            "rpc_errors",
        }
        values = {
            key: bounded
            for key, value in metrics.items()
            if key in columns and (bounded := require_int64(value, key)) > 0
        }
        if not values:
            return
        names = list(values)
        insert_columns = ",".join(["bucket_start", *names])
        placeholders = ",".join("?" for _ in range(len(names) + 1))
        peak_columns = {"peak_actions_1m", "peak_actions_10m"}
        updates = ",".join(
            (
                f"{name}=MAX({name},excluded.{name})"
                if name in peak_columns
                else f"{name}={name}+excluded.{name}"
            )
            for name in names
        )
        async with self._transaction():
            previous = self.connection.execute(
                "SELECT * FROM telegram_activity_hourly WHERE bucket_start=?", (bucket_start,),
            ).fetchone()
            if previous is not None:
                for name, amount in values.items():
                    old_amount = require_int64(previous[name], name, minimum=0)
                    if name not in peak_columns:
                        require_int64(old_amount + amount, name, minimum=0)
            self.connection.execute(
                f"""INSERT INTO telegram_activity_hourly({insert_columns})
                    VALUES ({placeholders})
                    ON CONFLICT(bucket_start) DO UPDATE SET {updates}""",
                (bucket_start, *(values[name] for name in names)),
            )

    async def get_telegram_activity_daily(
        self,
        days: int = 14,
    ) -> list[TelegramActivityDay]:
        """Returns compact Moscow-day totals; empty days are intentionally omitted."""
        moscow = timezone(timedelta(hours=3))
        first_day = datetime.now(UTC).astimezone(moscow).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        first_day -= timedelta(days=max(1, days) - 1)
        cutoff = first_day.astimezone(UTC).isoformat()
        async with self.lock:
            rows = self.connection.execute(
                """
                SELECT date(bucket_start, '+3 hours') day,
                       SUM(outgoing_total) outgoing_total,
                       SUM(inline_callbacks) inline_callbacks,
                       SUM(map_requests) map_requests,
                       MAX(peak_actions_1m) peak_actions_1m,
                       MAX(peak_actions_10m) peak_actions_10m,
                       SUM(incoming_new_messages) incoming_new_messages,
                       SUM(incoming_message_edits) incoming_message_edits,
                       SUM(incoming_semantic_states) incoming_semantic_states,
                       SUM(callback_successes) callback_successes,
                       SUM(callback_timeouts) callback_timeouts,
                       SUM(flood_waits) flood_waits,
                       SUM(flood_wait_seconds) flood_wait_seconds,
                       SUM(recovery_attempts) recovery_attempts,
                       SUM(silent_stalls) silent_stalls,
                       SUM(manual_restriction_marks) manual_restriction_marks,
                       SUM(rpc_errors) rpc_errors
                FROM telegram_activity_hourly
                WHERE bucket_start >= ?
                GROUP BY date(bucket_start, '+3 hours')
                ORDER BY day DESC
                """,
                (cutoff,),
            ).fetchall()
            return [cast(TelegramActivityDay, dict(row)) for row in rows]

    async def compact_if_needed(
        self,
        *,
        min_free_pages: int = 256,
        min_free_ratio: float = 0.20,
    ) -> bool:
        """Rebuilds SQLite only when cleanup left a meaningful amount of free space."""
        async with self.lock:
            page_count = int(self.connection.execute("PRAGMA page_count").fetchone()[0])
            free_pages = int(
                self.connection.execute("PRAGMA freelist_count").fetchone()[0]
            )
            if page_count <= 0 or free_pages < max(1, min_free_pages):
                return False
            if free_pages / page_count < max(0.0, min(1.0, min_free_ratio)):
                return False

            self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            self.connection.execute("VACUUM")
            self.connection.execute("PRAGMA optimize")
            return True

    async def start_session(
        self,
        *,
        cycles_count: int,
        moves_per_cycle: int,
    ) -> int:
        require_int64(cycles_count, "cycles_count", minimum=1)
        require_int64(moves_per_cycle, "moves_per_cycle", minimum=1)
        async with self._transaction():
            now = utc_now()
            self._close_abandoned_sessions()
            cursor = self.connection.execute(
                "INSERT INTO sessions(started_at,status) VALUES (?, 'RUNNING')",
                (now,),
            )
            if cursor.rowcount != 1 or cursor.lastrowid is None:
                raise RuntimeError("SQLite не вернул ID новой сессии")
            sid = int(cursor.lastrowid)
            state = self.connection.execute(
                """
                UPDATE farmer_state SET
                    process_status='RUNNING', game_state='STARTING',
                    moves=0, current_cycle=1, cycles_count=?,
                    moves_in_cycle=0, moves_per_cycle=?,
                    rest_until=NULL, pause_requested=0,
                    last_error=NULL, session_id=?, last_progress_at=?
                WHERE singleton=1
            """,
                (cycles_count, moves_per_cycle, sid, now),
            )
            if state.rowcount != 1:
                raise RuntimeError("SQLite did not update the required session state")
            return sid

    async def finish_session(
        self, session_id: int | None, reason: str, runtime_seconds: int
    ) -> None:
        require_int64(runtime_seconds, "runtime_seconds", minimum=0)
        if session_id is not None:
            require_int64(session_id, "session_id", minimum=1)
        async with self._transaction():
            if session_id is not None:
                finished = self.connection.execute(
                    """
                    UPDATE sessions SET ended_at=?, status='STOPPED',
                    stop_reason=?, runtime_seconds=? WHERE id=?
                """,
                    (utc_now(), reason, runtime_seconds, session_id),
                )
                if finished.rowcount != 1:
                    raise RuntimeError("SQLite did not finish the required session")
            state = self.connection.execute(
                """
                UPDATE farmer_state SET process_status='STOPPED',
                game_state='STOPPED', active_target=NULL,
                last_action=?, last_progress_at=?, pause_requested=0,
                rest_until=NULL WHERE singleton=1
            """,
                (reason, utc_now()),
            )
            if state.rowcount != 1:
                raise RuntimeError("SQLite did not update the required session state")

    async def checkpoint(self, *, truncate: bool = False) -> tuple[int, int, int]:
        """Copies committed WAL pages into the main database file."""
        mode = "TRUNCATE" if truncate else "PASSIVE"
        async with self.lock:
            row = self.connection.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
            if row is None:
                return 0, 0, 0
            return int(row[0]), int(row[1]), int(row[2])

    async def close(self) -> None:
        """Checkpoints WAL and closes SQLite on a graceful application stop."""
        async with self.lock:
            try:
                self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                self.connection.close()

    async def update_state(self, **fields: Unpack[FarmerStatePatch]) -> None:
        unexpected = fields.keys() - FarmerStatePatch.__annotations__.keys()
        if unexpected:
            raise ValueError("Неизвестные поля состояния: " + ", ".join(sorted(unexpected)))
        if not fields:
            return
        normalized: dict[str, object] = {}
        positive = {"current_cycle", "cycles_count", "moves_per_cycle", "session_id"}
        nonnegative = {"current_hp", "max_hp", "moves", "moves_in_cycle"}
        nullable = {"position_x", "position_y", "current_hp", "max_hp", "active_target",
                    "last_action", "last_progress_at", "last_error", "session_id", "rest_until"}
        text = {"active_target", "last_action", "last_error"}
        timestamps = {"last_progress_at", "rest_until"}
        for key, value in fields.items():
            if value is None:
                if key not in nullable:
                    raise ValueError(f"{key} cannot be None")
            elif key in positive:
                require_int64(value, key, minimum=1)
            elif key in nonnegative:
                require_int64(value, key, minimum=0)
            elif key in {"position_x", "position_y"}:
                require_int64(value, key)
            elif key == "pause_requested":
                if require_int64(value, key, minimum=0) not in (0, 1):
                    raise ValueError("pause_requested must be 0 or 1")
            elif key in text:
                if not isinstance(value, str):
                    raise ValueError(f"{key} must be a string or None")
            elif key in timestamps:
                if not isinstance(value, str):
                    raise ValueError(f"{key} must be an aware ISO timestamp")
                moment = datetime.fromisoformat(value)
                if moment.tzinfo is None or moment.utcoffset() is None:
                    raise ValueError(f"{key} must be an aware ISO timestamp")
                value = moment.astimezone(UTC).isoformat()
            elif key == "game_state":
                value = require_phase_name(value, "game_state")
            elif key == "process_status":
                if not isinstance(value, str) or value not in PROCESS_STATUS_NAMES:
                    raise ValueError("Unknown process_status")
            normalized[key] = value
        sql = ", ".join(f"{key}=?" for key in fields)
        async with self._transaction():
            cursor = self.connection.execute(
                f"UPDATE farmer_state SET {sql} WHERE singleton=1",
                list(normalized.values()),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Отсутствует обязательное состояние farmer_state(singleton=1)")

    @staticmethod
    def _decode_state_row(row: sqlite3.Row) -> FarmerState:
        """Validate persisted state without guessing corrupted or future values.

        Aware legacy timestamps are normalized to UTC. Unsupported enum values,
        lossy numeric representations, and malformed rows fail closed so the
        supervisor cannot act on a state that violates the public read contract.
        """

        def integer(field: str, *, minimum: int | None = None) -> int:
            value = row[field]
            if minimum is None:
                return require_int64(value, f"Stored {field}")
            return require_int64(value, f"Stored {field}", minimum=minimum)

        def optional_integer(field: str, *, minimum: int | None = None) -> int | None:
            if row[field] is None:
                return None
            return integer(field, minimum=minimum)

        def optional_text(field: str) -> str | None:
            value = row[field]
            if value is not None and not isinstance(value, str):
                raise ValueError(f"Stored {field} must be a string or None")
            return value

        def timestamp(field: str) -> str | None:
            value = row[field]
            if value is None:
                return None
            if not isinstance(value, str):
                raise ValueError(f"Stored {field} must be an aware ISO timestamp")
            try:
                moment = datetime.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(
                    f"Stored {field} must be an aware ISO timestamp"
                ) from exc
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise ValueError(f"Stored {field} must be an aware ISO timestamp")
            return moment.astimezone(UTC).isoformat()

        process_status = row["process_status"]
        if not isinstance(process_status, str) or process_status not in PROCESS_STATUS_NAMES:
            raise ValueError("Stored process_status is unsupported")
        game_state = row["game_state"]
        game_state = require_phase_name(game_state, "Stored game_state")
        pause_requested = integer("pause_requested", minimum=0)
        if pause_requested not in (0, 1):
            raise ValueError("Stored pause_requested must be 0 or 1")
        singleton = integer("singleton", minimum=1)
        if singleton != 1:
            raise ValueError("Stored farmer_state singleton must be 1")

        return FarmerState(
            singleton=singleton,
            process_status=process_status,
            game_state=game_state,
            position_x=optional_integer("position_x"),
            position_y=optional_integer("position_y"),
            current_hp=optional_integer("current_hp", minimum=0),
            max_hp=optional_integer("max_hp", minimum=0),
            active_target=optional_text("active_target"),
            moves=integer("moves", minimum=0),
            last_action=optional_text("last_action"),
            last_progress_at=timestamp("last_progress_at"),
            last_error=optional_text("last_error"),
            session_id=optional_integer("session_id", minimum=1),
            current_cycle=integer("current_cycle", minimum=1),
            cycles_count=integer("cycles_count", minimum=1),
            moves_in_cycle=integer("moves_in_cycle", minimum=0),
            moves_per_cycle=integer("moves_per_cycle", minimum=1),
            rest_until=timestamp("rest_until"),
            pause_requested=pause_requested,
        )

    def _get_state_unlocked(self) -> FarmerState:
        """Read the complete singleton while the caller owns the connection lock."""
        row = self.connection.execute("SELECT * FROM farmer_state WHERE singleton=1").fetchone()
        if row is None:
            raise RuntimeError("Отсутствует обязательное состояние farmer_state(singleton=1)")
        return self._decode_state_row(row)

    async def get_state(self) -> FarmerState:
        async with self.lock:
            return self._get_state_unlocked()

    async def set_setting(self, key: str, value: object) -> None:
        await self.set_settings({key: value})

    @staticmethod
    def _validated_setting_keys(keys: Iterable[str]) -> tuple[str, ...]:
        result = tuple(keys)
        if any(not isinstance(key, str) or not key.strip() for key in result):
            raise ValueError("Setting keys must be nonblank strings")
        return tuple(sorted(result))

    async def set_settings(self, values: Mapping[str, object]) -> None:
        await self.set_and_delete_settings(values, frozenset())

    async def set_and_delete_settings(
        self, values: Mapping[str, object], keys: set[str] | frozenset[str],
    ) -> None:
        """Atomically publish validated values and remove an explicit obsolete key set."""
        names = self._validated_setting_keys(values)
        deleted = self._validated_setting_keys(keys)
        if set(names) & set(deleted):
            raise ValueError("A setting cannot be updated and deleted in the same operation")
        updated_at = utc_now()
        rows = [(key, canonical_json_value(values[key]), updated_at) for key in names]
        if not rows and not deleted:
            return
        async with self._transaction():
            self.connection.executemany(
                """INSERT INTO settings(key,value_json,updated_at) VALUES (?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json=excluded.value_json, updated_at=excluded.updated_at
                WHERE settings.value_json != excluded.value_json""", rows,
            )
            if rows:
                placeholders = ",".join("?" for _ in names)
                saved = dict(self.connection.execute(
                    f"SELECT key,value_json FROM settings WHERE key IN ({placeholders})", names,
                ).fetchall())
                if any(saved.get(key) != value for key, value, _ in rows):
                    raise RuntimeError("SQLite did not persist the requested settings")
            self._delete_settings_unlocked(deleted)

    def _delete_settings_unlocked(self, keys: tuple[str, ...]) -> int:
        if not keys:
            return 0
        placeholders = ",".join("?" for _ in keys)
        cursor = self.connection.execute(
            f"DELETE FROM settings WHERE key IN ({placeholders})", keys,
        )
        if self.connection.execute(
            f"SELECT 1 FROM settings WHERE key IN ({placeholders}) LIMIT 1", keys,
        ).fetchone():
            raise RuntimeError("SQLite did not delete the requested settings")
        return max(0, cursor.rowcount)

    async def delete_settings(self, keys: set[str] | frozenset[str]) -> int:
        validated = self._validated_setting_keys(keys)
        if not validated:
            return 0
        async with self._transaction():
            return self._delete_settings_unlocked(validated)

    async def get_settings(self) -> dict[str, JsonValue]:
        async with self.lock:
            rows = self.connection.execute("SELECT key,value_json FROM settings").fetchall()
            result: dict[str, JsonValue] = {}
            for row in rows:
                with suppress(json.JSONDecodeError):
                    result[row["key"]] = json.loads(row["value_json"])
            return result

    async def get_setting(self, key: str, default: JsonValue = None) -> JsonValue:
        async with self.lock:
            row = self.connection.execute(
                "SELECT value_json FROM settings WHERE key=?",
                (key,),
            ).fetchone()
            if row is None:
                return default
            try:
                return cast(JsonValue, json.loads(row["value_json"]))
            except json.JSONDecodeError:
                return default

    async def remember_map_obstacle(
        self,
        location_name: str,
        position: tuple[int, int],
    ) -> bool:
        """Persist a blocked cell learned from an inbound map message."""
        x, y = position
        require_int64(x, "position_x")
        require_int64(y, "position_y")
        async with self._transaction():
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO map_obstacles(
                    location_name, position_x, position_y, discovered_at
                ) VALUES (?, ?, ?, ?)
                """,
                (location_name, x, y, utc_now()),
            )
            return cursor.rowcount > 0

    async def get_map_obstacles(self, location_name: str) -> set[tuple[int, int]]:
        async with self.lock:
            rows = self.connection.execute(
                """
                SELECT position_x, position_y
                FROM map_obstacles
                WHERE location_name=?
                """,
                (location_name,),
            ).fetchall()
            return {(int(row["position_x"]), int(row["position_y"])) for row in rows}

    async def forget_map_obstacles(
        self,
        location_name: str,
        positions: set[tuple[int, int]],
    ) -> int:
        if not positions:
            return 0
        for x, y in positions:
            require_int64(x, "position_x")
            require_int64(y, "position_y")
        async with self._transaction():
            deleted = self.connection.executemany(
                """
                DELETE FROM map_obstacles
                WHERE location_name=? AND position_x=? AND position_y=?
                """,
                [(location_name, x, y) for x, y in positions],
            ).rowcount
            return max(0, deleted)

    async def clear_map_obstacles(self) -> int:
        """Forget observations created by an incompatible navigation model."""
        async with self._transaction():
            cursor = self.connection.execute("DELETE FROM map_obstacles")
            return max(0, cursor.rowcount)

    async def add_event(
        self,
        event_type: str,
        message: str,
        level: str = "INFO",
        payload: Mapping[str, JsonValue] | None = None,
    ) -> int:
        serialized_payload = (
            canonical_json_object(payload)
            if payload is not None
            else None
        )
        async with self._transaction():
            cur = self.connection.execute(
                """
                INSERT INTO events(created_at,level,event_type,message,payload_json)
                VALUES (?,?,?,?,?)
            """,
                (
                    utc_now(),
                    level,
                    event_type,
                    message,
                    serialized_payload,
                ),
            )
            if cur.rowcount != 1 or cur.lastrowid is None:
                raise RuntimeError("SQLite не вернул ID нового события")
            return int(cur.lastrowid)

    async def record_battle_outcome(
        self,
        outcome: BattleOutcome,
        *,
        events: tuple[BattleEvent, ...] = (),
        legacy_source_event_ids: tuple[SourceEventId, ...] = (),
    ) -> RecordBattleResult:
        """Atomically record normalized mandatory facts without invoking game diagnostics."""
        events = tuple(events)
        if any(not isinstance(event, BattleEvent) for event in events):
            raise ValueError("Expected immutable BattleEvent values")
        legacy_source_event_ids = tuple(legacy_source_event_ids)
        if any(
            not isinstance(identifier, SourceEventId)
            for identifier in legacy_source_event_ids
        ):
            raise ValueError("Expected immutable SourceEventId aliases")
        if len(set(legacy_source_event_ids)) != len(legacy_source_event_ids):
            raise ValueError("Legacy source event aliases must be unique")
        if outcome.source_event_id in legacy_source_event_ids:
            raise ValueError("Current source event id cannot also be a legacy alias")
        happened_at = outcome.happened_at.astimezone(UTC).isoformat()
        rewards = outcome.rewards
        async with self._transaction():
            alias_matches = [
                row
                for identifier in legacy_source_event_ids
                if (
                    row := self.connection.execute(
                        "SELECT id,source_event_id FROM battles WHERE source_event_id=?",
                        (identifier.value,),
                    ).fetchone()
                )
                is not None
            ]
            if len(alias_matches) > 1:
                raise IdempotencyConflict(
                    "Multiple legacy battle rows claim the same source event"
                )
            previous = self.connection.execute(
                "SELECT id FROM battles WHERE source_event_id=?",
                (outcome.source_event_id.value,),
            ).fetchone()
            if previous is None and alias_matches:
                alias = alias_matches[0]
                battle_id = require_int64(alias["id"], "battle_id", minimum=1)
                if (
                    self._stored_battle_fingerprint(battle_id)
                    == self._outcome_fingerprint(outcome)
                ):
                    rekeyed = self.connection.execute(
                        "UPDATE battles SET source_event_id=? "
                        "WHERE id=? AND source_event_id=?",
                        (
                            outcome.source_event_id.value,
                            battle_id,
                            alias["source_event_id"],
                        ),
                    )
                    if rekeyed.rowcount != 1:
                        raise RuntimeError("SQLite did not rekey the legacy battle identity")
                    previous = alias
            if previous is not None:
                battle_id = require_int64(previous["id"], "battle_id", minimum=1)
                if self._stored_battle_fingerprint(battle_id) != self._outcome_fingerprint(outcome):
                    raise IdempotencyConflict(
                        f"Battle source event {outcome.source_event_id.value!r} "
                        "has conflicting rewards/result"
                    )
                self._enqueue_battle_events_unlocked(battle_id, events)
                return RecordBattleResult(inserted=False, battle_id=battle_id)
            px, py = outcome.position if outcome.position is not None else (None, None)
            cursor = self.connection.execute(
                """
                INSERT INTO battles(
                    source_event_id,source_message_id,session_id,happened_at,
                    target_name,result,xp,dust,position_x,position_y
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    outcome.source_event_id.value,
                    outcome.source_message_id,
                    outcome.session_id,
                    happened_at,
                    outcome.target_name,
                    outcome.result,
                    rewards.xp,
                    rewards.dust,
                    px,
                    py,
                ),
            )
            if cursor.rowcount != 1 or cursor.lastrowid is None:
                raise RuntimeError("SQLite не сохранил обязательный исход боя")
            battle_id = int(cursor.lastrowid)
            if rewards.crystals > 0:
                currency = self.connection.execute(
                    "INSERT INTO battle_currencies(battle_id,currency_code,amount) VALUES (?,?,?)",
                    (battle_id, MIST_CRYSTAL_CODE, rewards.crystals),
                )
                if currency.rowcount != 1:
                    raise RuntimeError("SQLite не сохранил обязательную валюту боя")
            for item in rewards.items:
                drop = self.connection.execute(
                    "INSERT INTO drops(battle_id,item_name,quantity,is_card) VALUES (?,?,?,?)",
                    (battle_id, item.name, item.quantity, int(item.is_card)),
                )
                if drop.rowcount != 1:
                    raise RuntimeError("SQLite не сохранил обязательный предмет боя")
            if outcome.session_id is not None:
                current = self.connection.execute(
                    "SELECT wins,defeats,xp,dust FROM sessions WHERE id=?", (outcome.session_id,),
                ).fetchone()
                if current is None:
                    raise RuntimeError("SQLite has no required battle session")
                increments = (int(outcome.result == "VICTORY"), int(outcome.result == "DEFEAT"),
                              rewards.xp, rewards.dust)
                for field, increment in zip(
                    ("wins", "defeats", "xp", "dust"), increments, strict=True
                ):
                    require_int64(
                        require_int64(current[field], field) + increment, field, minimum=0
                    )
                session = self.connection.execute(
                    """
                    UPDATE sessions SET wins=wins+?, defeats=defeats+?,
                    xp=xp+?, dust=dust+? WHERE id=?
                    """,
                    (
                        int(outcome.result == "VICTORY"),
                        int(outcome.result == "DEFEAT"),
                        rewards.xp,
                        rewards.dust,
                        outcome.session_id,
                    ),
                )
                if session.rowcount != 1:
                    raise RuntimeError("SQLite не обновил обязательные итоги сессии")
            self._enqueue_battle_events_unlocked(battle_id, events)
        return RecordBattleResult(
            inserted=True,
            battle_id=battle_id,
            cards=tuple(item.name for item in rewards.items if item.is_card),
        )

    @staticmethod
    def _content_fingerprint(
        source_message_id: int | None,
        target: str,
        result: str,
        xp: int,
        dust: int,
        crystals: int,
        items: Iterable[tuple[str, int, bool]],
    ) -> str:
        content = (
            source_message_id,
            target.strip(),
            result,
            xp,
            dust,
            crystals,
            sorted(items),
        )
        return hashlib.sha256(json.dumps(
            content, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    @classmethod
    def _outcome_fingerprint(cls, outcome: BattleOutcome) -> str:
        reward = outcome.rewards
        return cls._content_fingerprint(
            outcome.source_message_id,
            outcome.target_name,
            outcome.result,
            reward.xp,
            reward.dust,
            reward.crystals,
            ((item.name, item.quantity, item.is_card) for item in reward.items),
        )

    async def get_battle_outcome(self, battle_id: int) -> BattleOutcome | None:
        """Read one consistent immutable ledger snapshot, including original context."""
        require_int64(battle_id, "battle_id", minimum=1)
        async with self._transaction():
            return self._get_battle_outcome_unlocked(battle_id)

    def _get_battle_outcome_unlocked(self, battle_id: int) -> BattleOutcome | None:
        row = self.connection.execute("SELECT * FROM battles WHERE id=?", (battle_id,)).fetchone()
        if row is None:
            return None
        currency = self.connection.execute(
            "SELECT amount FROM battle_currencies WHERE battle_id=? AND currency_code=?",
            (battle_id, MIST_CRYSTAL_CODE),
        ).fetchone()
        drop_rows = self.connection.execute(
            "SELECT item_name,quantity,is_card FROM drops WHERE battle_id=? ORDER BY id",
            (battle_id,),
        ).fetchall()
        items: list[ItemDrop] = []
        for item in drop_rows:
            card_flag = require_int64(item["is_card"], "is_card", minimum=0)
            if card_flag not in (0, 1):
                raise ValueError("Stored is_card must be 0 or 1")
            items.append(ItemDrop(item["item_name"], item["quantity"], bool(card_flag)))
        x, y = row["position_x"], row["position_y"]
        if x is None and y is None:
            position = None
        elif x is None or y is None:
            raise ValueError("Stored battle position must contain both coordinates")
        else:
            position = (require_int64(x, "position_x"), require_int64(y, "position_y"))
        return BattleOutcome(
            source_event_id=SourceEventId(row["source_event_id"]),
            source_message_id=(
                require_int64(row["source_message_id"], "source_message_id", minimum=1)
                if row["source_message_id"] is not None
                else None
            ),
            session_id=row["session_id"],
            target_name=row["target_name"], result=cast(BattleResult, row["result"]),
            rewards=RewardBundle(xp=row["xp"], dust=row["dust"],
                                 crystals=currency["amount"] if currency is not None else 0,
                                 items=tuple(items)),
            position=position, happened_at=datetime.fromisoformat(str(row["happened_at"])),
        )

    def _stored_battle_fingerprint(self, battle_id: int) -> str:
        outcome = self._get_battle_outcome_unlocked(battle_id)
        if outcome is None:
            raise RuntimeError("Expected an existing battle for idempotency comparison")
        return self._outcome_fingerprint(outcome)

    def _enqueue_battle_events_unlocked(
        self, battle_id: int, events: tuple[BattleEvent, ...]
    ) -> None:
        for event in events:
            cursor = self.connection.execute(
                """INSERT INTO battle_outbox(
                    battle_id,namespace,idempotency_key,event_type,schema_version,
                    payload_json,created_at
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(battle_id,namespace,idempotency_key) DO NOTHING""",
                (battle_id, event.namespace, event.idempotency_key, event.event_type,
                 event.schema_version, event.payload_json, utc_now()),
            )
            if cursor.rowcount == 1:
                continue
            previous = self.connection.execute(
                "SELECT event_type,schema_version,payload_json FROM battle_outbox "
                "WHERE battle_id=? AND namespace=? AND idempotency_key=?",
                (battle_id, event.namespace, event.idempotency_key),
            ).fetchone()
            if previous is None:
                raise RuntimeError("SQLite did not save a required battle event")
            if tuple(previous) != (event.event_type, event.schema_version, event.payload_json):
                raise IdempotencyConflict("Conflicting event for the same battle/namespace/key")

    @staticmethod
    def _decode_battle_outbox_row(row: sqlite3.Row) -> BattleOutboxEnvelope:
        """Reject malformed persisted envelopes instead of coercing their identity."""
        return BattleOutboxEnvelope(
            id=row["id"],
            battle_id=row["battle_id"],
            created_at=row["created_at"],
            event=BattleEvent(
                row["namespace"],
                row["idempotency_key"],
                row["event_type"],
                row["schema_version"],
                row["payload_json"],
            ),
            attempts=row["attempts"],
            next_attempt_at=row["next_attempt_at"],
            last_error=row["last_error"],
        )

    async def pending_battle_events(
        self, *, namespace: str, limit: int = 100, after_id: int = 0,
        include_deferred: bool = False,
    ) -> tuple[BattleOutboxEnvelope, ...]:
        if not isinstance(namespace, str) or not namespace.strip():
            raise ValueError("namespace must be nonblank")
        require_int64(limit, "limit", minimum=1)
        require_int64(after_id, "after_id", minimum=0)
        if type(include_deferred) is not bool:
            raise ValueError("include_deferred must be bool")
        retry_filter = "" if include_deferred else (
            " AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
        )
        params: tuple[object, ...] = (namespace.strip(), after_id)
        if not include_deferred:
            params += (utc_now(),)
        async with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM battle_outbox WHERE namespace=? AND acknowledged_at IS NULL "
                f"AND id > ?{retry_filter} ORDER BY id LIMIT ?", (*params, limit),
            ).fetchall()
        return tuple(self._decode_battle_outbox_row(row) for row in rows)

    async def pending_battle_event_entries(
        self, *, namespace: str, limit: int = 100, after_id: int | None = None,
        include_deferred: bool = False,
    ) -> tuple[BattleOutboxEnvelope | InvalidBattleOutboxEntry, ...]:
        """Isolate corrupt rows without hiding healthy rows behind deferred ones.

        Unlike the strict envelope reader, this consumer boundary never coerces
        corrupted metadata. Undecodable rows retain their exact identity and can
        be deferred individually. Decode errors do not block the whole batch.
        """
        if not isinstance(namespace, str) or not namespace.strip():
            raise ValueError("namespace must be nonblank")
        require_int64(limit, "limit", minimum=1)
        if after_id is not None:
            require_int64(after_id, "after_id")
        if type(include_deferred) is not bool:
            raise ValueError("include_deferred must be bool")
        now = datetime.now(UTC)
        entries: list[BattleOutboxEnvelope | InvalidBattleOutboxEntry] = []
        cursor_filter = "" if after_id is None else " AND id>?"
        params: tuple[object, ...] = (namespace.strip(),)
        if after_id is not None:
            params += (after_id,)
        async with self.lock:
            cursor = self.connection.execute(
                "SELECT * FROM battle_outbox WHERE namespace=? AND acknowledged_at IS NULL"
                f"{cursor_filter} ORDER BY id", params,
            )
            for row in cursor:
                try:
                    envelope = self._decode_battle_outbox_row(row)
                except (TypeError, ValueError, OverflowError, RecursionError) as error:
                    # A valid quarantine deadline still applies when some other
                    # metadata is corrupt. Invalid deadlines are immediately due.
                    try:
                        deferred_until = self._aware_utc(row["next_attempt_at"])
                    except (TypeError, ValueError):
                        deferred_until = now
                    if not include_deferred and deferred_until > now:
                        continue
                    entries.append(InvalidBattleOutboxEntry(
                        row["id"], row["namespace"], f"{type(error).__name__}: {error}",
                    ))
                else:
                    if (not include_deferred and envelope.next_attempt_at is not None
                            and self._aware_utc(envelope.next_attempt_at) > now):
                        continue
                    entries.append(envelope)
                if len(entries) == limit:
                    break
        return tuple(entries)

    @staticmethod
    def _aware_utc(value: object) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Expected an aware timestamp")
        return value.astimezone(UTC)

    @staticmethod
    def _retry_at(delay: float) -> datetime:
        try:
            valid = (not isinstance(delay, bool) and isinstance(delay, (int, float))
                     and math.isfinite(delay) and delay >= 0)
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError("retry_after_seconds must be finite and nonnegative")
        try:
            return datetime.now(UTC) + timedelta(seconds=delay)
        except OverflowError as error:
            raise ValueError("retry_after_seconds exceeds supported timestamp range") from error

    async def defer_invalid_battle_event(
        self, entry: InvalidBattleOutboxEntry, *, retry_after_seconds: float = 60,
    ) -> bool:
        """Quarantine one still-invalid row; never repair/coerce its corrupt data."""
        if not isinstance(entry, InvalidBattleOutboxEntry):
            raise ValueError("entry must be InvalidBattleOutboxEntry")
        next_attempt = self._retry_at(retry_after_seconds).isoformat()
        async with self._transaction():
            row = self.connection.execute(
                "SELECT * FROM battle_outbox WHERE id=? AND namespace=? "
                "AND acknowledged_at IS NULL", (entry.id, entry.namespace),
            ).fetchone()
            if row is None:
                return False
            try:
                self._decode_battle_outbox_row(row)
            except (TypeError, ValueError, OverflowError, RecursionError):
                cursor = self.connection.execute(
                    "UPDATE battle_outbox SET attempts=CASE "
                    "WHEN typeof(attempts)='integer' AND attempts>=0 AND attempts<? "
                    "THEN attempts+1 ELSE attempts END,next_attempt_at=?,last_error=? "
                    "WHERE id=? AND namespace=? AND acknowledged_at IS NULL",
                    (INT64_MAX, next_attempt, entry.decode_error[:2000], entry.id, entry.namespace),
                )
                return cursor.rowcount == 1
            return False

    @staticmethod
    def _barrier_value_preview(value: object) -> str:
        preview = repr(value)
        return preview if len(preview) <= 500 else preview[:497] + "..."

    def _repair_battle_event_barrier_unlocked(
        self,
        *,
        namespace: str,
        corrupt_value: object,
        repaired_until: datetime,
    ) -> datetime:
        repaired_until = self._aware_utc(repaired_until)
        updated = self.connection.execute(
            "UPDATE battle_outbox_barriers SET blocked_until=? WHERE namespace=?",
            (repaired_until.isoformat(), namespace),
        )
        if updated.rowcount != 1:
            raise RuntimeError("SQLite did not repair the corrupt outbox barrier")
        audit_payload: dict[str, JsonValue] = {
            "namespace": namespace,
            "previous_value_type": type(corrupt_value).__name__,
            "previous_value_preview": self._barrier_value_preview(corrupt_value),
            "repaired_until": repaired_until.isoformat(),
        }
        audit = self.connection.execute(
            """
            INSERT INTO events(created_at,level,event_type,message,payload_json)
            VALUES (?,?,?,?,?)
            """,
            (
                utc_now(),
                "WARNING",
                "BATTLE_OUTBOX_BARRIER_REPAIRED",
                "Повреждённый барьер очереди боя восстановлен",
                canonical_json_object(audit_payload),
            ),
        )
        if audit.rowcount != 1 or audit.lastrowid is None:
            raise RuntimeError("SQLite did not audit the corrupt outbox barrier repair")
        actual = self.connection.execute(
            "SELECT blocked_until FROM battle_outbox_barriers WHERE namespace=?",
            (namespace,),
        ).fetchone()
        if actual is None or self._aware_utc(actual[0]) != repaired_until:
            raise RuntimeError("SQLite did not persist the repaired outbox barrier")
        return repaired_until

    def _extend_battle_event_barrier_unlocked(
        self, namespace: str, until: datetime
    ) -> datetime:
        if not isinstance(namespace, str) or not namespace.strip():
            raise ValueError("namespace must be nonblank")
        namespace = namespace.strip()
        until = self._aware_utc(until)
        row = self.connection.execute(
            "SELECT blocked_until FROM battle_outbox_barriers WHERE namespace=?",
            (namespace,),
        ).fetchone()
        if row is not None:
            try:
                previous_until = self._aware_utc(row[0])
            except (TypeError, ValueError, OverflowError):
                return self._repair_battle_event_barrier_unlocked(
                    namespace=namespace,
                    corrupt_value=row[0],
                    repaired_until=until,
                )
            until = max(until, previous_until)
        self.connection.execute(
            "INSERT INTO battle_outbox_barriers(namespace,blocked_until) VALUES (?,?) "
            "ON CONFLICT(namespace) DO UPDATE SET blocked_until=excluded.blocked_until",
            (namespace, until.isoformat()),
        )
        actual = self.connection.execute(
            "SELECT blocked_until FROM battle_outbox_barriers WHERE namespace=?",
            (namespace,),
        ).fetchone()
        if actual is None or self._aware_utc(actual[0]) < until:
            raise RuntimeError("SQLite did not persist the outbox barrier")
        return self._aware_utc(actual[0])

    async def extend_battle_event_barrier(
        self, *, namespace: str, blocked_until: datetime,
    ) -> datetime:
        """Monotonically extend a durable namespace-wide delivery deadline.

        This is a retry barrier, not a lease. A single external side-effect
        consumer is still required. The barrier survives ACK and retention.
        """
        until = self._aware_utc(blocked_until)
        async with self._transaction():
            return self._extend_battle_event_barrier_unlocked(namespace, until)

    async def get_battle_event_barrier(self, *, namespace: str) -> datetime | None:
        if not isinstance(namespace, str) or not namespace.strip():
            raise ValueError("namespace must be nonblank")
        namespace = namespace.strip()
        async with self._transaction():
            row = self.connection.execute(
                "SELECT blocked_until FROM battle_outbox_barriers WHERE namespace=?",
                (namespace,),
            ).fetchone()
            if row is None:
                return None
            try:
                return self._aware_utc(row[0])
            except (TypeError, ValueError, OverflowError):
                fallback = datetime.now(UTC) + timedelta(
                    seconds=CORRUPT_BARRIER_FALLBACK_SECONDS
                )
                return self._repair_battle_event_barrier_unlocked(
                    namespace=namespace,
                    corrupt_value=row[0],
                    repaired_until=fallback,
                )

    async def ack_battle_event(self, event_id: int, *, namespace: str) -> bool:
        if not isinstance(namespace, str) or not namespace.strip():
            raise ValueError("namespace must be nonblank")
        require_int64(event_id, "event_id", minimum=1)
        async with self._transaction():
            cursor = self.connection.execute(
                "UPDATE battle_outbox SET acknowledged_at=?,next_attempt_at=NULL,last_error=NULL "
                "WHERE id=? AND namespace=? AND acknowledged_at IS NULL",
                (utc_now(), event_id, namespace.strip()),
            )
            return cursor.rowcount == 1

    async def fail_battle_event(
        self, event_id: int, *, namespace: str, error: str, retry_after_seconds: float = 60,
    ) -> bool:
        """Keep a failed intent durable while scheduling a later idempotent retry."""
        require_int64(event_id, "event_id", minimum=1)
        if not isinstance(namespace, str) or not namespace.strip():
            raise ValueError("namespace must be nonblank")
        next_attempt = self._retry_at(retry_after_seconds).isoformat()
        async with self._transaction():
            cursor = self.connection.execute(
                "UPDATE battle_outbox SET attempts=CASE WHEN attempts<? "
                "THEN attempts+1 ELSE attempts END, "
                "next_attempt_at=?,last_error=? "
                "WHERE id=? AND namespace=? AND acknowledged_at IS NULL",
                (INT64_MAX, next_attempt, str(error)[:2000], event_id, namespace.strip()),
            )
            return cursor.rowcount == 1

    async def load_combat_knowledge(
        self, *, namespace: str
    ) -> dict[int, dict[str, JsonValue]]:
        """Load valid optional profiles and durably quarantine corrupt rows.

        A combat-knowledge row is an optimization, so one corrupt profile must not
        prevent the farmer from starting. Quarantine is an atomic delete plus audit
        event; a database failure still propagates and rolls the delete back.
        """
        namespace = self._validate_combat_knowledge_namespace(namespace)
        async with self.lock:
            rows = self.connection.execute(
                "SELECT rowid AS storage_rowid,profile_max_hp,knowledge_json "
                "FROM combat_knowledge WHERE namespace=?",
                (namespace,),
            ).fetchall()

        result: dict[int, dict[str, JsonValue]] = {}
        corrupt_rows: list[tuple[int, object, object, str, str]] = []
        for row in rows:
            storage_rowid = require_int64(
                row["storage_rowid"], "Stored combat knowledge rowid", minimum=1
            )
            stored_profile = row["profile_max_hp"]
            raw_json = row["knowledge_json"]
            try:
                profile_max_hp = require_int64(
                    stored_profile, "Stored profile_max_hp", minimum=1
                )
                if not isinstance(raw_json, str):
                    raise ValueError("Stored combat knowledge must be JSON text")
                payload: object = json.loads(raw_json)
                if not isinstance(payload, dict):
                    raise ValueError("Stored combat knowledge must be a JSON object")
                canonical = canonical_json_object(
                    cast(Mapping[str, JsonValue], payload)
                )
            except (TypeError, ValueError, OverflowError, RecursionError) as error:
                corrupt_rows.append(
                    (
                        storage_rowid,
                        stored_profile,
                        raw_json,
                        type(error).__name__,
                        str(error)[:500],
                    )
                )
                continue
            result[profile_max_hp] = cast(
                dict[str, JsonValue], json.loads(canonical)
            )

        if not corrupt_rows:
            return result

        async with self._transaction():
            for storage_rowid, stored_profile, raw_json, error_type, reason in corrupt_rows:
                deleted = self.connection.execute(
                    "DELETE FROM combat_knowledge "
                    "WHERE rowid=? AND namespace=? "
                    "AND profile_max_hp IS ? AND knowledge_json IS ?",
                    (storage_rowid, namespace, stored_profile, raw_json),
                )
                # Another caller may have repaired or quarantined the row after
                # our read. The exact predicate prevents deleting that newer value.
                if deleted.rowcount != 1:
                    continue
                audit_payload: dict[str, JsonValue] = {
                    "namespace": namespace,
                    "storage_rowid": storage_rowid,
                    "profile_key_type": type(stored_profile).__name__,
                    "error_type": error_type,
                    "reason": reason,
                }
                event = self.connection.execute(
                    """
                    INSERT INTO events(created_at,level,event_type,message,payload_json)
                    VALUES (?,?,?,?,?)
                    """,
                    (
                        utc_now(),
                        "WARNING",
                        "COMBAT_KNOWLEDGE_QUARANTINED",
                        "Повреждённый необязательный профиль боевой памяти удалён",
                        canonical_json_object(audit_payload),
                    ),
                )
                if event.rowcount != 1 or event.lastrowid is None:
                    raise RuntimeError("SQLite не подтвердил журналирование карантина")
        return result

    @staticmethod
    def _validate_combat_knowledge_namespace(namespace: str) -> str:
        if not isinstance(namespace, str) or not namespace.strip():
            raise ValueError("Namespace боевой памяти не должен быть пустым")
        return namespace.strip()

    async def save_combat_knowledge(
        self,
        profile_max_hp: int,
        payload: Mapping[str, JsonValue],
        *,
        namespace: str,
    ) -> None:
        namespace = self._validate_combat_knowledge_namespace(namespace)
        require_int64(profile_max_hp, "profile_max_hp", minimum=1)
        serialized = canonical_json_object(payload)
        async with self._transaction():
            self.connection.execute(
                """
                INSERT INTO combat_knowledge(namespace,profile_max_hp,updated_at,knowledge_json)
                VALUES (?,?,?,?)
                ON CONFLICT(namespace,profile_max_hp) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    knowledge_json=excluded.knowledge_json
                """,
                (
                    namespace,
                    profile_max_hp,
                    utc_now(),
                    serialized,
                ),
            )

    async def get_current_session(self) -> SessionSummary:
        async with self.lock:
            row = self.connection.execute("""
                SELECT s.* FROM sessions s JOIN farmer_state f
                ON f.session_id=s.id WHERE f.singleton=1
            """).fetchone()
            if not row:
                return SessionSummary(None, None, "STOPPED", 0, 0, 0, 0, 0)
            return SessionSummary(
                row["id"],
                row["started_at"],
                row["status"],
                row["wins"],
                row["defeats"],
                row["xp"],
                row["dust"],
                row["runtime_seconds"],
            )

    async def get_drops(self, session_id: int | None = None) -> list[DropSummary]:
        query = """
            SELECT d.item_name,SUM(d.quantity) quantity,MAX(d.is_card) is_card
            FROM drops d JOIN battles b ON b.id=d.battle_id
        """
        params: tuple[object, ...] = ()
        if session_id is not None:
            query += " WHERE b.session_id=?"
            params = (session_id,)
        query += " GROUP BY d.item_name ORDER BY is_card DESC,quantity DESC,d.item_name"
        async with self.lock:
            return [
                cast(DropSummary, dict(row))
                for row in self.connection.execute(query, params).fetchall()
            ]

    async def get_events(self, limit: int = 20) -> list[EventSummary]:
        async with self.lock:
            rows = self.connection.execute(
                """
                SELECT created_at,level,event_type,message FROM events
                ORDER BY id DESC LIMIT ?
            """,
                (limit,),
            ).fetchall()
            return [cast(EventSummary, dict(row)) for row in rows]

    async def get_statistics_dashboard(self) -> StatisticsDashboard:
        session = await self.get_current_session()
        async with self.lock:
            sid = session.session_id
            battle: BattleTotals
            drops: DropTotals
            targets: list[TargetTotals]
            if sid is None:
                battle = {
                    "battles": 0,
                    "wins": 0,
                    "defeats": 0,
                    "xp": 0,
                    "dust": 0,
                    "crystals": 0,
                }
                drops = {"items": 0, "cards": 0}
                targets = []
            else:
                row = self.connection.execute(
                    """SELECT COUNT(*) battles,
                    COALESCE(SUM(b.result='VICTORY'),0) wins,
                    COALESCE(SUM(b.result='DEFEAT'),0) defeats,
                    COALESCE(SUM(b.xp),0) xp, COALESCE(SUM(b.dust),0) dust,
                    COALESCE(SUM(c.amount),0) crystals
                    FROM battles b
                    LEFT JOIN battle_currencies c
                      ON c.battle_id=b.id AND c.currency_code=?
                    WHERE b.session_id=?""",
                    (MIST_CRYSTAL_CODE, sid),
                ).fetchone()
                battle = cast(BattleTotals, dict(row))
                row = self.connection.execute(
                    """SELECT COALESCE(SUM(d.quantity),0) items,
                    COALESCE(SUM(CASE WHEN d.is_card=1 THEN d.quantity ELSE 0 END),0) cards
                    FROM drops d JOIN battles b ON b.id=d.battle_id WHERE b.session_id=?""",
                    (sid,),
                ).fetchone()
                drops = cast(DropTotals, dict(row))
                targets = [
                    cast(TargetTotals, dict(r))
                    for r in self.connection.execute(
                        """SELECT b.target_name, COUNT(*) battles,
                    SUM(b.result='VICTORY') wins, COALESCE(SUM(b.xp),0) xp,
                    COALESCE(SUM(b.dust),0) dust,
                    COALESCE(SUM(c.amount),0) crystals
                    FROM battles b
                    LEFT JOIN battle_currencies c
                      ON c.battle_id=b.id AND c.currency_code=?
                    WHERE b.session_id=? GROUP BY b.target_name
                    ORDER BY wins DESC, battles DESC, b.target_name""",
                        (MIST_CRYSTAL_CODE, sid),
                    ).fetchall()
                ]
            state = self._get_state_unlocked()
        runtime = session.runtime_seconds
        if session.started_at and session.status == "RUNNING":
            with suppress(ValueError):
                runtime = max(
                    0,
                    int(
                        (
                            datetime.now(UTC) - datetime.fromisoformat(session.started_at)
                        ).total_seconds()
                    ),
                )
        return {
            "session": session,
            "battle": battle,
            "drops": drops,
            "targets": targets,
            "state": state,
            "runtime_seconds": runtime,
        }

    @staticmethod
    def format_statistics_text(data: StatisticsDashboard) -> str:
        b, d, st = data["battle"], data["drops"], data["state"]
        seconds = int(data.get("runtime_seconds", 0))
        h, rem = divmod(seconds, 3600)
        m, sec = divmod(rem, 60)
        return (
            "📈 Статистика текущей сессии\n\n"
            f"⏱ Время: {h:02d}:{m:02d}:{sec:02d}\n"
            f"⚔️ Боев: {b.get('battles', 0)}\n"
            f"🏆 Побед: {b.get('wins', 0)}\n"
            f"☠️ Поражений: {b.get('defeats', 0)}\n"
            f"✨ XP: {b.get('xp', 0)}\n"
            f"💠 Пыль: {b.get('dust', 0)}\n"
            f"💎 Кристаллы: {b.get('crystals', 0)}\n"
            f"🎁 Предметов: {d.get('items', 0)}\n"
            f"🃏 Карт: {d.get('cards', 0)}\n"
            f"👣 Ходов: {st.get('moves', 0)}"
        )
