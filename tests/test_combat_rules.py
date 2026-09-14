from __future__ import annotations

import subprocess
import sys
import unittest
from copy import deepcopy
from dataclasses import FrozenInstanceError, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

from automation_policy import CombatPlannerMode, parse_combat_planner_mode
from combat_knowledge_namespace import LEGACY_COMBAT_KNOWLEDGE_NAMESPACE
from combat_learning import build_shadow_plan, select_combat_planner_decision
from combat_round import parse_combat_round
from combat_rules import (
    CombatRuleset,
    CombatTurnInput,
    CombatTurnPlan,
    LegacyCombatRuleset,
)
from combat_strategy import (
    CombatMemory,
    ObservedRange,
    build_decision_trace,
    choose_combat_action,
)
from farmer_combat_runtime import FarmerCombatRuntime
from game_input import ActionOutcome
from legacy_combat_controller import LegacyCombatController
from legacy_combat_diagnostics import LegacyCombatDiagnostics
from legacy_fog_mechanisms import ManagedLegacyCombatController
from message_snapshot import MessageSnapshot
from notifications import Notifier
from settings_service import SettingsService
from storage import Storage
from tests.legacy_fog_factory import legacy_farmer, legacy_runtime


@dataclass(frozen=True)
class Button:
    text: str


@dataclass(frozen=True)
class ReadOnlyCombatView:
    """No message ID, click method, edit date, position, or map is required."""

    raw_text: str = "⚔️ Раунд 3\nХод Игрок\nМана: 12/12\nОсталось: 40 сек"
    buttons: tuple[tuple[Button, ...], ...] = (
        (
            Button("Атака аколита"),
            Button("Лечение [Мана 4]"),
            Button("Святое свечение [Мана 3]"),
        ),
    )


def prepared_turn(enemy_hp: int = 300, hp: int = 150) -> CombatTurnInput:
    memory = CombatMemory(target_name="Фонарщик", enemy_current_hp=enemy_hp, enemy_max_hp=enemy_hp)
    for _ in range(4):
        memory.incoming_damage.add(40)
    for _ in range(2):
        memory.outgoing_damage.setdefault("атака аколита", ObservedRange()).add(60)
        memory.outgoing_damage.setdefault("святое свечение", ObservedRange()).add(100)
        memory.direct_healing.add(140)
    return CombatTurnInput(
        message=ReadOnlyCombatView(),
        message_id=42,
        created_at=datetime(2026, 9, 14, 12, 30, tzinfo=UTC),
        memory=memory,
        current_hp=hp,
        max_hp=400,
        heal_threshold=160,
    )


