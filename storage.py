from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rewards import parse_item_stack


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class SessionSummary:
    session_id: int | None
    started_at: str | None
    ended_at: str | None
    status: str
    wins: int
    defeats: int
    xp: int
    dust: int
    runtime_seconds: int


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self._create_schema()

    def _create_schema(self) -> None:
        self.connection.executescript("""
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
        );

        CREATE TABLE IF NOT EXISTS drops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            battle_id INTEGER NOT NULL,
            item_name TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            is_card INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(battle_id) REFERENCES battles(id) ON DELETE CASCADE
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
            max_moves INTEGER NOT NULL DEFAULT 0,
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
            payload_json TEXT,
            notified INTEGER NOT NULL DEFAULT 0
        );

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
        """)
        self.connection.commit()

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

    async def cleanup_old_data(self, retention_days: int = 7) -> dict[str, int]:
        """Удаляет диагностические и статистические записи старше retention_days."""
        cutoff = (datetime.now(UTC) - timedelta(days=max(1, retention_days))).isoformat()
        async with self.lock:
            deleted_events = self.connection.execute(
                "DELETE FROM events WHERE created_at < ?", (cutoff,)
            ).rowcount
            old_battle_ids = [
                int(row["id"])
                for row in self.connection.execute(
                    "SELECT id FROM battles WHERE happened_at < ?", (cutoff,)
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
            self.connection.commit()
            self.connection.execute("PRAGMA optimize")
            return {
                "events": max(0, deleted_events),
                "drops": max(0, deleted_drops),
                "battles": max(0, deleted_battles),
                "sessions": max(0, deleted_sessions),
            }

    async def start_session(
        self,
        *,
        cycles_count: int,
        moves_per_cycle: int,
    ) -> int:
        async with self.lock:
            now = utc_now()
            self._close_abandoned_sessions()
            cursor = self.connection.execute(
                "INSERT INTO sessions(started_at,status) VALUES (?, 'RUNNING')",
                (now,),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite не вернул ID новой сессии")
            sid = int(cursor.lastrowid)
            self.connection.execute(
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
            self.connection.commit()
            return sid

    async def finish_session(self, session_id, reason, runtime_seconds) -> None:
        async with self.lock:
            if session_id is not None:
                self.connection.execute(
                    """
                    UPDATE sessions SET ended_at=?, status='STOPPED',
                    stop_reason=?, runtime_seconds=? WHERE id=?
                """,
                    (utc_now(), reason, runtime_seconds, session_id),
                )
            self.connection.execute(
                """
                UPDATE farmer_state SET process_status='STOPPED',
                game_state='STOPPED', active_target=NULL,
                last_action=?, last_progress_at=?, pause_requested=0,
                rest_until=NULL WHERE singleton=1
            """,
                (reason, utc_now()),
            )
            self.connection.commit()

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

    async def update_state(self, **fields) -> None:
        allowed = {
            "process_status",
            "game_state",
            "position_x",
            "position_y",
            "current_hp",
            "max_hp",
            "active_target",
            "moves",
            "max_moves",
            "last_action",
            "last_progress_at",
            "last_error",
            "session_id",
            "current_cycle",
            "cycles_count",
            "moves_in_cycle",
            "moves_per_cycle",
            "rest_until",
            "pause_requested",
        }
        clean = {k: v for k, v in fields.items() if k in allowed}
        if not clean:
            return
        sql = ", ".join(f"{k}=?" for k in clean)
        async with self.lock:
            self.connection.execute(
                f"UPDATE farmer_state SET {sql} WHERE singleton=1",
                list(clean.values()),
            )
            self.connection.commit()

    async def get_state(self) -> dict:
        async with self.lock:
            row = self.connection.execute("SELECT * FROM farmer_state WHERE singleton=1").fetchone()
            return dict(row) if row else {}

    async def set_setting(self, key: str, value) -> None:
        await self.set_settings({key: value})

    async def set_settings(self, values: dict) -> None:
        if not values:
            return
        updated_at = utc_now()
        rows = [
            (key, json.dumps(value, ensure_ascii=False), updated_at)
            for key, value in values.items()
        ]
        async with self.lock:
            self.connection.executemany(
                """
                INSERT INTO settings(key,value_json,updated_at)
                VALUES (?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json=excluded.value_json,
                    updated_at=excluded.updated_at
                WHERE settings.value_json != excluded.value_json
            """,
                rows,
            )
            self.connection.commit()

    async def get_settings(self) -> dict:
        async with self.lock:
            rows = self.connection.execute("SELECT key,value_json FROM settings").fetchall()
            result = {}
            for row in rows:
                with suppress(json.JSONDecodeError):
                    result[row["key"]] = json.loads(row["value_json"])
            return result

    async def get_setting(self, key: str, default=None):
        async with self.lock:
            row = self.connection.execute(
                "SELECT value_json FROM settings WHERE key=?",
                (key,),
            ).fetchone()
            if row is None:
                return default
            try:
                return json.loads(row["value_json"])
            except json.JSONDecodeError:
                return default

    async def remember_map_obstacle(
        self,
        location_name: str,
        position: tuple[int, int],
    ) -> bool:
        """Persist a blocked cell learned from an inbound map message."""
        x, y = position
        async with self.lock:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO map_obstacles(
                    location_name, position_x, position_y, discovered_at
                ) VALUES (?, ?, ?, ?)
                """,
                (location_name, x, y, utc_now()),
            )
            self.connection.commit()
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

    async def add_event(self, event_type, message, level="INFO", payload=None) -> int:
        async with self.lock:
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
                    json.dumps(payload, ensure_ascii=False) if payload else None,
                ),
            )
            self.connection.commit()
            if cur.lastrowid is None:
                raise RuntimeError("SQLite не вернул ID нового события")
            return int(cur.lastrowid)

    async def record_battle(
        self,
        *,
        telegram_message_id,
        session_id,
        target_name,
        result,
        xp=0,
        dust=0,
        items=(),
        position=None,
    ) -> tuple[bool, list[str]]:
        cards: list[str] = []
        async with self.lock:
            if self.connection.execute(
                "SELECT 1 FROM battles WHERE telegram_message_id=?",
                (telegram_message_id,),
            ).fetchone():
                return False, cards
            px, py = position if position else (None, None)
            cur = self.connection.execute(
                """
                INSERT INTO battles(
                    telegram_message_id,session_id,happened_at,target_name,
                    result,xp,dust,position_x,position_y
                ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
                (
                    telegram_message_id,
                    session_id,
                    utc_now(),
                    target_name,
                    result,
                    xp,
                    dust,
                    px,
                    py,
                ),
            )
            if cur.lastrowid is None:
                raise RuntimeError("SQLite не вернул ID нового боя")
            battle_id = int(cur.lastrowid)
            for item in items:
                item_name, quantity = parse_item_stack(str(item))
                n = item_name.casefold()
                is_card = int(
                    n.startswith("карта ") or n.startswith("🃏карта ") or n.startswith("🃏 карта ")
                )
                if is_card:
                    cards.append(item_name)
                self.connection.execute(
                    "INSERT INTO drops(battle_id,item_name,quantity,is_card) VALUES (?,?,?,?)",
                    (battle_id, item_name, quantity, is_card),
                )
            if session_id is not None:
                self.connection.execute(
                    """
                    UPDATE sessions SET wins=wins+?, defeats=defeats+?,
                    xp=xp+?, dust=dust+? WHERE id=?
                """,
                    (
                        int(result == "VICTORY"),
                        int(result == "DEFEAT"),
                        xp,
                        dust,
                        session_id,
                    ),
                )
            self.connection.commit()
            return True, cards

    async def get_current_session(self) -> SessionSummary:
        async with self.lock:
            row = self.connection.execute("""
                SELECT s.* FROM sessions s JOIN farmer_state f
                ON f.session_id=s.id WHERE f.singleton=1
            """).fetchone()
            if not row:
                return SessionSummary(None, None, None, "STOPPED", 0, 0, 0, 0, 0)
            return SessionSummary(
                row["id"],
                row["started_at"],
                row["ended_at"],
                row["status"],
                row["wins"],
                row["defeats"],
                row["xp"],
                row["dust"],
                row["runtime_seconds"],
            )

    async def get_drops(self, session_id=None) -> list[dict]:
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
            return [dict(r) for r in self.connection.execute(query, params).fetchall()]

    async def get_events(self, limit=20) -> list[dict]:
        async with self.lock:
            rows = self.connection.execute(
                """
                SELECT created_at,level,event_type,message FROM events
                ORDER BY id DESC LIMIT ?
            """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    async def get_statistics_dashboard(self) -> dict:
        session = await self.get_current_session()
        async with self.lock:
            sid = session.session_id
            if sid is None:
                battle = {"battles": 0, "wins": 0, "defeats": 0, "xp": 0, "dust": 0}
                drops = {"items": 0, "cards": 0}
                targets = []
            else:
                row = self.connection.execute(
                    """SELECT COUNT(*) battles,
                    SUM(result='VICTORY') wins, SUM(result='DEFEAT') defeats,
                    COALESCE(SUM(xp),0) xp, COALESCE(SUM(dust),0) dust
                    FROM battles WHERE session_id=?""",
                    (sid,),
                ).fetchone()
                battle = dict(row)
                row = self.connection.execute(
                    """SELECT COALESCE(SUM(d.quantity),0) items,
                    COALESCE(SUM(CASE WHEN d.is_card=1 THEN d.quantity ELSE 0 END),0) cards
                    FROM drops d JOIN battles b ON b.id=d.battle_id WHERE b.session_id=?""",
                    (sid,),
                ).fetchone()
                drops = dict(row)
                targets = [
                    dict(r)
                    for r in self.connection.execute(
                        """SELECT target_name, COUNT(*) battles,
                    SUM(result='VICTORY') wins, COALESCE(SUM(xp),0) xp,
                    COALESCE(SUM(dust),0) dust FROM battles
                    WHERE session_id=? GROUP BY target_name
                    ORDER BY wins DESC, battles DESC, target_name""",
                        (sid,),
                    ).fetchall()
                ]
            state = self.connection.execute(
                "SELECT moves,current_cycle,cycles_count,moves_in_cycle,"
                "moves_per_cycle FROM farmer_state WHERE singleton=1"
            ).fetchone()
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
            "state": dict(state or {}),
            "runtime_seconds": runtime,
        }

    @staticmethod
    def format_statistics_text(data: dict) -> str:
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
            f"🎁 Предметов: {d.get('items', 0)}\n"
            f"🃏 Карт: {d.get('cards', 0)}\n"
            f"👣 Ходов: {st.get('moves', 0)}"
        )
