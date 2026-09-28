from __future__ import annotations

import asyncio
import random
import sqlite3
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from combat_knowledge_namespace import LEGACY_COMBAT_KNOWLEDGE_NAMESPACE
from combat_strategy import (
    COMBAT_MODEL_VERSION,
    CombatMemory,
    RecentCombatKnowledge,
)
from config import (
    DEFAULT_BATTLE_START_HP_PERCENT,
    DEFAULT_COMBAT_PLANNER_MODE,
    DEFAULT_HEAL_THRESHOLD,
    DEFAULT_MOVES_PER_CYCLE_MAX,
    DEFAULT_MOVES_PER_CYCLE_MIN,
)
from farmer import Farmer
from game_mechanisms import CycleDescriptor
from human_delays import ActivityBreakPlanner
from legacy_combat_diagnostics import LegacyCombatDiagnostics
from models import BotState, RuntimeContext
from rewards import BattleReward
from settings_service import SettingsService
from storage import SCHEMA_VERSION, Storage
from tests.fog_test_fixtures import make_offline_farmer
from tests.legacy_fog_factory import legacy_runtime
from tests.storage_fixtures import record_legacy_battle


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_schema_is_versioned_and_indexed(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.sqlite3")
            version = int(storage.connection.execute("PRAGMA user_version").fetchone()[0])
            indexes = {
                str(row[0])
                for row in storage.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }

            self.assertEqual(version, SCHEMA_VERSION)
            self.assertTrue(
                {
                    "idx_battles_happened_at",
                    "idx_battles_session_id",
                    "idx_drops_battle_id",
                    "idx_events_created_at",
                    "idx_sessions_status_ended_at",
                }.issubset(indexes)
            )
            await storage.close()

    async def test_new_database_uses_current_schema_directly(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.sqlite3")
            columns = {
                str(row["name"])
                for row in storage.connection.execute("PRAGMA table_info(farmer_state)").fetchall()
            }

            self.assertTrue(
                {
                    "current_cycle",
                    "cycles_count",
                    "moves_in_cycle",
                    "moves_per_cycle",
                    "rest_until",
                    "pause_requested",
                }.issubset(columns)
            )
            self.assertNotIn("max_moves", columns)
            event_columns = {
                str(row["name"])
                for row in storage.connection.execute("PRAGMA table_info(events)").fetchall()
            }
            self.assertNotIn("notified", event_columns)
            tables = {
                str(row["name"])
                for row in storage.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertNotIn("combat_decisions", tables)
            self.assertIn("combat_knowledge", tables)
            self.assertNotIn("combat_battle_analysis", tables)
            self.assertIn("battle_currencies", tables)
            self.assertIn("telegram_activity_hourly", tables)
            self.assertNotIn("combat_strategy_stats", tables)
            self.assertNotIn("combat_policy_stats", tables)
            await storage.close()

    async def test_telegram_activity_batches_sum_counts_and_keep_peak_max(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.sqlite3")
            await storage.increment_telegram_activity(
                "2026-08-26T10:00:00+00:00",
                {
                    "outgoing_total": 7,
                    "inline_callbacks": 6,
                    "peak_actions_1m": 4,
                },
            )
            await storage.increment_telegram_activity(
                "2026-08-26T10:00:00+00:00",
                {
                    "outgoing_total": 2,
                    "manual_restriction_marks": 1,
                    "peak_actions_1m": 3,
                },
            )
            await storage.increment_telegram_activity(
                "2026-08-26T11:00:00+00:00",
                {"incoming_message_edits": 12, "silent_stalls": 1},
            )

            with patch("storage.datetime") as clock:
                clock.now.return_value = datetime(2026, 8, 26, tzinfo=UTC)
                days = await storage.get_telegram_activity_daily(days=1)

            self.assertEqual(len(days), 1)
            self.assertEqual(days[0]["outgoing_total"], 9)
            self.assertEqual(days[0]["peak_actions_1m"], 4)
            self.assertEqual(days[0]["incoming_message_edits"], 12)
            self.assertEqual(days[0]["manual_restriction_marks"], 1)
            self.assertEqual(days[0]["silent_stalls"], 1)
            await storage.close()

    async def test_legacy_cleanup_preserves_unowned_obsolete_tables(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "test.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE combat_strategy_stats(id INTEGER PRIMARY KEY);
                CREATE TABLE combat_policy_stats(id INTEGER PRIMARY KEY);
                """
            )
            connection.close()

            storage = Storage(path)
            await LegacyCombatDiagnostics(storage).cleanup()
            tables = {
                str(row["name"])
                for row in storage.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }

            self.assertIn("combat_strategy_stats", tables)
            self.assertIn("combat_policy_stats", tables)
            await storage.close()

    async def test_cleanup_keeps_current_traces_and_compact_analysis(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.sqlite3")
            session_id = await storage.start_session(cycles_count=1, moves_per_cycle=10)

            def trace(version: int, message_id: int) -> dict[str, object]:
                return {
                    "model_version": version,
                    "created_at": "2026-08-22T10:00:00+00:00",
                    "telegram_message_id": message_id,
                    "target_name": "Пепельник",
                    "round_number": 1,
                    "player": {"current_hp": 700, "max_hp": 830},
                    "mana": {"current": 13, "maximum": 13},
                    "decision": {
                        "skill_name": "атака аколита",
                        "target": "enemy",
                        "reason": "test",
                        "urgent": False,
                    },
                    "outcome": {
                        "target": "enemy",
                        "effect": "damage",
                        "amount": 42,
                    },
                }

            await record_legacy_battle(
                storage,
                telegram_message_id=100,
                session_id=session_id,
                target_name="Пепельник",
                result="VICTORY",
                combat_decisions=(trace(COMBAT_MODEL_VERSION - 1, 101),),
            )
            await record_legacy_battle(
                storage,
                telegram_message_id=200,
                session_id=session_id,
                target_name="Пепельник",
                result="VICTORY",
                combat_decisions=(trace(COMBAT_MODEL_VERSION, 201),),
            )
            await storage.add_event("LOW_HP_WAIT_STARTED", "noise")
            await storage.add_event("WATCHDOG_TRIGGERED", "keep")

            deleted_decisions = await LegacyCombatDiagnostics(storage).cleanup()
            cleanup = await storage.cleanup_old_data(
                retention_days=3650,
                event_types_to_delete=("LOW_HP_WAIT_STARTED", "LOW_HP_WAIT_FINISHED"),
            )

            versions = [
                int(row[0])
                for row in storage.connection.execute(
                    "SELECT json_extract(trace_json, '$.model_version') FROM combat_decisions"
                ).fetchall()
            ]
            self.assertEqual(versions, [COMBAT_MODEL_VERSION])
            self.assertEqual(deleted_decisions, 1)
            self.assertEqual(cleanup["events"], 1)
            self.assertEqual(
                storage.connection.execute(
                    "SELECT COUNT(*) FROM combat_battle_analysis"
                ).fetchone()[0],
                2,
            )
            await storage.close()

    async def test_compaction_reclaims_pages_after_bulk_cleanup(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.sqlite3")
            payload = {"padding": "x" * 2000}
            for index in range(400):
                await storage.add_event(
                    "LOW_HP_WAIT_STARTED",
                    f"noise {index}",
                    payload=payload,
                )
            await storage.cleanup_old_data(
                retention_days=3650,
                event_types_to_delete=("LOW_HP_WAIT_STARTED", "LOW_HP_WAIT_FINISHED"),
            )
            free_before = int(storage.connection.execute("PRAGMA freelist_count").fetchone()[0])

            compacted = await storage.compact_if_needed(
                min_free_pages=1,
                min_free_ratio=0,
            )

            self.assertGreater(free_before, 0)
            self.assertTrue(compacted)
            self.assertEqual(
                storage.connection.execute("PRAGMA freelist_count").fetchone()[0],
                0,
            )
            await storage.close()

    async def test_combat_knowledge_survives_restart_by_character_profile(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "test.sqlite3"
            knowledge = RecentCombatKnowledge()
            knowledge.add_incoming("Фонарщик", 61)
            knowledge.add_incoming("Фонарщик", 66, critical=True)
            knowledge.add_outgoing("Фонарщик", "лечение", 94)
            knowledge.add_direct_healing(124)
            knowledge.add_renewal_healing(40)
            knowledge.confirm_treatment_enemy("Фонарщик")

            storage = Storage(path)
            await storage.save_combat_knowledge(
                780, knowledge.as_payload(), namespace=LEGACY_COMBAT_KNOWLEDGE_NAMESPACE
            )
            await storage.close()

            reopened = Storage(path)
            profiles = await reopened.load_combat_knowledge(
                namespace=LEGACY_COMBAT_KNOWLEDGE_NAMESPACE
            )
            restored = RecentCombatKnowledge.from_payload(profiles[780])
            memory = CombatMemory(target_name="Фонарщик", knowledge=restored)
            restored.load_into(memory)

            self.assertEqual(memory.incoming_damage.minimum, 61)
            self.assertEqual(memory.critical_incoming_damage.minimum, 66)
            self.assertEqual(memory.damage_floor("лечение"), 0)
            self.assertEqual(memory.direct_heal(), 124)
            self.assertEqual(memory.renewal_tick(), 40)
            self.assertTrue(memory.treatment_can_target_enemy())
            await reopened.close()

    async def test_learned_map_obstacles_survive_restart(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "test.sqlite3"
            storage = Storage(path)
            self.assertTrue(await storage.remember_map_obstacle("Выжженное поле", (4, 7)))
            self.assertFalse(await storage.remember_map_obstacle("Выжженное поле", (4, 7)))
            await storage.close()

            reopened = Storage(path)
            self.assertEqual(
                await reopened.get_map_obstacles("Выжженное поле"),
                {(4, 7)},
            )
            await reopened.close()

    async def test_inconsistent_map_obstacles_can_be_forgotten(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.sqlite3")
            await storage.remember_map_obstacle("Мертвый лес", (5, 3))
            await storage.remember_map_obstacle("Мертвый лес", (5, 4))

            deleted = await storage.forget_map_obstacles(
                "Мертвый лес",
                {(5, 3), (5, 4)},
            )

            self.assertEqual(deleted, 2)
            self.assertEqual(await storage.get_map_obstacles("Мертвый лес"), set())
            await storage.close()

    async def test_unknown_setting_is_preserved_without_extra_writes(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.sqlite3")
            await storage.set_setting("removed_setting", 610)
            settings = SettingsService(storage)

            await settings.load()

            self.assertEqual(settings.values.heal_threshold, DEFAULT_HEAL_THRESHOLD)
            self.assertEqual(
                settings.values.battle_start_hp_percent,
                DEFAULT_BATTLE_START_HP_PERCENT,
            )
            self.assertEqual((await storage.get_settings())["removed_setting"], 610)
            changes_after_first_load = storage.connection.total_changes
            await settings.load()
            self.assertEqual(storage.connection.total_changes, changes_after_first_load)
            await storage.close()

    async def test_legacy_move_count_becomes_configurable_range(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.sqlite3")
            await storage.set_setting("moves_per_cycle", 100)
            settings = SettingsService(storage)

            await settings.load()

            self.assertEqual(settings.values.moves_per_cycle_min, 80)
            self.assertEqual(settings.values.moves_per_cycle_max, 120)
            stored = await storage.get_settings()
            self.assertNotIn("moves_per_cycle", stored)
            self.assertEqual(stored["moves_per_cycle_min"], 80)
            self.assertEqual(stored["moves_per_cycle_max"], 120)
            await storage.close()

    async def test_move_range_is_validated_and_persisted(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "test.sqlite3"
            storage = Storage(path)
            settings = SettingsService(storage)
            await settings.load()

            self.assertEqual(
                (
                    settings.values.moves_per_cycle_min,
                    settings.values.moves_per_cycle_max,
                ),
                (DEFAULT_MOVES_PER_CYCLE_MIN, DEFAULT_MOVES_PER_CYCLE_MAX),
            )
            self.assertEqual(settings.parse_moves_range("91–127"), (91, 127))
            self.assertEqual(settings.parse_moves_range("91 127"), (91, 127))
            with self.assertRaises(ValueError):
                settings.parse_moves_range("127 91")
            with self.assertRaises(ValueError):
                settings.parse_moves_range("-1 127")

            await settings.set_moves_range(91, 127)
            await storage.close()

            reopened = Storage(path)
            loaded = SettingsService(reopened)
            await loaded.load()
            self.assertEqual(loaded.values.moves_per_cycle_min, 91)
            self.assertEqual(loaded.values.moves_per_cycle_max, 127)
            await reopened.close()

    async def test_combat_planner_mode_is_safe_and_persisted(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "test.sqlite3"
            storage = Storage(path)
            await storage.set_setting("combat_planner_mode", "unsupported")
            settings = SettingsService(storage)

            await settings.load()

            self.assertEqual(
                settings.values.combat_planner_mode,
                DEFAULT_COMBAT_PLANNER_MODE,
            )
            self.assertEqual(await settings.cycle_combat_planner_mode(), "guarded")
            self.assertEqual(await settings.cycle_combat_planner_mode(), "active")
            await storage.close()

            reopened = Storage(path)
            loaded = SettingsService(reopened)
            await loaded.load()
            self.assertEqual(loaded.values.combat_planner_mode, "active")
            self.assertEqual(await loaded.cycle_combat_planner_mode(), "shadow")
            await reopened.close()

    async def test_runtime_control_setting_is_not_removed_by_ui_settings(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.sqlite3")
            await storage.set_setting("farmer_stop_requested", True)
            await storage.set_setting(
                "telegram_cooldown_until",
                "2026-08-25T00:30:00+00:00",
            )
            await storage.set_setting("telegram_cooldown_reason", "test")

            await SettingsService(storage).load()

            self.assertTrue(await storage.get_setting("farmer_stop_requested"))
            self.assertEqual(
                await storage.get_setting("telegram_cooldown_until"),
                "2026-08-25T00:30:00+00:00",
            )
            self.assertEqual(await storage.get_setting("telegram_cooldown_reason"), "test")
            await storage.close()

    async def test_removed_activity_profile_is_cleaned_from_settings(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "test.sqlite3"
            storage = Storage(path)
            await storage.set_setting("activity_profile", "fast")

            settings = SettingsService(storage)
            await settings.load()

            self.assertFalse(hasattr(settings.values, "activity_profile"))
            self.assertNotIn("activity_profile", await storage.get_settings())
            await storage.close()

    async def test_treatment_enemy_targets_are_persisted_without_duplicates(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "test.sqlite3"
            storage = Storage(path)
            settings = SettingsService(storage)
            await settings.load()
            self.assertTrue(await settings.add_treatment_enemy_target("Костяной заяц"))
            self.assertFalse(await settings.add_treatment_enemy_target("костяной заяц"))
            await storage.close()

            reopened = Storage(path)
            loaded = SettingsService(reopened)
            await loaded.load()
            self.assertEqual(
                loaded.values.treatment_enemy_targets,
                ("Костяной заяц",),
            )
            self.assertTrue(await loaded.remove_treatment_enemy_target("КОСТЯНОЙ ЗАЯЦ"))
            self.assertEqual(loaded.values.treatment_enemy_targets, ())
            await reopened.close()

    async def test_activity_break_resumes_with_one_state_refresh(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.sqlite3")
            farmer = Farmer.__new__(Farmer)
            farmer.running = True
            farmer.state = BotState.ACTIVITY_BREAK
            farmer.mechanisms = SimpleNamespace(
                snapshot=lambda: SimpleNamespace(cycle_progress_units=31)
            )
            farmer.activity_break_planner = ActivityBreakPlanner(random.Random(5))
            farmer.activity_break_task = None
            farmer.storage = storage
            farmer.mark_progress = lambda _reason: None
            farmer.log = lambda _message: None
            refreshes = 0

            async def count_refresh() -> None:
                nonlocal refreshes
                refreshes += 1

            farmer.process_latest_state = count_refresh

            await farmer.finish_activity_break(0)

            state = await storage.get_state()
            events = await storage.get_events()
            self.assertEqual(farmer.state, BotState.STARTING)
            self.assertEqual(refreshes, 1)
            self.assertIsNone(state["rest_until"])
            self.assertEqual(events[0]["event_type"], "ACTIVITY_BREAK_FINISHED")
            await storage.close()

    async def test_new_session_closes_abandoned_running_sessions(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.sqlite3")
            first = await storage.start_session(cycles_count=1, moves_per_cycle=10)
            second = await storage.start_session(cycles_count=1, moves_per_cycle=10)

            row = storage.connection.execute(
                "SELECT status,stop_reason FROM sessions WHERE id=?", (first,)
            ).fetchone()
            assert row is not None
            self.assertEqual(row["status"], "INTERRUPTED")
            self.assertIn("без корректной остановки", row["stop_reason"])
            self.assertNotEqual(first, second)
            await storage.close()

    async def test_record_battle_stores_stack_quantity(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.sqlite3")
            session_id = await storage.start_session(cycles_count=1, moves_per_cycle=10)
            await record_legacy_battle(
                storage,
                telegram_message_id=1,
                session_id=session_id,
                target_name="Цель",
                result="VICTORY",
                items=("Золотой хитин x3",),
            )

            drops = await storage.get_drops(session_id)
            self.assertEqual(drops[0]["item_name"], "Золотой хитин")
            self.assertEqual(drops[0]["quantity"], 3)
            await storage.close()

    async def test_record_battle_aggregates_crystals_as_currency(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.sqlite3")
            session_id = await storage.start_session(cycles_count=1, moves_per_cycle=10)
            await record_legacy_battle(
                storage,
                telegram_message_id=2,
                session_id=session_id,
                target_name="Цель",
                result="VICTORY",
                xp=14,
                dust=7,
                crystals=2,
            )

            dashboard = await storage.get_statistics_dashboard()
            self.assertEqual(dashboard["battle"]["crystals"], 2)
            self.assertEqual(dashboard["targets"][0]["crystals"], 2)
            self.assertEqual(await storage.get_drops(session_id), [])
            await storage.close()

    async def test_combat_decisions_are_linked_to_battle_result(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.sqlite3")
            session_id = await storage.start_session(cycles_count=1, moves_per_cycle=10)
            trace = {
                "created_at": "2026-08-15T20:00:00+00:00",
                "telegram_message_id": 10,
                "target_name": "Фонарщик",
                "round_number": 8,
                "decision": {
                    "skill_name": "Лечение",
                    "target": "enemy",
                    "reason": "добивание",
                    "urgent": True,
                },
            }
            await record_legacy_battle(
                storage,
                telegram_message_id=11,
                session_id=session_id,
                target_name="Фонарщик",
                result="VICTORY",
                combat_decisions=(trace,),
            )

            decisions = await LegacyCombatDiagnostics(storage).get_decisions("Фонарщик")
            self.assertEqual(len(decisions), 1)
            self.assertEqual(decisions[0]["result"], "VICTORY")
            self.assertEqual(decisions[0]["chosen_skill"], "Лечение")
            self.assertEqual(decisions[0]["trace"]["round_number"], 8)

            faster_trace = dict(trace)
            faster_trace["telegram_message_id"] = 12
            faster_trace["round_number"] = 6
            await record_legacy_battle(
                storage,
                telegram_message_id=13,
                session_id=session_id,
                target_name="Фонарщик",
                result="VICTORY",
                combat_decisions=(faster_trace,),
            )
            learning = await LegacyCombatDiagnostics(storage).learning_stats(target_name="Фонарщик")
            self.assertEqual([row["rounds"] for row in learning], [8, 6])
            await storage.close()

    async def test_actual_treatment_target_drives_saved_policy(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.sqlite3")
            session_id = await storage.start_session(cycles_count=1, moves_per_cycle=10)
            trace = {
                "model_version": 3,
                "created_at": "2026-08-16T09:31:17+00:00",
                "telegram_message_id": 20,
                "target_name": "Черная мушка",
                "round_number": 15,
                "decision": {
                    "skill_name": "лечение",
                    "target": "enemy",
                    "reason": "планировалась атака",
                    "urgent": False,
                },
                "outcome": {
                    "target": "self",
                    "effect": "healing",
                    "amount": 46,
                },
            }

            await record_legacy_battle(
                storage,
                telegram_message_id=21,
                session_id=session_id,
                target_name="Черная мушка",
                result="VICTORY",
                combat_decisions=(trace,),
            )

            decisions = await LegacyCombatDiagnostics(storage).get_decisions("Черная мушка")
            policies = await LegacyCombatDiagnostics(storage).learning_overview(
                target_name="Черная мушка"
            )
            self.assertEqual(decisions[0]["chosen_target"], "self")
            self.assertEqual(policies[0]["self_heals"], 1)
            self.assertEqual(policies[0]["offensive_ratio"], 0)
            await storage.close()

    async def test_profiled_battle_analysis_records_shadow_training_metrics(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.sqlite3")
            session_id = await storage.start_session(cycles_count=1, moves_per_cycle=10)
            traces = (
                {
                    "model_version": 4,
                    "created_at": "2026-08-19T20:00:00+00:00",
                    "telegram_message_id": 41,
                    "target_name": "Фонарщик",
                    "round_number": 4,
                    "player": {"current_hp": 600, "max_hp": 880},
                    "mana": {"current": 8, "maximum": 12},
                    "incoming_damage": {"worst_next_hit": 110},
                    "direct_heal_estimate": 135,
                    "decision": {
                        "skill_name": "лечение",
                        "target": "self",
                        "reason": "test",
                        "urgent": False,
                    },
                    "outcome": {
                        "target": "self",
                        "effect": "healing",
                        "amount": 100,
                    },
                    "shadow_plan": {
                        "confident": True,
                        "agrees": False,
                    },
                },
                {
                    "model_version": 4,
                    "created_at": "2026-08-19T20:00:10+00:00",
                    "telegram_message_id": 42,
                    "target_name": "Фонарщик",
                    "round_number": 5,
                    "player": {"current_hp": 120, "max_hp": 880},
                    "mana": {"current": 4, "maximum": 12},
                    "incoming_damage": {"worst_next_hit": 105},
                    "decision": {
                        "skill_name": "атака аколита",
                        "target": "enemy",
                        "reason": "test",
                        "urgent": False,
                    },
                    "outcome": {
                        "target": "enemy",
                        "effect": "damage",
                        "amount": 35,
                    },
                    "shadow_plan": {
                        "confident": True,
                        "agrees": True,
                    },
                },
            )
            await record_legacy_battle(
                storage,
                telegram_message_id=43,
                session_id=session_id,
                target_name="Фонарщик",
                result="VICTORY",
                combat_decisions=traces,
            )

            rows = await LegacyCombatDiagnostics(storage).learning_stats(
                target_name="Фонарщик",
                profile_max_hp=880,
            )
            self.assertEqual(len(rows), 1)
            analysis = rows[0]
            self.assertEqual(analysis["model_version"], 4)
            self.assertEqual(analysis["minimum_hp"], 120)
            self.assertAlmostEqual(analysis["minimum_hp_percent"], 12000 / 880)
            self.assertEqual(analysis["minimum_mana"], 4)
            self.assertEqual(analysis["lost_healing_potential"], 35)
            self.assertEqual(analysis["dangerous_turns"], 1)
            self.assertEqual(analysis["shadow_confident"], 2)
            self.assertEqual(analysis["shadow_agreements"], 1)
            overview = await LegacyCombatDiagnostics(storage).learning_overview(
                target_name="Фонарщик",
                profile_max_hp=880,
            )
            self.assertEqual(overview[0]["battles"], 1)
            self.assertEqual(overview[0]["shadow_agreement_rate"], 0.5)

            storage.connection.execute("DELETE FROM combat_battle_analysis")
            storage.connection.commit()
            self.assertEqual(await LegacyCombatDiagnostics(storage).backfill(), 1)
            self.assertEqual(await LegacyCombatDiagnostics(storage).backfill(), 0)
            storage.connection.execute("UPDATE battles SET happened_at='2020-01-01T00:00:00+00:00'")
            storage.connection.commit()
            await storage.cleanup_old_data(retention_days=1)
            self.assertEqual(
                storage.connection.execute("SELECT COUNT(*) FROM battles").fetchone()[0],
                0,
            )
            self.assertEqual(
                len(
                    await LegacyCombatDiagnostics(storage).learning_stats(
                        target_name="Фонарщик",
                        profile_max_hp=880,
                    )
                ),
                0,
            )
            await storage.close()

    async def test_unknown_settings_are_preserved_for_forward_compatibility(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.sqlite3")
            await storage.set_settings({"max_hp": 400, "max_mana": 11, "heal_amount": 141})
            settings = SettingsService(storage)

            await settings.load()

            stored = await storage.get_settings()
            self.assertEqual(stored["max_hp"], 400)
            self.assertEqual(stored["max_mana"], 11)
            self.assertEqual(stored["heal_amount"], 141)
            await storage.close()

    async def test_different_sequences_share_semantic_policy_stats(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.sqlite3")
            session_id = await storage.start_session(cycles_count=1, moves_per_cycle=10)

            def trace(round_number: int, skill: str, target: str) -> dict:
                return {
                    "created_at": "2026-08-15T20:00:00+00:00",
                    "telegram_message_id": round_number,
                    "target_name": "Фонарщик",
                    "round_number": round_number,
                    "decision": {
                        "skill_name": skill,
                        "target": target,
                        "reason": "test",
                        "urgent": False,
                    },
                }

            first = (
                trace(1, "Святое свечение", "enemy"),
                trace(2, "Атака аколита", "enemy"),
                trace(3, "Лечение", "self"),
                trace(4, "Обновление", "self"),
            )
            second = (first[1], first[0], first[3], first[2])
            await record_legacy_battle(
                storage,
                telegram_message_id=100,
                session_id=session_id,
                target_name="Фонарщик",
                result="VICTORY",
                combat_decisions=first,
            )
            await record_legacy_battle(
                storage,
                telegram_message_id=101,
                session_id=session_id,
                target_name="Фонарщик",
                result="VICTORY",
                combat_decisions=second,
            )

            policies = await LegacyCombatDiagnostics(storage).learning_overview(
                target_name="Фонарщик"
            )
            self.assertEqual(len(policies), 1)
            self.assertEqual(policies[0]["battles"], 2)
            self.assertEqual(policies[0]["offensive_ratio"], 0.5)
            await storage.close()

    async def test_stop_persists_final_movement_snapshot(self) -> None:
        class DisconnectedClient:
            async def disconnect(self) -> None:
                return None

            def is_connected(self) -> bool:
                return False

        cancelled = asyncio.Event()

        async def background_worker() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.sqlite3")
            session_id = await storage.start_session(cycles_count=1, moves_per_cycle=80)
            farmer = make_offline_farmer(storage, DisconnectedClient())
            farmer.state = BotState.MAP
            legacy = legacy_runtime(farmer)
            legacy.context = RuntimeContext(
                current_position=(4, 5),
                current_hp=780,
                max_hp=780,
                move_count=89,
            )
            legacy.moves_in_cycle = 80
            legacy._cycle = CycleDescriptor(80, 80, 80, "перемещений")
            await legacy.initialize()
            farmer._mechanisms_initialized = True
            farmer.session_id = session_id
            farmer.worker_task = farmer._start_background(
                background_worker(), name="test-final-snapshot-worker"
            )
            await asyncio.sleep(0)

            await farmer.stop("тестовая остановка")

            state = await storage.get_state()
            self.assertEqual(state["moves"], 89)
            self.assertEqual(state["moves_in_cycle"], 80)
            self.assertEqual(state["position_x"], 4)
            self.assertEqual(state["position_y"], 5)
            self.assertTrue(cancelled.is_set())
            await storage.close()

    async def test_record_battle_preserves_outcome_when_optional_analysis_fails(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.sqlite3")
            decision = {
                "model_version": COMBAT_MODEL_VERSION,
                "telegram_message_id": 501,
                "target_name": "Пепельник",
                "decision": {
                    "skill_name": "атака аколита",
                    "target": "enemy",
                    "reason": "test",
                    "urgent": False,
                },
            }

            with patch(
                "legacy_combat_diagnostics.LegacyCombatDiagnostics._write_analysis",
                side_effect=RuntimeError("analysis failed"),
            ):
                with self.assertLogs("fog_farmer", level="ERROR"):
                    inserted, _ = await record_legacy_battle(
                        storage,
                        telegram_message_id=500,
                        session_id=None,
                        target_name="Пепельник",
                        result="VICTORY",
                        combat_decisions=(decision,),
                    )

            self.assertTrue(inserted)
            self.assertEqual(
                storage.connection.execute("SELECT COUNT(*) FROM battles").fetchone()[0],
                1,
            )
            inserted, _ = await record_legacy_battle(
                storage,
                telegram_message_id=500,
                session_id=None,
                target_name="Пепельник",
                result="VICTORY",
            )
            self.assertFalse(inserted)
            await storage.close()

    def test_runtime_statistics_count_stack_quantity(self) -> None:
        from farm_statistics import FarmStatistics

        stats = FarmStatistics()
        reward = BattleReward(dust=0, xp=0, items=("Осколок x3",))
        stats.add_victory(1, reward)
        self.assertEqual(stats.session_report().drops, {"Осколок": 3})

    def test_runtime_statistics_count_crystals(self) -> None:
        from farm_statistics import FarmStatistics

        stats = FarmStatistics()
        stats.add_victory(1, BattleReward(dust=0, xp=0, items=(), crystals=3))

        self.assertEqual(stats.session_report().crystals, 3)