class CombatRulesTests(unittest.TestCase):
    def test_modes_preserve_legacy_decisions_and_complete_traces(self) -> None:
        rules: CombatRuleset = LegacyCombatRuleset()
        cases: tuple[tuple[int, int, CombatPlannerMode, str], ...] = (
            (300, 150, "shadow", "лечение"),
            (300, 150, "guarded", "лечение"),
            (300, 150, "active", "атака аколита"),
            (180, 90, "shadow", "лечение"),
            (180, 90, "guarded", "святое свечение"),
            (180, 90, "active", "святое свечение"),
        )
        for enemy_hp, hp, mode, expected_skill in cases:
            with self.subTest(enemy_hp=enemy_hp, hp=hp, mode=mode):
                turn = replace(prepared_turn(enemy_hp, hp), planner_mode=mode)
                parsed = parse_combat_round(
                    turn.message.raw_text or "",
                    [button.text for row in turn.message.buttons or () for button in row],
                )
                baseline = choose_combat_action(
                    turn.message,
                    memory=turn.memory,
                    current_hp=turn.current_hp,
                    max_hp=turn.max_hp,
                    heal_threshold=turn.heal_threshold,
                    round_state=parsed,
                )
                assert baseline is not None
                shadow = build_shadow_plan(
                    turn.message,
                    memory=turn.memory,
                    current_hp=turn.current_hp,
                    max_hp=turn.max_hp,
                    executed=baseline,
                    round_state=parsed,
                )
                assert shadow is not None
                decision = select_combat_planner_decision(shadow, mode)
                shadow = shadow.with_execution(decision, mode=mode)
                trace = build_decision_trace(
                    created_at=turn.created_at.isoformat(),
                    telegram_message_id=turn.message_id,
                    memory=turn.memory,
                    round_state=parsed,
                    current_hp=turn.current_hp,
                    max_hp=turn.max_hp,
                    decision=decision,
                    shadow_plan=shadow.as_payload(),
                )

                result = rules.plan_turn(turn)
                self.assertEqual(result.decision, decision)
                self.assertEqual(decision.skill_name, expected_skill)
                self.assertEqual(result.round_state, parsed)
                self.assertEqual(result.shadow_plan, shadow)
                assert result.trace is not None
                self.assertEqual(result.trace.as_payload(), trace.as_payload())

    def test_planning_neither_learns_nor_sets_pending_actions(self) -> None:
        turn = prepared_turn()
        before = deepcopy(turn.memory)
        result = LegacyCombatRuleset().plan_turn(turn)
        self.assertEqual(turn.memory, before)
        self.assertIsNone(turn.memory.pending_skill)
        self.assertEqual(turn.memory.round_history, [])
        assert result.trace is not None
        self.assertEqual(result.trace.incoming_samples, 4)

    def test_result_is_detached_from_memory_and_mutable_trace_payloads(self) -> None:
        turn = prepared_turn()
        result = LegacyCombatRuleset().plan_turn(turn)
        trace = result.trace
        assert trace is not None and trace.shadow_plan is not None
        expected_payload = deepcopy(trace.as_payload())
        trace.shadow_plan.clear()
        turn.memory.reset()
        fresh_trace = result.trace
        assert fresh_trace is not None
        self.assertEqual(fresh_trace.as_payload(), expected_payload)
        attribute = "decision"
        with self.assertRaises(FrozenInstanceError):
            setattr(result, attribute, None)

    def test_supplied_observed_round_is_preserved(self) -> None:
        rules = LegacyCombatRuleset()
        turn = prepared_turn()
        parsed = rules.parse_round(turn.message)
        assert parsed is not None
        observed = replace(parsed, remaining_seconds=19)
        result = rules.plan_turn(replace(turn, round_state=observed))
        self.assertIs(result.round_state, observed)

    def test_plan_rejects_missing_or_mismatched_trace(self) -> None:
        result = LegacyCombatRuleset().plan_turn(prepared_turn())
        assert result.decision is not None and result.trace is not None
        with self.assertRaises(ValueError):
            CombatTurnPlan(result.decision, result.round_state, result.shadow_plan, None)
        with self.assertRaises(ValueError):
            replace(result, decision=replace(result.decision, skill_name="Другое действие"))
        with self.assertRaises(ValueError):
            CombatTurnPlan(None, result.round_state, None, result.trace)

    def test_turn_input_rejects_invalid_identity_health_and_time(self) -> None:
        turn = prepared_turn()
        for message_id in (0, -1, True):
            with self.subTest(message_id=message_id), self.assertRaises(ValueError):
                replace(turn, message_id=message_id)
        with self.assertRaises(ValueError):
            replace(turn, created_at=turn.created_at.replace(tzinfo=None))
        with self.assertRaises(ValueError):
            replace(turn, current_hp=-1)
        with self.assertRaises(ValueError):
            replace(turn, heal_threshold=0)

    def test_no_castable_action_has_no_trace_or_forecast(self) -> None:
        message = ReadOnlyCombatView("Мана: 0/12", ((Button("Лечение [Мана 4]"),),))
        result = LegacyCombatRuleset().plan_turn(replace(prepared_turn(), message=message))
        self.assertIsNone(result.decision)
        self.assertIsNone(result.shadow_plan)
        self.assertIsNone(result.trace)

    def test_unknown_health_keeps_basic_attack_without_forecast(self) -> None:
        result = LegacyCombatRuleset().plan_turn(
            replace(prepared_turn(), current_hp=None, max_hp=None)
        )
        assert result.decision is not None and result.trace is not None
        self.assertEqual(result.decision.skill_name, "атака аколита")
        self.assertIsNone(result.shadow_plan)
        self.assertIsNone(result.trace.player_current_hp)

    def test_rules_namespace_matches_persistence_and_excludes_character_profile(self) -> None:
        class ChangedRules(LegacyCombatRuleset):
            rules_version: ClassVar[int] = 2
            knowledge_namespace: ClassVar[str] = "legacy-acolyte:rules-2:model-5"

        rules: CombatRuleset = LegacyCombatRuleset()
        namespace = rules.knowledge_namespace
        self.assertEqual(namespace, LEGACY_COMBAT_KNOWLEDGE_NAMESPACE)
        self.assertIn(f"model-{rules.model_version}", namespace)
        self.assertNotIn("hp-", namespace)
        self.assertNotEqual(namespace, ChangedRules().knowledge_namespace)
        self.assertEqual(namespace, LegacyCombatRuleset().knowledge_namespace)

    def test_unknown_mode_retains_safe_shadow_fallback(self) -> None:
        self.assertEqual(parse_combat_planner_mode(" ACTIVE "), "active")
        self.assertEqual(parse_combat_planner_mode("guarded"), "guarded")
        self.assertEqual(parse_combat_planner_mode("unsupported"), "shadow")

    def test_import_and_planning_need_neither_telegram_nor_navigation(self) -> None:
        project = Path(__file__).resolve().parents[1]
        script = """
import importlib.abc
import sys
from datetime import UTC, datetime
from types import SimpleNamespace

class BlockRuntimeImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'telethon', 'farmer', 'navigator', 'storage'}:
            raise AssertionError('Combat rules imported runtime dependency: ' + fullname)
        return None

sys.meta_path.insert(0, BlockRuntimeImports())
sys.path.insert(0, sys.argv[1])
from combat_rules import CombatTurnInput, LegacyCombatRuleset
from combat_strategy import CombatMemory
import parser

def reject_map(*args, **kwargs):
    raise AssertionError('Combat planning must not parse a map')
parser.parse_map = reject_map
message = SimpleNamespace(
    raw_text='Мана: 12/12',
    buttons=((SimpleNamespace(text='Атака аколита'),),),
)
assert not hasattr(message, 'click')
turn = CombatTurnInput(message, 42, datetime.now(UTC), CombatMemory(), 400, 400, 160)
assert LegacyCombatRuleset().plan_turn(turn).decision is not None
"""
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(project)],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


