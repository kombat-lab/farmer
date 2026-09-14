from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from battle_records import BattleOutcome, ItemDrop, RewardBundle, SourceEventId
from combat_knowledge_namespace import LEGACY_COMBAT_KNOWLEDGE_NAMESPACE
from combat_strategy import (
    COMBAT_MODEL_VERSION,
    CombatDecision,
    CombatMemory,
    SkillTarget,
    build_decision_trace,
)
from legacy_combat_diagnostics import LEGACY_TRACE_SCHEMA_VERSION, LegacyCombatDiagnostics
from storage import Storage


def legacy_trace(**fields: object) -> dict[str, object]:
    return {
        "model_version": COMBAT_MODEL_VERSION,
        "telegram_message_id": 1,
        "player": {"current_hp": 400, "max_hp": 500},
        "decision": {"skill_name": "атака аколита", "target": "enemy"},
        **fields,
    }


class LedgerDependencyTests(unittest.TestCase):
    def test_core_ledger_operates_without_legacy_parser_or_analytics_imports(self) -> None:
        script = """
import asyncio
import importlib.abc
import sys
from pathlib import Path

class BlockLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname in {
            'combat_learning', 'combat_strategy', 'legacy_combat_diagnostics',
            'legacy_battle_rewards', 'rewards', 'farmer', 'telethon',
        }:
            raise AssertionError('core imported legacy module: ' + fullname)

sys.meta_path.insert(0, BlockLegacy())
from battle_records import BattleOutcome, RewardBundle, SourceEventId
from storage import Storage

async def check():
    store = Storage(Path(':memory:'))
    result = await store.record_battle_outcome(BattleOutcome(
        source_event_id=SourceEventId('test:core:1'), source_message_id=1,
        session_id=None, target_name='target',
        result='VICTORY', rewards=RewardBundle(xp=4),
    ))
    assert result.inserted
    assert store.connection.execute('SELECT xp FROM battles').fetchone()[0] == 4
    await store.cleanup_old_data()
    await store.close()

asyncio.run(check())
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class LegacyDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = Storage(Path(":memory:"))
        self.diagnostics = LegacyCombatDiagnostics(self.store)
        self.outcome = BattleOutcome(
            source_event_id=SourceEventId("test:diagnostics:2"),
            source_message_id=2,
            session_id=None,
            target_name="Моль",
            result="VICTORY",
            rewards=RewardBundle(xp=4, items=(ItemDrop("Карта Моль", is_card=True),)),
        )

    async def asyncTearDown(self) -> None:
        await self.store.close()

    def table_names(self) -> set[str]:
        return {str(row[0]) for row in self.store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}

    async def test_generic_retention_only_deletes_explicit_event_types(self) -> None:
        await self.store.add_event("LOW_HP_WAIT_STARTED", "current")
        await self.store.add_event("WATCHDOG_TRIGGERED", "current")
        self.assertEqual((await self.store.cleanup_old_data())["events"], 0)
        counts = await self.store.cleanup_old_data(
            event_types_to_delete=("LOW_HP_WAIT_STARTED", "'); DROP TABLE battles; --"),
        )
        self.assertEqual(counts["events"], 1)
        self.assertIn("battles", self.table_names())
        self.assertNotIn("combat_decisions", self.table_names())

    async def test_legacy_cleanup_preserves_unowned_tables(self) -> None:
        self.store.connection.executescript("""
            CREATE TABLE combat_policy_stats(id INTEGER PRIMARY KEY);
            CREATE TABLE combat_strategy_stats(id INTEGER PRIMARY KEY);
        """)
        await self.store.cleanup_old_data()
        self.assertIn("combat_policy_stats", self.table_names())
        self.assertIn("combat_strategy_stats", self.table_names())
        await self.diagnostics.cleanup()
        self.assertIn("combat_policy_stats", self.table_names())
        self.assertIn("combat_strategy_stats", self.table_names())

    async def test_optional_schema_is_owned_and_initialized_by_adapter(self) -> None:
        self.assertNotIn("combat_decisions", self.table_names())
        self.assertNotIn("combat_battle_analysis", self.table_names())
        await self.store.record_battle_outcome(self.outcome)
        self.assertNotIn("combat_decisions", self.table_names())
        await self.diagnostics.initialize()
        await self.diagnostics.initialize()
        self.assertIn("combat_decisions", self.table_names())
        self.assertIn("combat_battle_analysis", self.table_names())
        self.assertFalse(self.store.connection.in_transaction)

    async def test_optional_schema_creation_rolls_back_as_a_unit(self) -> None:
        def deny_analysis(action: int, name: str | None, *unused: object) -> int:
            if action == sqlite3.SQLITE_CREATE_TABLE and name == "combat_battle_analysis":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.store.connection.set_authorizer(deny_analysis)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                await self.diagnostics.initialize()
        finally:
            self.store.connection.set_authorizer(None)
        self.assertNotIn("combat_decisions", self.table_names())
        self.assertNotIn("combat_battle_analysis", self.table_names())
        self.assertFalse(self.store.connection.in_transaction)
        await self.diagnostics.initialize()
        self.assertIn("combat_decisions", self.table_names())

    async def test_typed_service_records_actual_decision_trace(self) -> None:
        trace = build_decision_trace(
            created_at=self.outcome.happened_at.isoformat(), telegram_message_id=1,
            memory=CombatMemory(target_name="Моль"), round_state=None,
            current_hp=400, max_hp=500,
            decision=CombatDecision("атака аколита", SkillTarget.ENEMY, "test"),
        )
        result = await self.diagnostics.record_battle(self.outcome, decisions=(trace,))
        self.assertTrue(result.inserted)
        rows = await self.diagnostics.get_decisions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trace"]["decision"]["skill_name"], "атака аколита")
        self.assertEqual(len(await self.diagnostics.learning_stats()), 1)

    async def test_primary_ledger_rejects_diagnostics_argument(self) -> None:
        with self.assertRaises(TypeError):
            await self.store.record_battle_outcome(self.outcome, combat_decisions=())

    async def test_diagnostics_initialization_failure_cannot_rollback_rewards(self) -> None:
        with patch.object(self.diagnostics, "initialize", side_effect=RuntimeError("DDL failed")):
            with self.assertLogs("fog_farmer", level="ERROR"):
                result = await self.diagnostics.record_payloads(
                    self.outcome, combat_decisions=(legacy_trace(),),
                )
        self.assertTrue(result.inserted)
        self.assertEqual(result.cards, ("Карта Моль",))
        self.assertEqual(self.store.connection.execute("SELECT xp FROM battles").fetchone()[0], 4)
        self.assertFalse(self.store.connection.in_transaction)

    async def test_duplicate_outcome_skips_optional_work_and_does_not_repeat_cards(self) -> None:
        first = await self.diagnostics.record_payloads(
            self.outcome, combat_decisions=(legacy_trace(),),
        )
        with patch.object(self.diagnostics, "_persist", side_effect=AssertionError("duplicate")):
            duplicate = await self.diagnostics.record_payloads(
                self.outcome, combat_decisions=(legacy_trace(),),
            )
        self.assertEqual(first.cards, ("Карта Моль",))
        self.assertFalse(duplicate.inserted)
        self.assertEqual(duplicate.cards, ())
        self.assertEqual(duplicate.battle_id, first.battle_id)
        rows = await self.diagnostics.get_decisions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trace"]["ruleset_namespace"], LEGACY_COMBAT_KNOWLEDGE_NAMESPACE)
        self.assertEqual(rows[0]["trace"]["trace_schema_version"], LEGACY_TRACE_SCHEMA_VERSION)

    async def test_incompatible_payloads_do_not_enter_legacy_schema(self) -> None:
        for message_id, fields in enumerate((
            {"ruleset_namespace": "future-rules:1"},
            {"trace_schema_version": LEGACY_TRACE_SCHEMA_VERSION + 1},
            {"trace_schema_version": True},
            {"model_version": COMBAT_MODEL_VERSION + 1},
        ), start=10):
            with self.subTest(fields=fields):
                with self.assertLogs("fog_farmer", level="WARNING"):
                    recorded = await self.diagnostics.record_payloads(
                        replace(
                            self.outcome,
                            source_event_id=SourceEventId(
                                f"test:diagnostics:{message_id}"
                            ),
                            source_message_id=message_id,
                        ),
                        combat_decisions=(legacy_trace(**fields),),
                    )
                self.assertTrue(recorded.inserted)
        self.assertEqual(await self.diagnostics.get_decisions(), [])

    async def test_backfill_and_cleanup_preserve_foreign_or_future_trace_payloads(self) -> None:
        await self.diagnostics.record_payloads(
            self.outcome,
            combat_decisions=(legacy_trace(model_version=COMBAT_MODEL_VERSION - 1),),
        )
        for fields in (
            {"ruleset_namespace": "future-rules:1", "model_version": 1},
            {"trace_schema_version": LEGACY_TRACE_SCHEMA_VERSION + 1, "model_version": 1},
            {"model_version": COMBAT_MODEL_VERSION + 1},
        ):
            with self.subTest(fields=fields):
                supported = json.dumps(legacy_trace(model_version=COMBAT_MODEL_VERSION - 1))
                self.store.connection.execute(
                    "UPDATE combat_decisions SET trace_json=?", (supported,)
                )
                self.store.connection.commit()
                await self.diagnostics.backfill()
                raw = json.dumps(legacy_trace(**fields))
                self.store.connection.execute("UPDATE combat_decisions SET trace_json=?", (raw,))
                self.store.connection.commit()
                self.assertEqual(await self.diagnostics.cleanup(), 0)
                self.assertEqual(await self.diagnostics.get_decisions(), [])
                self.store.connection.execute("DELETE FROM combat_battle_analysis")
                self.store.connection.commit()
                self.assertEqual(await self.diagnostics.backfill(), 0)
                self.assertEqual(self.store.connection.execute(
                    "SELECT trace_json FROM combat_decisions"
                ).fetchone()[0], raw)

    async def test_cleanup_requires_both_supported_trace_and_existing_projection(self) -> None:
        with patch("legacy_combat_diagnostics.battle_learning_summary", side_effect=ValueError):
            with self.assertLogs("fog_farmer", level="ERROR"):
                await self.diagnostics.record_payloads(
                    self.outcome,
                    combat_decisions=(legacy_trace(model_version=COMBAT_MODEL_VERSION - 1),),
                )
        self.assertEqual(await self.diagnostics.cleanup(), 0)
        self.assertEqual(await self.diagnostics.backfill(), 1)
        self.assertEqual(await self.diagnostics.cleanup(), 1)
        self.assertEqual(await self.diagnostics.get_decisions(), [])
        self.assertEqual(len(await self.diagnostics.learning_stats()), 1)


if __name__ == "__main__":
    unittest.main()
