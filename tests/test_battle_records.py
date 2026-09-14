from __future__ import annotations

import sqlite3
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from battle_records import (
    BattleOutcome,
    IdempotencyConflict,
    ItemDrop,
    RecordBattleResult,
    RewardBundle,
    SourceEventId,
)
from combat_learning import battle_learning_summary
from combat_strategy import COMBAT_KNOWLEDGE_SCHEMA_VERSION, RecentCombatKnowledge
from legacy_battle_rewards import reward_bundle_from_reward
from legacy_combat_diagnostics import LegacyCombatDiagnostics
from message_snapshot import legacy_v4_message_source_event_id
from rewards import BattleReward
from storage import Storage
from tests.storage_fixtures import record_legacy_battle


def decision_trace() -> dict[str, object]:
    return {
        "model_version": 5,
        "telegram_message_id": 100,
        "round_number": 2,
        "player": {"current_hp": 400, "max_hp": 500},
        "decision": {"skill_name": "лечение", "target": "enemy", "reason": "test"},
        "outcome": {"target": "self", "effect": "healing", "amount": 100},
    }


class BattleRecordValueTests(unittest.TestCase):
    def test_rewards_are_normalized_before_persistence(self) -> None:
        raw = BattleReward(dust=4, xp=12, crystals=2, items=("Осколок x3", "🃏 Карта Моль ×2"))
        rewards = reward_bundle_from_reward(raw)
        self.assertEqual(rewards, RewardBundle(
            xp=12, dust=4, crystals=2,
            items=(ItemDrop("Осколок", 3), ItemDrop("🃏 Карта Моль", 2, True)),
        ))
        self.assertEqual(raw.items, ("Осколок x3", "🃏 Карта Моль ×2"))

    def test_record_values_are_immutable(self) -> None:
        item = ItemDrop("Карта Моль", is_card=True)
        reward = RewardBundle(items=(item,))
        outcome = BattleOutcome(
            source_event_id=SourceEventId("test:battle:1"),
            source_message_id=1,
            session_id=None,
            target_name="Моль",
            result="VICTORY",
            rewards=reward,
        )
        result = RecordBattleResult(inserted=True, battle_id=1, cards=(item.name,))
        for instance, field, value in (
            (item, "quantity", 2), (reward, "xp", 1),
            (outcome, "target_name", "other"), (result, "inserted", False),
        ):
            with self.subTest(value=instance):
                with self.assertRaises(FrozenInstanceError):
                    setattr(instance, field, value)

    def test_invalid_rewards_are_rejected_before_sql(self) -> None:
        with self.assertRaises(ValueError):
            RewardBundle(xp=-1)
        with self.assertRaises(ValueError):
            ItemDrop("Осколок", quantity=0)
        with self.assertRaises(ValueError):
            BattleOutcome(
                source_event_id=SourceEventId("test:battle:1"),
                source_message_id=1,
                session_id=None,
                target_name="Моль",
                result="VICTORY",
                happened_at=datetime(2026, 9, 14),
            )

    def test_outcome_rejects_invalid_ids_names_positions_and_reward_types(self) -> None:
        outcome = BattleOutcome(
            source_event_id=SourceEventId("test:battle:1"),
            source_message_id=1,
            session_id=None,
            target_name="Моль",
            result="VICTORY",
        )
        invalid_fields = (
            {"source_event_id": "test:battle:1"}, {"source_event_id": None},
            {"source_message_id": True}, {"source_message_id": 0},
            {"source_message_id": -1}, {"session_id": True}, {"session_id": 0},
            {"session_id": -1}, {"target_name": " \t\n"}, {"target_name": None},
            {"position": ()}, {"position": (1,)}, {"position": (1, 2, 3)},
            {"position": (True, 1)}, {"position": (1.0, 2)}, {"position": ("1", 2)},
            {"position": "12"}, {"rewards": {}},
        )
        for fields in invalid_fields:
            with self.subTest(fields=fields):
                with self.assertRaises(ValueError):
                    replace(outcome, **fields)

    def test_items_and_rewards_reject_bool_quantities_and_non_bool_card_flags(self) -> None:
        for fields in ({"quantity": True}, {"quantity": -1}, {"name": " \t"},
                       {"is_card": 1}, {"is_card": "false"}):
            with self.subTest(fields=fields):
                with self.assertRaises(ValueError):
                    replace(ItemDrop("Моль"), **fields)
        for fields in ({"xp": True}, {"dust": False}, {"crystals": True}):
            with self.subTest(fields=fields):
                with self.assertRaises(ValueError):
                    RewardBundle(**fields)

    def test_names_are_trimmed_and_legacy_position_lists_become_immutable(self) -> None:
        coordinates = [-3, 0]
        outcome = BattleOutcome(
            source_event_id=SourceEventId("test:battle:1"),
            source_message_id=1,
            session_id=2,
            target_name="  Моль \n",
            result="VICTORY",
            position=coordinates,
        )
        coordinates[0] = 99
        self.assertEqual(outcome.target_name, "Моль")
        self.assertEqual(outcome.position, (-3, 0))
        self.assertEqual(ItemDrop(" \tКарта  Моль \n", is_card=True).name, "Карта  Моль")

    def test_source_event_id_is_strict_bounded_and_opaque(self) -> None:
        self.assertEqual(SourceEventId("src1:opaque").value, "src1:opaque")
        for value in ("", " leading", "trailing ", "line\nbreak", "x" * 256, 1, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SourceEventId(value)  # type: ignore[arg-type]

    def test_combat_knowledge_accepts_current_and_versionless_legacy_schema(self) -> None:
        knowledge = RecentCombatKnowledge()
        knowledge.add_incoming("Моль", 50)
        payload = knowledge.as_payload()
        self.assertEqual(payload["version"], COMBAT_KNOWLEDGE_SCHEMA_VERSION)
        self.assertEqual(RecentCombatKnowledge.from_payload(payload).incoming, knowledge.incoming)
        payload.pop("version")
        self.assertEqual(RecentCombatKnowledge.from_payload(payload).incoming, knowledge.incoming)

    def test_unknown_knowledge_versions_do_not_leak_old_samples(self) -> None:
        for version in (0, COMBAT_KNOWLEDGE_SCHEMA_VERSION + 1, "1", True, None):
            with self.subTest(version=version):
                payload = {"version": version, "incoming": {"моль": [999]},
                           "treatment_enemy_targets": ["моль"]}
                knowledge = RecentCombatKnowledge.from_payload(payload)
                self.assertEqual(knowledge.incoming, {})
                self.assertEqual(knowledge.treatment_enemy_targets, set())


class BattleLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.storage = Storage(Path(":memory:"))
        self.diagnostics = LegacyCombatDiagnostics(self.storage)
        await self.diagnostics.initialize()
        self.session_id = await self.storage.start_session(cycles_count=1, moves_per_cycle=10)
        self.outcome = BattleOutcome(
            source_event_id=SourceEventId("test:battle:101:revision:1"),
            source_message_id=101,
            session_id=self.session_id,
            target_name="Моль",
            result="VICTORY",
            rewards=RewardBundle(xp=12, dust=4, crystals=2,
                                 items=(ItemDrop("Карта Моль", 2, True),)),
        )

    async def asyncTearDown(self) -> None:
        await self.storage.close()

    async def assert_mandatory_outcome(self) -> None:
        stats = await self.storage.get_statistics_dashboard()
        self.assertEqual(stats["battle"], {
            "battles": 1, "wins": 1, "defeats": 0, "xp": 12, "dust": 4, "crystals": 2,
        })
        self.assertEqual(stats["drops"], {"items": 2, "cards": 2})
        self.assertEqual(stats["session"].wins, 1)
        self.assertEqual(stats["session"].xp, 12)

    async def test_normalized_api_is_idempotent_and_returns_new_cards_once(self) -> None:
        result = await self.storage.record_battle_outcome(self.outcome)
        duplicate = await self.storage.record_battle_outcome(self.outcome)
        self.assertTrue(result.inserted)
        self.assertEqual(result.cards, ("Карта Моль",))
        self.assertFalse(duplicate.inserted)
        self.assertEqual(duplicate.battle_id, result.battle_id)
        self.assertEqual(duplicate.cards, ())
        await self.assert_mandatory_outcome()

    async def test_same_source_id_with_different_facts_conflicts_atomically(self) -> None:
        first = await self.storage.record_battle_outcome(self.outcome)
        with self.assertRaises(IdempotencyConflict):
            await self.storage.record_battle_outcome(
                replace(self.outcome, result="DEFEAT", rewards=RewardBundle())
            )
        self.assertEqual(
            await self.storage.get_battle_outcome(first.battle_id),
            self.outcome,
        )
        await self.assert_mandatory_outcome()

    async def test_multiple_matching_alias_rows_are_rejected_even_when_exact_id_exists(
        self,
    ) -> None:
        await self.storage.record_battle_outcome(self.outcome)
        aliases = (
            SourceEventId("legacy-v4:test-alias-1"),
            SourceEventId("legacy-v4:test-alias-2"),
        )
        for alias in aliases:
            await self.storage.record_battle_outcome(
                replace(self.outcome, source_event_id=alias, session_id=None)
            )
        with self.assertRaisesRegex(IdempotencyConflict, "Multiple legacy battle rows"):
            await self.storage.record_battle_outcome(
                self.outcome,
                legacy_source_event_ids=aliases,
            )
        self.assertFalse(self.storage.connection.in_transaction)
        self.assertEqual(
            self.storage.connection.execute("SELECT COUNT(*) FROM battles").fetchone()[0],
            3,
        )

    async def test_distinct_source_revisions_may_reuse_the_same_message_id(self) -> None:
        first = await self.storage.record_battle_outcome(self.outcome)
        second = await self.storage.record_battle_outcome(
            replace(
                self.outcome,
                source_event_id=SourceEventId("test:battle:101:revision:2"),
            )
        )
        self.assertTrue(first.inserted)
        self.assertTrue(second.inserted)
        self.assertNotEqual(first.battle_id, second.battle_id)
        self.assertEqual(
            self.storage.connection.execute(
                "SELECT COUNT(*) FROM battles WHERE source_message_id=101"
            ).fetchone()[0],
            2,
        )
        session = await self.storage.get_current_session()
        self.assertEqual((session.wins, session.xp, session.dust), (2, 24, 8))

    async def test_legacy_alias_rekeys_equal_facts_but_not_a_new_outcome(self) -> None:
        legacy_id = legacy_v4_message_source_event_id(101)
        migrated = replace(self.outcome, source_event_id=legacy_id)
        first = await self.storage.record_battle_outcome(migrated)
        replay = await self.storage.record_battle_outcome(
            self.outcome,
            legacy_source_event_ids=(legacy_id,),
        )
        self.assertFalse(replay.inserted)
        self.assertEqual(replay.battle_id, first.battle_id)
        row = self.storage.connection.execute(
            "SELECT source_event_id FROM battles WHERE id=?", (first.battle_id,)
        ).fetchone()
        self.assertEqual(row["source_event_id"], self.outcome.source_event_id.value)

        different = replace(
            self.outcome,
            source_event_id=SourceEventId("test:battle:101:revision:2"),
            result="DEFEAT",
            rewards=RewardBundle(),
        )
        inserted = await self.storage.record_battle_outcome(
            different,
            legacy_source_event_ids=(legacy_id,),
        )
        self.assertTrue(inserted.inserted)
        self.assertNotEqual(inserted.battle_id, first.battle_id)

    async def test_legacy_reward_adapter_records_normalized_outcome(self) -> None:
        inserted, cards = await record_legacy_battle(self.storage,
            telegram_message_id=101, session_id=self.session_id, target_name="Моль",
            result="VICTORY", xp=12, dust=4, crystals=2, items=("Карта Моль x2",),
        )
        self.assertTrue(inserted)
        self.assertEqual(cards, ["Карта Моль"])
        await self.assert_mandatory_outcome()

    async def test_outcome_time_is_preserved_without_requiring_map_coordinates(self) -> None:
        moment = datetime(2026, 9, 14, 14, tzinfo=timezone(timedelta(hours=3)))
        outcome = BattleOutcome(
            source_event_id=SourceEventId("test:battle:8"),
            source_message_id=8,
            session_id=None,
            target_name="Моль",
            result="DEFEAT",
            happened_at=moment,
        )
        recorded = await self.storage.record_battle_outcome(outcome)
        row = self.storage.connection.execute(
            "SELECT happened_at,position_x,position_y FROM battles WHERE id=?",
            (recorded.battle_id,),
        ).fetchone()
        self.assertEqual(row["happened_at"], moment.astimezone(UTC).isoformat())
        self.assertIsNone(row["position_x"])
        self.assertIsNone(row["position_y"])

    async def test_analysis_computation_failure_preserves_facts_and_retryable_traces(self) -> None:
        def fail_analysis(_traces: object) -> None:
            self.assertFalse(self.storage.connection.in_transaction)
            raise RuntimeError("analysis math failed")

        with patch("legacy_combat_diagnostics.battle_learning_summary", side_effect=fail_analysis):
            with self.assertLogs("fog_farmer", level="ERROR"):
                await self.diagnostics.record_payloads(
                    self.outcome, combat_decisions=(decision_trace(),)
                )
        await self.assert_mandatory_outcome()
        self.assertEqual(len(await self.diagnostics.get_decisions()), 1)
        self.assertEqual(await self.diagnostics.learning_stats(), [])
        self.assertEqual(await self.diagnostics.backfill(), 1)
        self.assertEqual(await self.diagnostics.backfill(), 0)

    async def test_analysis_write_failure_rolls_back_only_projection(self) -> None:
        original_write = self.diagnostics._write_analysis

        def fail_after_partial_write(connection: sqlite3.Connection, **values: object) -> None:
            original_write(connection, **values)
            raise RuntimeError("failure after projection insert")

        with patch.object(
            self.diagnostics, "_write_analysis", side_effect=fail_after_partial_write
        ):
            with self.assertLogs("fog_farmer", level="ERROR"):
                recorded = await self.diagnostics.record_payloads(
                    self.outcome, combat_decisions=(decision_trace(),)
                )
        self.assertTrue(recorded.inserted)
        await self.assert_mandatory_outcome()
        self.assertEqual(len(await self.diagnostics.get_decisions()), 1)
        self.assertEqual(await self.diagnostics.learning_stats(), [])
        self.assertEqual(await self.diagnostics.backfill(), 1)

    async def test_mandatory_drop_failure_rolls_back_entire_outcome(self) -> None:
        self.storage.connection.executescript("""
            CREATE TRIGGER fail_drop BEFORE INSERT ON drops
            BEGIN SELECT RAISE(ABORT, 'mandatory drop failed'); END;
        """)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "mandatory drop failed"):
            await self.storage.record_battle_outcome(self.outcome)
        for table in ("battles", "drops", "battle_currencies", "combat_decisions"):
            self.assertEqual(self.storage.connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0], 0)
        self.assertEqual((await self.storage.get_current_session()).wins, 0)
        self.assertFalse(self.storage.connection.in_transaction)
        self.storage.connection.execute("DROP TRIGGER fail_drop")
        self.assertTrue((await self.storage.record_battle_outcome(self.outcome)).inserted)
        await self.assert_mandatory_outcome()

    async def test_bad_trace_rows_cannot_block_mandatory_outcome(self) -> None:
        self.storage.connection.executescript("""
            CREATE TRIGGER fail_trace BEFORE INSERT ON combat_decisions
            BEGIN SELECT RAISE(ABORT, 'optional trace failed'); END;
        """)
        with self.assertLogs("fog_farmer", level="ERROR"):
            await self.diagnostics.record_payloads(
                self.outcome, combat_decisions=(decision_trace(),)
            )
        await self.assert_mandatory_outcome()
        self.assertEqual(await self.diagnostics.get_decisions(), [])
        self.assertEqual(await self.diagnostics.learning_stats(), [])

    async def test_sqlite_transaction_rollback_in_trace_cannot_erase_mandatory_ledger(self) -> None:
        self.storage.connection.executescript("""
            CREATE TRIGGER rollback_trace BEFORE INSERT ON combat_decisions
            BEGIN SELECT RAISE(ROLLBACK, 'optional transaction rollback'); END;
        """)
        with self.assertLogs("fog_farmer", level="ERROR"):
            recorded = await self.diagnostics.record_payloads(
                self.outcome, combat_decisions=(decision_trace(),)
            )
        self.assertTrue(recorded.inserted)
        await self.assert_mandatory_outcome()
        self.assertEqual(await self.diagnostics.get_decisions(), [])
        self.assertFalse(self.storage.connection.in_transaction)

    async def test_analysis_rollback_preserves_ledger_and_committed_traces(self) -> None:
        self.storage.connection.executescript("""
            CREATE TRIGGER rollback_analysis BEFORE INSERT ON combat_battle_analysis
            BEGIN SELECT RAISE(ROLLBACK, 'optional analysis rollback'); END;
        """)
        with self.assertLogs("fog_farmer", level="ERROR"):
            await self.diagnostics.record_payloads(
                self.outcome, combat_decisions=(decision_trace(),)
            )
        await self.assert_mandatory_outcome()
        self.assertEqual(len(await self.diagnostics.get_decisions()), 1)
        self.assertEqual(await self.diagnostics.learning_stats(), [])
        self.storage.connection.execute("DROP TRIGGER rollback_analysis")
        self.assertEqual(await self.diagnostics.backfill(), 1)

    async def test_silently_ignored_mandatory_drop_rejects_the_whole_outcome(self) -> None:
        self.storage.connection.executescript("""
            CREATE TRIGGER ignore_drop BEFORE INSERT ON drops
            BEGIN SELECT RAISE(IGNORE); END;
        """)
        with self.assertRaisesRegex(RuntimeError, "обязательный предмет"):
            await self.storage.record_battle_outcome(self.outcome)
        self.assertEqual(self.storage.connection.execute(
            "SELECT COUNT(*) FROM battles"
        ).fetchone()[0], 0)
        self.assertEqual(self.storage.connection.execute(
            "SELECT COUNT(*) FROM battle_currencies"
        ).fetchone()[0], 0)
        self.assertEqual((await self.storage.get_current_session()).wins, 0)
        self.assertFalse(self.storage.connection.in_transaction)

    async def test_backfill_computes_each_summary_outside_transaction(self) -> None:
        await self.diagnostics.record_payloads(self.outcome, combat_decisions=(decision_trace(),))
        self.storage.connection.execute("DELETE FROM combat_battle_analysis")
        self.storage.connection.commit()

        def checked_summary(traces: list[dict[str, object]]) -> object:
            self.assertFalse(self.storage.connection.in_transaction)
            return battle_learning_summary(traces)

        with patch(
            "legacy_combat_diagnostics.battle_learning_summary", side_effect=checked_summary
        ):
            self.assertEqual(await self.diagnostics.backfill(), 1)


if __name__ == "__main__":
    unittest.main()