@dataclass(frozen=True)
class ActionableCombatMessage(ReadOnlyCombatView):
    id: int = 42
    edit_date: datetime | None = None

    async def click(self, row: int, column: int) -> object:
        raise AssertionError("The integration test must not execute a Telegram callback")


@dataclass(frozen=True)
class RecordingRuleset(LegacyCombatRuleset):
    turns: list[CombatTurnInput] = field(default_factory=list)

    def plan_turn(self, turn: CombatTurnInput) -> CombatTurnPlan:
        self.turns.append(turn)
        return super().plan_turn(turn)


class FarmerCombatRulesIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_farmer_factory_injects_rules_behind_controller_boundary(self) -> None:
        for outcome in (ActionOutcome.SENT, ActionOutcome.REJECTED):
            with self.subTest(outcome=outcome), TemporaryDirectory() as directory:
                storage = Storage(Path(directory) / "combat-test.sqlite3")
                farmer = None
                try:
                    settings = SettingsService(storage)
                    await settings.set_value("combat_planner_mode", "active")
                    turn = prepared_turn()
                    rules = RecordingRuleset()

                    with patch(
                        "tests.legacy_fog_factory.create_test_client",
                        return_value=MagicMock(),
                    ):
                        farmer = legacy_farmer(
                            storage,
                            MagicMock(spec=Notifier),
                            settings,
                        )
                    legacy = legacy_runtime(farmer)
                    diagnostics = LegacyCombatDiagnostics(storage)
                    runtime = FarmerCombatRuntime(
                        legacy._services,
                        legacy.context,
                        diagnostics,
                        storage,
                        legacy.statistics,
                        legacy,
                        source_scope="test:combat-rules",
                        legacy_combat_policy=settings.legacy_combat_policy,
                        target_policy=settings.target_policy,
                        add_treatment_target=settings.add_treatment_enemy_target,
                        remove_treatment_target=settings.remove_treatment_enemy_target,
                        wake_battle_notifications=legacy.battle_notifications.wake,
                    )
                    controller = LegacyCombatController(
                        runtime,
                        character_name="Игрок",
                        ruleset=rules,
                    )
                    controller.activate_combat_profile(400)
                    controller.memory = turn.memory
                    legacy.combat = ManagedLegacyCombatController(controller, diagnostics)
                    legacy.context.current_hp = turn.current_hp
                    legacy.context.max_hp = turn.max_hp
                    message = ActionableCombatMessage(
                        raw_text="⚔️ Раунд 3\nХод Игрок\nМана: 12/12\n"
                        "Выберите навык:\nОсталось: 40 сек"
                    )
                    await farmer.enqueue_message(message)
                    with (
                        patch.object(farmer, "mark_progress"),
                        patch.object(farmer, "log"),
                        patch.object(
                            farmer, "click_button_outcome", new=AsyncMock(return_value=outcome)
                        ) as click,
                    ):
                        await farmer.handle_message(message)
                    click.assert_awaited_once()
                    self.assertEqual(click.call_args.kwargs["position"], (0, 2))
                    self.assertEqual(click.call_args.kwargs["remaining_seconds"], 40)
                    captured = rules.turns[-1]
                    self.assertIsInstance(captured.message, MessageSnapshot)
                    self.assertFalse(hasattr(captured.message, "click"))
                    self.assertEqual(captured.message_id, message.id)
                    self.assertIsNotNone(captured.created_at.utcoffset())
                    self.assertEqual(captured.planner_mode, "active")
                    managed = legacy.combat
                    assert isinstance(managed, ManagedLegacyCombatController)
                    controller = managed.legacy_controller
                    if outcome is ActionOutcome.SENT:
                        self.assertEqual(controller.memory.pending_skill, "святое свечение")
                        self.assertIsNotNone(controller.pending_combat_decision)
                    else:
                        self.assertIsNone(controller.memory.pending_skill)
                        self.assertIsNone(controller.memory.pending_target)
                        self.assertIsNone(controller.pending_combat_decision)
                finally:
                    if farmer is not None:
                        await farmer.task_scope.cancel_and_wait(1.0)
                        await farmer.mechanisms.aclose()
                    await storage.close()
