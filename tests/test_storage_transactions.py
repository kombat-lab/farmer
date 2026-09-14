from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from battle_outbox import BattleEvent
from battle_records import BattleOutcome, ItemDrop, RewardBundle, SourceEventId
from storage import Storage


class StorageTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.storage = Storage(Path(":memory:"))

    async def asyncTearDown(self) -> None:
        await self.storage.close()

    def install_trigger(
        self,
        name: str,
        *,
        timing: str,
        operation: str,
        table: str,
        failure: str = "FAIL",
        when: str = "",
    ) -> None:
        self.storage.connection.executescript(
            f"""
            CREATE TRIGGER {name} {timing} {operation} ON {table}
            {when}
            BEGIN
                SELECT RAISE({failure}, 'injected transaction failure');
            END;
            """
        )

    def drop_trigger(self, name: str) -> None:
        self.storage.connection.execute(f"DROP TRIGGER {name}")
        self.storage.connection.commit()

    async def assert_clean_and_commit_unrelated_write(self, marker: str) -> None:
        self.assertFalse(self.storage.connection.in_transaction)
        await self.storage.set_setting("transaction_probe", marker)
        self.assertEqual(await self.storage.get_setting("transaction_probe"), marker)
        self.assertFalse(self.storage.connection.in_transaction)

    async def test_start_session_failure_restores_abandoned_session_and_singleton(self) -> None:
        original_id = await self.storage.start_session(cycles_count=2, moves_per_cycle=15)
        original_state = await self.storage.get_state()
        self.install_trigger(
            "fail_start_state", timing="AFTER", operation="UPDATE", table="farmer_state"
        )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected transaction failure"):
            await self.storage.start_session(cycles_count=3, moves_per_cycle=25)

        sessions = self.storage.connection.execute(
            "SELECT id,status,ended_at FROM sessions ORDER BY id"
        ).fetchall()
        self.assertEqual([tuple(row) for row in sessions], [(original_id, "RUNNING", None)])
        self.assertEqual(await self.storage.get_state(), original_state)
        self.drop_trigger("fail_start_state")
        await self.assert_clean_and_commit_unrelated_write("start")
        self.assertEqual(
            self.storage.connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1
        )

    async def test_finish_session_failure_restores_session_and_singleton(self) -> None:
        session_id = await self.storage.start_session(cycles_count=1, moves_per_cycle=10)
        original_state = await self.storage.get_state()
        self.install_trigger(
            "rollback_finish_state",
            timing="AFTER",
            operation="UPDATE",
            table="farmer_state",
            failure="ROLLBACK",
        )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected transaction failure"):
            await self.storage.finish_session(session_id, "done", 123)

        session = self.storage.connection.execute(
            "SELECT status,ended_at,stop_reason,runtime_seconds FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        self.assertEqual(tuple(session), ("RUNNING", None, None, 0))
        self.assertEqual(await self.storage.get_state(), original_state)
        self.drop_trigger("rollback_finish_state")
        await self.assert_clean_and_commit_unrelated_write("finish")
        self.assertEqual(
            self.storage.connection.execute(
                "SELECT status FROM sessions WHERE id=?", (session_id,)
            ).fetchone()[0],
            "RUNNING",
        )

    async def test_cleanup_late_failure_restores_all_earlier_deletions(self) -> None:
        old = datetime.now(UTC) - timedelta(days=120)
        event = BattleEvent.from_payload(
            namespace="test-consumer",
            idempotency_key="notify",
            event_type="battle-recorded",
            schema_version=1,
            payload={"message_id": 101},
        )
        outcome = BattleOutcome(
            source_event_id=SourceEventId("test:transactions:101"),
            source_message_id=101,
            session_id=None,
            target_name="Moth",
            result="VICTORY",
            rewards=RewardBundle(
                xp=5,
                dust=3,
                crystals=2,
                items=(ItemDrop("Card", quantity=2, is_card=True),),
            ),
            happened_at=old,
        )
        await self.storage.record_battle_outcome(outcome, events=(event,))
        self.storage.connection.execute(
            "INSERT INTO events(created_at,level,event_type,message) VALUES (?,?,?,?)",
            (old.isoformat(), "INFO", "OLD", "old event"),
        )
        self.storage.connection.execute(
            "INSERT INTO telegram_activity_hourly(bucket_start,outgoing_total) VALUES (?,?)",
            (old.replace(minute=0, second=0, microsecond=0).isoformat(), 1),
        )
        self.storage.connection.commit()
        self.install_trigger(
            "fail_late_cleanup",
            timing="AFTER",
            operation="DELETE",
            table="telegram_activity_hourly",
        )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected transaction failure"):
            await self.storage.cleanup_old_data(retention_days=7)

        for table in (
            "events",
            "battles",
            "drops",
            "battle_currencies",
            "battle_outbox",
            "telegram_activity_hourly",
        ):
            with self.subTest(table=table):
                self.assertEqual(
                    self.storage.connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0],
                    1,
                )
        self.drop_trigger("fail_late_cleanup")
        await self.assert_clean_and_commit_unrelated_write("cleanup")
        self.assertEqual(
            self.storage.connection.execute("SELECT COUNT(*) FROM battles").fetchone()[0], 1
        )

    async def test_telegram_activity_insert_and_update_failures_roll_back(self) -> None:
        bucket = "2026-09-14T10:00:00+00:00"
        self.install_trigger(
            "fail_activity_insert",
            timing="AFTER",
            operation="INSERT",
            table="telegram_activity_hourly",
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected transaction failure"):
            await self.storage.increment_telegram_activity(bucket, {"outgoing_total": 2})
        self.assertEqual(
            self.storage.connection.execute(
                "SELECT COUNT(*) FROM telegram_activity_hourly"
            ).fetchone()[0],
            0,
        )
        self.drop_trigger("fail_activity_insert")

        await self.storage.increment_telegram_activity(bucket, {"outgoing_total": 2})
        self.install_trigger(
            "rollback_activity_update",
            timing="AFTER",
            operation="UPDATE",
            table="telegram_activity_hourly",
            failure="ROLLBACK",
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected transaction failure"):
            await self.storage.increment_telegram_activity(bucket, {"outgoing_total": 7})
        self.assertEqual(
            self.storage.connection.execute(
                "SELECT outgoing_total FROM telegram_activity_hourly"
            ).fetchone()[0],
            2,
        )
        self.drop_trigger("rollback_activity_update")
        await self.assert_clean_and_commit_unrelated_write("activity")
        self.assertEqual(
            self.storage.connection.execute(
                "SELECT outgoing_total FROM telegram_activity_hourly"
            ).fetchone()[0],
            2,
        )

    async def test_state_and_event_writes_roll_back_after_trigger_failures(self) -> None:
        original_state = await self.storage.get_state()
        self.install_trigger(
            "fail_state_update", timing="AFTER", operation="UPDATE", table="farmer_state"
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected transaction failure"):
            await self.storage.update_state(moves=99, last_action="partial")
        self.assertEqual(await self.storage.get_state(), original_state)
        self.drop_trigger("fail_state_update")

        self.install_trigger(
            "rollback_event_insert",
            timing="AFTER",
            operation="INSERT",
            table="events",
            failure="ROLLBACK",
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected transaction failure"):
            await self.storage.add_event("PARTIAL", "must roll back")
        self.assertEqual(
            self.storage.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0
        )
        self.drop_trigger("rollback_event_insert")
        await self.assert_clean_and_commit_unrelated_write("state-event")
        self.assertEqual(await self.storage.get_state(), original_state)

    async def test_multirow_setting_insert_and_delete_failures_are_atomic(self) -> None:
        self.install_trigger(
            "fail_second_setting",
            timing="AFTER",
            operation="INSERT",
            table="settings",
            when="WHEN NEW.key='second'",
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected transaction failure"):
            await self.storage.set_settings({"first": 1, "second": 2})
        self.assertEqual(await self.storage.get_settings(), {})
        self.drop_trigger("fail_second_setting")

        await self.storage.set_settings({"first": 1, "second": 2})
        self.install_trigger(
            "rollback_setting_delete",
            timing="AFTER",
            operation="DELETE",
            table="settings",
            failure="ROLLBACK",
            when="WHEN OLD.key='second'",
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected transaction failure"):
            await self.storage.delete_settings({"first", "second"})
        self.assertEqual(await self.storage.get_settings(), {"first": 1, "second": 2})
        self.drop_trigger("rollback_setting_delete")
        await self.assert_clean_and_commit_unrelated_write("settings")
        self.assertEqual(await self.storage.get_setting("first"), 1)
        self.assertEqual(await self.storage.get_setting("second"), 2)

    async def test_all_map_obstacle_mutations_roll_back(self) -> None:
        self.install_trigger(
            "fail_obstacle_insert",
            timing="AFTER",
            operation="INSERT",
            table="map_obstacles",
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected transaction failure"):
            await self.storage.remember_map_obstacle("map", (1, 1))
        self.assertEqual(await self.storage.get_map_obstacles("map"), set())
        self.drop_trigger("fail_obstacle_insert")

        for position in ((1, 1), (2, 2), (3, 3)):
            await self.storage.remember_map_obstacle("map", position)
        self.install_trigger(
            "rollback_obstacle_delete",
            timing="AFTER",
            operation="DELETE",
            table="map_obstacles",
            failure="ROLLBACK",
            when="WHEN OLD.position_x=2",
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected transaction failure"):
            await self.storage.forget_map_obstacles("map", {(1, 1), (2, 2)})
        self.assertEqual(
            await self.storage.get_map_obstacles("map"), {(1, 1), (2, 2), (3, 3)}
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected transaction failure"):
            await self.storage.clear_map_obstacles()
        self.assertEqual(
            await self.storage.get_map_obstacles("map"), {(1, 1), (2, 2), (3, 3)}
        )
        self.drop_trigger("rollback_obstacle_delete")
        await self.assert_clean_and_commit_unrelated_write("obstacles")

    async def test_battle_and_outbox_failure_rolls_back_rewards_and_session_totals(self) -> None:
        session_id = await self.storage.start_session(cycles_count=1, moves_per_cycle=10)
        event = BattleEvent.from_payload(
            namespace="notifications",
            idempotency_key="card",
            event_type="card-drop",
            schema_version=1,
            payload={"card": "Moth"},
        )
        outcome = BattleOutcome(
            source_event_id=SourceEventId("test:transactions:501"),
            source_message_id=501,
            session_id=session_id,
            target_name="Moth",
            result="VICTORY",
            rewards=RewardBundle(
                xp=8,
                dust=4,
                crystals=2,
                items=(ItemDrop("Moth card", is_card=True),),
            ),
        )
        self.install_trigger(
            "fail_outbox_insert",
            timing="AFTER",
            operation="INSERT",
            table="battle_outbox",
        )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected transaction failure"):
            await self.storage.record_battle_outcome(outcome, events=(event,))

        for table in ("battles", "drops", "battle_currencies", "battle_outbox"):
            with self.subTest(table=table):
                self.assertEqual(
                    self.storage.connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0],
                    0,
                )
        session = await self.storage.get_current_session()
        self.assertEqual((session.wins, session.xp, session.dust), (0, 0, 0))
        self.drop_trigger("fail_outbox_insert")
        await self.assert_clean_and_commit_unrelated_write("battle")
        self.assertEqual(
            self.storage.connection.execute("SELECT COUNT(*) FROM battles").fetchone()[0], 0
        )

    async def test_duplicate_battle_outbox_enqueue_failure_does_not_touch_ledger(self) -> None:
        outcome = BattleOutcome(
            source_event_id=SourceEventId("test:transactions:601"),
            source_message_id=601,
            session_id=None,
            target_name="Moth",
            result="VICTORY",
            rewards=RewardBundle(xp=5),
        )
        first = await self.storage.record_battle_outcome(outcome)
        event = BattleEvent.from_payload(
            namespace="diagnostics",
            idempotency_key="legacy",
            event_type="battle-diagnostics",
            schema_version=1,
            payload={"message_id": 601},
        )
        self.install_trigger(
            "rollback_duplicate_outbox",
            timing="AFTER",
            operation="INSERT",
            table="battle_outbox",
            failure="ROLLBACK",
        )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected transaction failure"):
            await self.storage.record_battle_outcome(outcome, events=(event,))

        row = self.storage.connection.execute(
            "SELECT id,xp FROM battles WHERE source_message_id=601"
        ).fetchone()
        self.assertEqual(tuple(row), (first.battle_id, 5))
        self.assertEqual(
            self.storage.connection.execute("SELECT COUNT(*) FROM battle_outbox").fetchone()[0], 0
        )
        self.drop_trigger("rollback_duplicate_outbox")
        await self.assert_clean_and_commit_unrelated_write("duplicate-outbox")

    async def test_outbox_ack_failure_keeps_event_pending(self) -> None:
        event = BattleEvent.from_payload(
            namespace="notifications",
            idempotency_key="card",
            event_type="card-drop",
            schema_version=1,
            payload={"card": "Moth"},
        )
        result = await self.storage.record_battle_outcome(
            BattleOutcome(
                SourceEventId("test:transactions:701"),
                701,
                None,
                "Moth",
                "VICTORY",
            ),
            events=(event,),
        )
        pending = await self.storage.pending_battle_events(namespace="notifications")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].battle_id, result.battle_id)
        self.install_trigger(
            "fail_outbox_ack",
            timing="AFTER",
            operation="UPDATE",
            table="battle_outbox",
        )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected transaction failure"):
            await self.storage.ack_battle_event(pending[0].id, namespace="notifications")

        self.assertEqual(
            len(await self.storage.pending_battle_events(namespace="notifications")), 1
        )
        self.drop_trigger("fail_outbox_ack")
        await self.assert_clean_and_commit_unrelated_write("outbox-ack")

    async def test_combat_knowledge_update_failure_preserves_previous_json(self) -> None:
        await self.storage.save_combat_knowledge(
            500, {"sample": 1}, namespace="rules-v1"
        )
        self.install_trigger(
            "fail_knowledge_update",
            timing="AFTER",
            operation="UPDATE",
            table="combat_knowledge",
        )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected transaction failure"):
            await self.storage.save_combat_knowledge(
                500, {"sample": 2}, namespace="rules-v1"
            )

        row = self.storage.connection.execute(
            "SELECT knowledge_json FROM combat_knowledge WHERE namespace=? AND profile_max_hp=?",
            ("rules-v1", 500),
        ).fetchone()
        self.assertEqual(json.loads(row[0]), {"sample": 1})
        self.drop_trigger("fail_knowledge_update")
        await self.assert_clean_and_commit_unrelated_write("knowledge")
        self.assertEqual(
            await self.storage.load_combat_knowledge(namespace="rules-v1"),
            {500: {"sample": 1}},
        )


if __name__ == "__main__":
    unittest.main()
