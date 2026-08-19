from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from combat_learning import battle_learning_summary, resolved_decision
from rewards import parse_item_stack


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class SessionSummary:
    session_id: int | None
    started_at: str | None
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

        CREATE TABLE IF NOT EXISTS combat_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            battle_id INTEGER NOT NULL,
            sequence_number INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            telegram_message_id INTEGER NOT NULL,
            target_name TEXT NOT NULL,
            round_number INTEGER,
            chosen_skill TEXT NOT NULL,
            chosen_target TEXT NOT NULL,
            reason TEXT NOT NULL,
            urgent INTEGER NOT NULL DEFAULT 0,
            trace_json TEXT NOT NULL,
            UNIQUE(battle_id, sequence_number),
            FOREIGN KEY(battle_id) REFERENCES battles(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_combat_decisions_target
            ON combat_decisions(target_name, chosen_skill);

        CREATE TABLE IF NOT EXISTS combat_knowledge (
            profile_max_hp INTEGER PRIMARY KEY,
            updated_at TEXT NOT NULL,
            knowledge_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS combat_battle_analysis (
            battle_id INTEGER PRIMARY KEY,
            target_name TEXT NOT NULL,
            result TEXT NOT NULL,
            happened_at TEXT NOT NULL,
            profile_max_hp INTEGER NOT NULL DEFAULT 0,
            model_version INTEGER NOT NULL DEFAULT 0,
            rounds INTEGER NOT NULL DEFAULT 0,
            total_actions INTEGER NOT NULL DEFAULT 0,
            offensive_actions INTEGER NOT NULL DEFAULT 0,
            self_heals INTEGER NOT NULL DEFAULT 0,
            renewals INTEGER NOT NULL DEFAULT 0,
            minimum_hp INTEGER,
            minimum_hp_percent REAL,
            last_decision_hp INTEGER,
            minimum_mana INTEGER,
            last_decision_mana INTEGER,
            effective_self_healing INTEGER NOT NULL DEFAULT 0,
            lost_healing_potential INTEGER NOT NULL DEFAULT 0,
            dangerous_turns INTEGER NOT NULL DEFAULT 0,
            shadow_decisions INTEGER NOT NULL DEFAULT 0,
            shadow_confident INTEGER NOT NULL DEFAULT 0,
            shadow_agreements INTEGER NOT NULL DEFAULT 0,
            policy_key TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_combat_analysis_profile_target
            ON combat_battle_analysis(profile_max_hp, target_name);

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

    async def delete_settings(self, keys: set[str] | frozenset[str]) -> int:
        if not keys:
            return 0
        placeholders = ",".join("?" for _ in keys)
        async with self.lock:
            cursor = self.connection.execute(
                f"DELETE FROM settings WHERE key IN ({placeholders})",
                tuple(sorted(keys)),
            )
            self.connection.commit()
            return max(0, cursor.rowcount)

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

    async def forget_map_obstacles(
        self,
        location_name: str,
        positions: set[tuple[int, int]],
    ) -> int:
        if not positions:
            return 0
        async with self.lock:
            deleted = self.connection.executemany(
                """
                DELETE FROM map_obstacles
                WHERE location_name=? AND position_x=? AND position_y=?
                """,
                [(location_name, x, y) for x, y in positions],
            ).rowcount
            self.connection.commit()
            return max(0, deleted)

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

    def _write_battle_analysis(
        self,
        *,
        battle_id: int,
        target_name: str,
        result: str,
        happened_at: str,
        traces: list[dict[str, Any]],
    ) -> None:
        if not traces:
            return
        summary = battle_learning_summary(traces)
        self.connection.execute(
            """
            INSERT INTO combat_battle_analysis(
                battle_id,target_name,result,happened_at,profile_max_hp,
                model_version,rounds,total_actions,offensive_actions,self_heals,
                renewals,minimum_hp,minimum_hp_percent,last_decision_hp,
                minimum_mana,last_decision_mana,effective_self_healing,
                lost_healing_potential,dangerous_turns,shadow_decisions,
                shadow_confident,shadow_agreements,policy_key,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(battle_id) DO NOTHING
            """,
            (
                battle_id,
                target_name,
                result,
                happened_at,
                summary.profile_max_hp,
                summary.model_version,
                summary.rounds,
                summary.total_actions,
                summary.offensive_actions,
                summary.self_heals,
                summary.renewals,
                summary.minimum_hp,
                summary.minimum_hp_percent,
                summary.last_decision_hp,
                summary.minimum_mana,
                summary.last_decision_mana,
                summary.effective_self_healing,
                summary.lost_healing_potential,
                summary.dangerous_turns,
                summary.shadow_decisions,
                summary.shadow_confident,
                summary.shadow_agreements,
                summary.policy_key,
                utc_now(),
            ),
        )

    async def backfill_combat_battle_analysis(self) -> int:
        """Builds compact learning rows from retained decision traces."""
        async with self.lock:
            battles = self.connection.execute(
                """
                SELECT b.id,b.target_name,b.result,b.happened_at
                FROM battles b
                LEFT JOIN combat_battle_analysis a ON a.battle_id=b.id
                WHERE a.battle_id IS NULL
                  AND EXISTS(
                      SELECT 1 FROM combat_decisions cd WHERE cd.battle_id=b.id
                  )
                ORDER BY b.id
                """
            ).fetchall()
            written = 0
            for battle in battles:
                rows = self.connection.execute(
                    """
                    SELECT trace_json FROM combat_decisions
                    WHERE battle_id=? ORDER BY sequence_number
                    """,
                    (int(battle["id"]),),
                ).fetchall()
                traces: list[dict[str, Any]] = []
                for row in rows:
                    try:
                        trace = json.loads(str(row["trace_json"]))
                    except json.JSONDecodeError:
                        continue
                    if isinstance(trace, dict):
                        traces.append(trace)
                if not traces:
                    continue
                self._write_battle_analysis(
                    battle_id=int(battle["id"]),
                    target_name=str(battle["target_name"]),
                    result=str(battle["result"]),
                    happened_at=str(battle["happened_at"]),
                    traces=traces,
                )
                written += 1
            if written:
                self.connection.commit()
            return written

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
        combat_decisions: tuple[dict[str, Any], ...] = (),
    ) -> tuple[bool, list[str]]:
        cards: list[str] = []
        async with self.lock:
            if self.connection.execute(
                "SELECT 1 FROM battles WHERE telegram_message_id=?",
                (telegram_message_id,),
            ).fetchone():
                return False, cards
            px, py = position if position else (None, None)
            happened_at = utc_now()
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
                    happened_at,
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
            for sequence_number, trace in enumerate(combat_decisions, start=1):
                decision_data = resolved_decision(trace)
                self.connection.execute(
                    """
                    INSERT INTO combat_decisions(
                        battle_id,sequence_number,created_at,telegram_message_id,
                        target_name,round_number,chosen_skill,chosen_target,
                        reason,urgent,trace_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        battle_id,
                        sequence_number,
                        str(trace.get("created_at") or utc_now()),
                        int(trace.get("telegram_message_id") or 0),
                        str(trace.get("target_name") or target_name),
                        trace.get("round_number"),
                        str(decision_data.get("skill_name") or "неизвестно"),
                        str(decision_data.get("target") or "unknown"),
                        str(decision_data.get("reason") or ""),
                        int(bool(decision_data.get("urgent"))),
                        json.dumps(trace, ensure_ascii=False),
                    ),
                )
            if combat_decisions:
                trace_list = list(combat_decisions)
                self._write_battle_analysis(
                    battle_id=battle_id,
                    target_name=target_name,
                    result=result,
                    happened_at=happened_at,
                    traces=trace_list,
                )
            for item in items:
                item_name, quantity = parse_item_stack(str(item))
                n = item_name.casefold()
                is_card = int(n.startswith(("карта ", "🃏карта ", "🃏 карта ")))
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

    async def get_combat_decisions(self, target_name: str | None = None) -> list[dict]:
        query = """
            SELECT cd.*, b.result
            FROM combat_decisions cd
            JOIN battles b ON b.id=cd.battle_id
        """
        params: tuple = ()
        if target_name is not None:
            query += " WHERE cd.target_name=?"
            params = (target_name,)
        query += " ORDER BY cd.id"

        async with self.lock:
            rows = self.connection.execute(query, params).fetchall()
            result: list[dict] = []
            for row in rows:
                item = dict(row)
                with suppress(json.JSONDecodeError):
                    item["trace"] = json.loads(str(item.pop("trace_json")))
                result.append(item)
            return result

    async def get_confirmed_treatment_targets(self) -> set[str]:
        """Finds monsters that have actually taken damage from Treatment."""
        async with self.lock:
            rows = self.connection.execute(
                "SELECT target_name,trace_json FROM combat_decisions"
            ).fetchall()

        confirmed: set[str] = set()
        for row in rows:
            try:
                trace = json.loads(str(row["trace_json"]))
            except json.JSONDecodeError:
                continue
            if not isinstance(trace, dict):
                continue
            outgoing = trace.get("outgoing_damage")
            if not isinstance(outgoing, list):
                continue
            if any(
                isinstance(estimate, dict)
                and str(estimate.get("skill_name", "")).casefold() == "лечение"
                and isinstance(samples := estimate.get("samples"), (int, float))
                and samples > 0
                for estimate in outgoing
            ):
                target = str(row["target_name"]).strip()
                if target:
                    confirmed.add(target)
        return confirmed

    async def get_combat_learning_stats(
        self,
        *,
        target_name: str | None = None,
        profile_max_hp: int | None = None,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if target_name is not None:
            conditions.append("a.target_name=?")
            params.append(target_name)
        if profile_max_hp is not None:
            conditions.append("a.profile_max_hp=?")
            params.append(profile_max_hp)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""
            SELECT a.*
            FROM combat_battle_analysis a
            {where}
            ORDER BY a.happened_at,a.battle_id
        """
        async with self.lock:
            return [
                dict(row)
                for row in self.connection.execute(query, tuple(params)).fetchall()
            ]

    async def get_combat_learning_overview(
        self,
        *,
        target_name: str | None = None,
        profile_max_hp: int | None = None,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if target_name is not None:
            conditions.append("a.target_name=?")
            params.append(target_name)
        if profile_max_hp is not None:
            conditions.append("a.profile_max_hp=?")
            params.append(profile_max_hp)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""
            SELECT a.profile_max_hp,a.target_name,a.policy_key,
                   COUNT(*) AS battles,
                   SUM(CASE WHEN a.result='VICTORY' THEN 1 ELSE 0 END) AS victories,
                   SUM(CASE WHEN a.result='DEFEAT' THEN 1 ELSE 0 END) AS defeats,
                   AVG(a.rounds) AS average_rounds,
                   MIN(CASE WHEN a.result='VICTORY' THEN a.rounds END)
                       AS best_victory_rounds,
                   AVG(a.minimum_hp_percent) AS average_minimum_hp_percent,
                   MIN(a.minimum_hp_percent) AS minimum_hp_percent,
                   CASE WHEN SUM(a.total_actions)>0
                       THEN CAST(SUM(a.offensive_actions) AS REAL)
                            / SUM(a.total_actions)
                       ELSE 0 END AS offensive_ratio,
                   SUM(a.self_heals) AS self_heals,
                   SUM(a.renewals) AS renewals,
                   SUM(a.lost_healing_potential) AS lost_healing_potential,
                   SUM(a.dangerous_turns) AS dangerous_turns,
                   SUM(a.shadow_confident) AS shadow_confident,
                   SUM(a.shadow_agreements) AS shadow_agreements,
                   CASE WHEN SUM(a.shadow_confident)>0
                       THEN CAST(SUM(a.shadow_agreements) AS REAL)
                            / SUM(a.shadow_confident)
                       ELSE NULL END AS shadow_agreement_rate
            FROM combat_battle_analysis a
            {where}
            GROUP BY a.profile_max_hp,a.target_name,a.policy_key
            ORDER BY victories DESC,average_rounds ASC,battles DESC
        """
        async with self.lock:
            return [
                dict(row)
                for row in self.connection.execute(query, tuple(params)).fetchall()
            ]

    async def load_combat_knowledge(self) -> dict[int, dict[str, Any]]:
        async with self.lock:
            rows = self.connection.execute(
                "SELECT profile_max_hp,knowledge_json FROM combat_knowledge"
            ).fetchall()

        result: dict[int, dict[str, Any]] = {}
        for row in rows:
            try:
                payload = json.loads(str(row["knowledge_json"]))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                result[int(row["profile_max_hp"])] = payload
        return result

    async def save_combat_knowledge(
        self,
        profile_max_hp: int,
        payload: dict[str, Any],
    ) -> None:
        if profile_max_hp <= 0:
            return
        async with self.lock:
            self.connection.execute(
                """
                INSERT INTO combat_knowledge(profile_max_hp,updated_at,knowledge_json)
                VALUES (?,?,?)
                ON CONFLICT(profile_max_hp) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    knowledge_json=excluded.knowledge_json
                """,
                (
                    profile_max_hp,
                    utc_now(),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            self.connection.commit()

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
