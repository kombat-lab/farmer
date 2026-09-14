from __future__ import annotations

import asyncio
import subprocess
import sys
import unittest
from copy import deepcopy
from dataclasses import FrozenInstanceError, dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, patch

from automation_policy import CombatPlannerMode, TargetPolicy
from battle_records import BattleOutcome
from combat import CombatAction, CombatActionKind, CombatController, CombatEventKind, CombatStatus
from combat_knowledge_namespace import LEGACY_COMBAT_KNOWLEDGE_NAMESPACE
from combat_rules import CombatTurnInput, CombatTurnPlan, LegacyCombatRuleset
from combat_strategy import ObservedRange, RecentCombatKnowledge, SkillTarget
from game_input import ActionOutcome
from legacy_combat_controller import LegacyCombatController, LegacyCombatRuntime
from message_snapshot import MessageSnapshot
from tests.combat_runtime_harness import Button, Message, Runtime


def turn_message(identifier: int = 42) -> Message:
    return Message(
        identifier,
        "⚔️ Раунд 3\nХод Игрок\nМана: 12/12\nВыберите навык:\n⏳ Осталось: 40 сек.",
        [[Button("Атака аколита"), Button("Лечение [Мана 4]"), Button("Святое свечение [Мана 3]")]],
    )


def prepare_turn(controller: LegacyCombatController, runtime: Runtime) -> None:
    runtime.context.current_hp = 150
    runtime.context.active_target = "Фонарщик"
    runtime.context.add_combat_enemy("Фонарщик")
    controller.activate_combat_profile(400)
    controller.memory.begin("Фонарщик")
    controller.memory.enemy_current_hp = 300
    controller.memory.enemy_max_hp = 300
    for _ in range(4):
        controller.memory.incoming_damage.add(40)
    for _ in range(2):
        controller.memory.outgoing_damage.setdefault("атака аколита", ObservedRange()).add(60)
        controller.memory.outgoing_damage.setdefault("святое свечение", ObservedRange()).add(100)
        controller.memory.direct_healing.add(140)


class LegacyCombatControllerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.runtime = Runtime()
        runtime: LegacyCombatRuntime = self.runtime
        self.controller = LegacyCombatController(runtime, character_name="Игрок")
        self.port: CombatController = self.controller

    async def dispatch(self, message: Message) -> None:
        observation = self.port.observe_message(self.runtime.capture(message))
        assert observation is not None
        self.assertTrue(await self.port.handle_message(observation))

    async def test_namespaced_knowledge_loads_once_and_persists_detached_payload(self) -> None:
        knowledge = RecentCombatKnowledge(incoming={"фонарщик": [31, 35]})
        self.runtime.knowledge[LEGACY_COMBAT_KNOWLEDGE_NAMESPACE, 400] = knowledge.as_payload()
        self.runtime.knowledge["future-rules:2", 800] = knowledge.as_payload()
        await self.port.initialize()
        await self.port.initialize()
        self.assertEqual(self.runtime.loads, [LEGACY_COMBAT_KNOWLEDGE_NAMESPACE])
        self.assertEqual(set(self.controller.combat_knowledge_profiles), {400})

        await self.dispatch(Message(1, "Вы напали: Фонарщик\n❤️ 300/300"))
        self.assertEqual(self.controller.memory.incoming_damage.samples, 2)
        self.assertEqual(self.controller.memory.incoming_damage.maximum, 35)
        await self.port.persist()
        namespace, hp, payload = self.runtime.saves[-1]
        self.assertEqual((namespace, hp), (LEGACY_COMBAT_KNOWLEDGE_NAMESPACE, 400))
        expected = deepcopy(payload)
        self.controller.memory.knowledge.add_incoming("Фонарщик", 99)
        self.port.reset()
        self.assertEqual(payload, expected)

    async def test_turn_modes_choose_the_same_legacy_actions(self) -> None:
        cases: tuple[tuple[CombatPlannerMode, int, str], ...] = (
            ("shadow", 1, "лечение"),
            ("guarded", 1, "лечение"),
            ("active", 2, "святое свечение"),
        )
        for mode, column, label in cases:
            with self.subTest(mode=mode):
                runtime = Runtime(policy=replace(self.runtime.policy, planner_mode=mode))
                controller = LegacyCombatController(runtime, character_name="Игрок")
                prepare_turn(controller, runtime)
                message = turn_message()
                observation = controller.observe_message(runtime.capture(message))
                assert observation is not None
                await controller.handle_message(observation)
                source, snapshot, action = runtime.actions[-1]
                self.assertIs(source, observation.event)
                self.assertEqual(snapshot, observation.snapshot)
                self.assertEqual(
                    (action.kind, action.position, action.label),
                    (CombatActionKind.ACTION, (0, column), label),
                )
                self.assertEqual(action.remaining_seconds, 40)
                self.assertEqual(controller.status().pending_action_label, label)
                self.assertTrue(controller.status().active)
                self.assertIsNotNone(controller.pending_combat_decision)

    async def test_unsent_turn_clears_pending_decision(self) -> None:
        prepare_turn(self.controller, self.runtime)
        self.runtime.action_result = ActionOutcome.REJECTED
        await self.dispatch(turn_message())
        self.assertIsNone(self.controller.memory.pending_skill)
        self.assertIsNone(self.controller.memory.pending_target)
        self.assertFalse(self.controller.memory.pending_urgent)
        self.assertIsNone(self.controller.pending_combat_decision)

    async def test_countdown_repeat_preserves_pending_trace_without_second_action(self) -> None:
        prepare_turn(self.controller, self.runtime)
        message = turn_message()
        await self.dispatch(message)
        pending = self.controller.pending_combat_decision
        message.raw_text = message.raw_text.replace("40 сек", "39 сек")
        message.edit_date = self.runtime.clock + timedelta(seconds=1)
        await self.dispatch(message)
        self.assertEqual(len(self.runtime.actions), 1)
        self.assertIs(self.controller.pending_combat_decision, pending)
        self.assertEqual(len(self.controller.memory.round_history), 1)
        assert self.controller.memory.latest_round is not None
        self.assertEqual(self.controller.memory.latest_round.remaining_seconds, 39)

    async def test_confirmation_learns_once_even_when_stale_or_keyboard_changes(self) -> None:
        prepare_turn(self.controller, self.runtime)
        self.runtime.policy = replace(self.runtime.policy, planner_mode="active")
        await self.dispatch(turn_message())
        confirmation = Message(
            43,
            "⚔️ Раунд 4\nЛевая сторона\nИгрок использует Святое свечение\n"
            "Фонарщик получает 100 урона",
        )
        self.runtime.latest = False
        await self.dispatch(confirmation)
        self.assertEqual(len(self.controller.combat_decisions), 1)
        self.assertEqual(self.controller.memory.outgoing_damage["святое свечение"].samples, 3)
        confirmation.buttons = [[Button("изменённая клавиатура")]]
        await self.dispatch(confirmation)
        self.assertEqual(len(self.controller.combat_decisions), 1)
        self.assertEqual(self.controller.memory.outgoing_damage["святое свечение"].samples, 3)
        self.assertEqual(len(self.runtime.actions), 1)

    async def test_target_selection_uses_exact_snapshot_position_and_confirms_treatment(
        self,
    ) -> None:
        prepare_turn(self.controller, self.runtime)
        self.controller.memory.pending_skill = "лечение"
        self.controller.memory.pending_target = SkillTarget.SELF
        message = Message(
            2,
            "Выберите цель для «Лечение»",
            [
                [Button("Игрок ветеран [100/400]"), Button("Фонарщик [40/300]")],
                [Button("Игрок [150/400]")],
            ],
        )
        await self.dispatch(message)
        action = self.runtime.actions[-1][2]
        self.assertEqual((action.kind, action.position), (CombatActionKind.TARGET, (1, 0)))
        self.assertEqual(action.label, "боевая цель Игрок")
        self.assertIn("Фонарщик", self.runtime.policy.treatment_enemies)
        self.assertEqual(self.runtime.events[0][0], "TREATMENT_ENEMY_CONFIRMED")

    async def test_target_policy_is_frozen_across_treatment_confirmation_await(self) -> None:
        self.controller.memory.pending_skill = "лечение"
        self.controller.memory.pending_target = SkillTarget.ENEMY
        self.runtime.policy_after_confirmation = TargetPolicy(("Пепельник", "Фонарщик"))
        message = Message(
            2,
            "Выберите цель для «Лечение»",
            [
                [Button("Фонарщик [100/300]"), Button("Пепельник [100/300]")],
            ],
        )
        await self.dispatch(message)
        self.assertEqual(self.runtime.actions[-1][2].position, (0, 0))
        self.assertEqual(self.runtime.targets.enabled[0], "Пепельник")

    async def test_cooldown_target_does_not_confirm_or_send(self) -> None:
        self.runtime.cooldown = 10
        self.controller.memory.pending_skill = "лечение"
        await self.dispatch(
            Message(
                2,
                "Выберите цель для «Лечение»",
                [
                    [Button("Фонарщик [100/300]")],
                ],
            )
        )
        self.assertEqual(self.runtime.actions, [])
        self.assertEqual(self.runtime.events, [])

    async def test_no_available_action_or_target_requests_recovery(self) -> None:
        await self.dispatch(
            Message(1, "Выберите навык:\nМана: 0/12", [[Button("Лечение [Мана 4]")]])
        )
        await self.dispatch(Message(2, "Выберите цель для «Лечение»", [[Button("Отмена")]]))
        self.assertEqual(
            self.runtime.requests,
            [
                "не найден доступный навык",
                "не найдена доступная цель навыка",
            ],
        )
        self.assertEqual(self.runtime.actions, [])

    async def test_duplicate_victory_records_stats_once(self) -> None:
        prepare_turn(self.controller, self.runtime)
        self.runtime.position = (4, 6)
        message = Message(
            99,
            "Бой завершён\nПобеда\n• + 10 XP\n• + 5 ед. Туманной пыли\nПредметы:\nКарта Фонарщик",
        )
        observation = self.port.observe_message(self.runtime.capture(message))
        assert observation is not None
        await asyncio.gather(*(self.port.handle_message(observation) for _ in range(3)))
        self.assertEqual(self.runtime.record_calls, 1)
        self.assertEqual(len(self.runtime.victories), 1)
        self.assertEqual(self.runtime.victories[99].xp, 10)
        self.assertEqual(self.runtime.reports, 1)
        self.assertEqual(len(self.runtime.saves), 1)
        self.assertEqual(self.runtime.outcomes[99].target_name, "Фонарщик")
        self.assertFalse(self.port.status().active)
        self.assertIsNone(self.port.status().target_name)

    async def test_duplicate_defeat_enters_recovery_once_without_action(self) -> None:
        prepare_turn(self.controller, self.runtime)
        message = Message(99, "Бой завершён\nПоражение")
        await self.dispatch(message)
        await self.dispatch(message)
        self.assertEqual(self.runtime.record_calls, 1)
        self.assertEqual(self.runtime.defeats, {99})
        self.assertEqual(self.runtime.recoveries, ["Фонарщик"])
        self.assertEqual(self.runtime.interruptions, 1)
        self.assertEqual(self.runtime.outcomes[99].result, "DEFEAT")
        self.assertEqual(self.runtime.actions, [])
        self.assertEqual(len(self.runtime.saves), 1)
        self.assertIsNone(self.controller.memory.target_name)

    async def test_mutable_source_cannot_change_observed_result_or_snapshot(self) -> None:
        prepare_turn(self.controller, self.runtime)
        message = Message(99, "Бой завершён\nПобеда\n• + 10 XP", [[Button("Исход")]])
        observation = self.port.observe_message(self.runtime.capture(message))
        assert observation is not None
        expected = observation.snapshot
        message.raw_text = "Бой завершён\nПоражение"
        message.buttons[0][0].text = "Изменено"
        await self.port.handle_message(observation)
        self.assertEqual(observation.snapshot, expected)
        self.assertEqual(expected.buttons[0][0].text, "Исход")
        self.assertEqual(self.runtime.outcomes[99].result, "VICTORY")
        self.assertEqual(self.runtime.outcomes[99].rewards.xp, 10)
        self.assertEqual(self.runtime.defeats, set())

    async def test_mutable_source_cannot_redirect_a_planned_action(self) -> None:
        prepare_turn(self.controller, self.runtime)
        message = turn_message()
        observation = self.port.observe_message(self.runtime.capture(message))
        assert observation is not None
        message.buttons[0].reverse()
        await self.port.handle_message(observation)
        self.assertEqual(self.runtime.actions[-1][2].position, (0, 1))
        self.assertEqual(len(self.controller.memory.round_history), 1)
        self.assertEqual(observation.snapshot.buttons[0][0].text, "Атака аколита")

    async def test_stale_turn_still_observes_facts_without_an_action(self) -> None:
        self.runtime.latest = False
        await self.dispatch(
            Message(
                1,
                "⚔️ Раунд 5\nИгрок получает 20 урона\nВыберите навык:",
                [[Button("Атака аколита")]],
            )
        )
        self.assertEqual(self.controller.memory.incoming_damage.samples, 1)
        self.assertEqual(self.runtime.actions, [])

    async def test_ambush_interrupts_discovery_and_sets_encounter_identity(self) -> None:
        await self.dispatch(Message(1, "На вас напали: Фонарщик\n❤️ 300/300"))
        self.assertEqual(self.runtime.interruptions, 1)
        self.assertEqual(self.port.status().target_name, "Фонарщик")
        self.assertEqual(self.port.status().enemy_names, ("Фонарщик",))
        self.assertTrue(self.port.status().active)

    def test_observation_and_neutral_status_are_immutable_and_need_no_click(self) -> None:
        snapshot = MessageSnapshot.from_message(turn_message())
        with patch("parser.parse_map", side_effect=AssertionError("Combat must not parse maps")):
            observation = self.port.observe_message(self.runtime.capture(snapshot))
        assert observation is not None
        self.assertEqual(observation.kind, CombatEventKind.TURN)
        self.assertFalse(hasattr(observation.snapshot, "click"))
        for value, field_name in ((observation, "kind"), (self.port.status(), "active")):
            self.assertFalse(hasattr(value, "__dict__"))
            with self.assertRaises(FrozenInstanceError):
                setattr(value, field_name, None)
        for text in ("Шаг начат", "Выбери цель для нападения", "Случайный текст"):
            self.assertIsNone(self.port.observe_message(self.runtime.capture(Message(2, text))))

    async def test_previously_persisted_result_does_not_reenter_session_statistics(self) -> None:
        for result in ("VICTORY", "DEFEAT"):
            with self.subTest(result=result):
                runtime = Runtime()
                text = "Победа\nПредметы:\nКарта Фонарщик" if result == "VICTORY" else "Поражение"
                message = Message(99, "Бой завершён\n" + text)
                snapshot = runtime.capture(message).snapshot
                runtime.outcomes[99] = BattleOutcome(
                    source_event_id=runtime.source_event_id(snapshot),
                    source_message_id=99,
                    session_id=2,
                    target_name="Фонарщик",
                    result="VICTORY" if result == "VICTORY" else "DEFEAT",
                )
                controller: CombatController = LegacyCombatController(
                    runtime, character_name="Игрок"
                )
                observation = controller.observe_message(runtime.capture(message))
                assert observation is not None
                await controller.handle_message(observation)
                self.assertEqual(runtime.victories, {})
                self.assertEqual(runtime.defeats, set())
                self.assertEqual(runtime.reports, 0)
                self.assertEqual(runtime.record_calls, 1)
                self.assertEqual(len(runtime.recoveries), 0)

    async def test_ledger_failure_does_not_commit_session_statistics(self) -> None:
        message = Message(99, "Бой завершён\nПобеда")
        observation = self.port.observe_message(self.runtime.capture(message))
        assert observation is not None
        with patch.object(
            self.runtime, "record_battle", new=AsyncMock(side_effect=OSError("disk"))
        ):
            with self.assertRaises(OSError):
                await self.port.handle_message(observation)
        self.assertEqual(self.runtime.victories, {})
        await self.port.handle_message(observation)
        self.assertEqual(len(self.runtime.victories), 1)

    async def test_failed_observation_retries_learning_and_trace_exactly_once(self) -> None:
        prepare_turn(self.controller, self.runtime)
        await self.dispatch(turn_message())
        confirmation = Message(
            43, "⚔️ Раунд 4\nИгрок использует Лечение\nФонарщик получает 124 урона"
        )
        observation = self.port.observe_message(self.runtime.capture(confirmation))
        assert observation is not None
        with patch.object(
            self.runtime, "add_treatment_enemy_target", new=AsyncMock(side_effect=OSError("disk"))
        ):
            with self.assertRaises(OSError):
                await self.port.handle_message(observation)
        self.assertEqual(len(self.controller.memory.round_history), 1)
        self.assertEqual(self.controller.combat_decisions, [])
        self.assertIsNotNone(self.controller.pending_combat_decision)
        await self.port.handle_message(observation)
        await self.port.handle_message(observation)
        self.assertEqual(len(self.controller.memory.round_history), 2)
        self.assertEqual(len(self.controller.combat_decisions), 1)
        self.assertEqual(self.controller.memory.outgoing_damage["лечение"].samples, 1)

    async def test_injected_rules_receive_the_original_observation_time(self) -> None:
        @dataclass(frozen=True)
        class RecordingRuleset(LegacyCombatRuleset):
            turns: list[CombatTurnInput] = field(default_factory=list)

            def plan_turn(self, turn: CombatTurnInput) -> CombatTurnPlan:
                self.turns.append(turn)
                return super().plan_turn(turn)

        rules = RecordingRuleset()
        controller = LegacyCombatController(self.runtime, character_name="Игрок", ruleset=rules)
        prepare_turn(controller, self.runtime)
        message = turn_message()
        observation = controller.observe_message(self.runtime.capture(message))
        assert observation is not None
        self.runtime.clock += timedelta(minutes=5)
        await controller.handle_message(observation)
        self.assertEqual(rules.turns[0].created_at, observation.observed_at)
        assert controller.pending_combat_decision is not None
        self.assertEqual(
            controller.pending_combat_decision.created_at, observation.observed_at.isoformat()
        )

    def test_action_rejects_invalid_positions_and_empty_labels(self) -> None:
        for position in ((-1, 0), (0, -1), (True, 0), (0, False), (0.5, 0), (0,), "00"):
            with self.subTest(position=position), self.assertRaises(ValueError):
                CombatAction(CombatActionKind.ACTION, cast(tuple[int, int], position), "Атака")
        for label in ("", "   "):
            with self.subTest(label=label), self.assertRaises(ValueError):
                CombatAction(CombatActionKind.ACTION, (0, 0), label)

    def test_action_and_status_validate_runtime_field_types(self) -> None:
        action = CombatAction(CombatActionKind.ACTION, (0, 0), "Атака")
        for value in (-1, True):
            with self.subTest(seconds=value), self.assertRaises(ValueError):
                replace(action, remaining_seconds=value)
        with self.assertRaises(ValueError):
            replace(action, kind=cast(CombatActionKind, "action"))
        with self.assertRaises(ValueError):
            replace(action, urgent=cast(bool, "yes"))
        status = self.port.status()
        with self.assertRaises(ValueError):
            replace(status, observations=-1)
        with self.assertRaises(ValueError):
            replace(status, enemy_names=("",))
        with self.assertRaises(ValueError):
            replace(status, active=cast(bool, 1))

    def test_status_detaches_names_and_observation_rejects_naive_time(self) -> None:
        names = ["Фонарщик"]
        status = CombatStatus(True, "Фонарщик", cast(tuple[str, ...], names), None, 1)
        names.clear()
        self.assertEqual(status.enemy_names, ("Фонарщик",))
        self.runtime.clock = self.runtime.clock.replace(tzinfo=None)
        with self.assertRaises(ValueError):
            self.port.observe_message(self.runtime.capture(turn_message()))

    async def test_unknown_skill_delivery_keeps_self_target_and_never_repeats_turn(self) -> None:
        prepare_turn(self.controller, self.runtime)
        self.runtime.action_result = ActionOutcome.DELIVERY_UNKNOWN
        message = turn_message()
        await self.dispatch(message)
        pending = self.controller.pending_combat_decision
        self.assertIsNotNone(pending)
        self.assertIs(self.controller.memory.pending_target, SkillTarget.SELF)
        await self.dispatch(message)
        self.assertEqual(len(self.runtime.actions), 1)
        await self.dispatch(
            Message(
                43,
                "Выберите цель для «Лечение»",
                [
                    [Button("Фонарщик [40/300]"), Button("Игрок [150/400]")],
                ],
            )
        )
        self.assertEqual(self.runtime.actions[-1][2].position, (0, 1))
        self.assertIs(self.controller.pending_combat_decision, pending)
        self.assertEqual(self.runtime.requests, [])

    async def test_same_round_text_edit_preserves_action_claim_and_learning(self) -> None:
        prepare_turn(self.controller, self.runtime)
        message = turn_message()
        await self.dispatch(message)
        pending = self.controller.pending_combat_decision
        message.raw_text += "\nНовая подпись интерфейса"
        message.edit_date = self.runtime.clock + timedelta(seconds=1)
        await self.dispatch(message)
        self.assertEqual(len(self.runtime.actions), 1)
        self.assertIs(self.controller.pending_combat_decision, pending)
        self.assertEqual(len(self.controller.memory.round_history), 1)

    async def test_repeated_target_prompt_is_claimed_across_new_message_ids(self) -> None:
        prepare_turn(self.controller, self.runtime)
        await self.dispatch(turn_message())
        self.runtime.action_result = ActionOutcome.DELIVERY_UNKNOWN
        first = Message(
            43,
            "Выберите цель для «Лечение»",
            [
                [Button("Фонарщик [40/300]"), Button("Игрок [150/400]")],
            ],
        )
        await self.dispatch(first)
        await self.dispatch(Message(44, first.raw_text + "\nОбновление", first.buttons))
        self.assertEqual(len(self.runtime.actions), 2)
        self.assertEqual(self.runtime.requests, [])

    async def test_runtime_exception_keeps_claim_when_delivery_is_not_known(self) -> None:
        prepare_turn(self.controller, self.runtime)
        message = turn_message()
        with patch.object(
            self.runtime, "execute_combat_action", new=AsyncMock(side_effect=OSError("after RPC"))
        ) as execute:
            with self.assertRaises(OSError):
                await self.dispatch(message)
            await self.dispatch(message)
        execute.assert_awaited_once()
        self.assertIsNotNone(self.controller.pending_combat_decision)
        self.assertIs(self.controller.memory.pending_target, SkillTarget.SELF)

    async def test_deferred_action_can_retry_but_unknown_cannot(self) -> None:
        prepare_turn(self.controller, self.runtime)
        self.runtime.action_result = ActionOutcome.DEFERRED
        message = turn_message()
        await self.dispatch(message)
        self.assertIsNone(self.controller.pending_combat_decision)
        self.runtime.action_result = ActionOutcome.SENT
        await self.dispatch(message)
        self.assertIsNotNone(self.controller.pending_combat_decision)
        await self.dispatch(message)
        self.assertEqual(len(self.runtime.actions), 2)

    async def test_old_confirmation_does_not_resolve_new_battle_pending(self) -> None:
        await self.dispatch(Message(10, "Вы напали: Фонарщик"))
        await self.dispatch(Message(20, "Вы напали: Пепельник"))
        message = turn_message(21)
        await self.dispatch(message)
        pending = self.controller.pending_combat_decision
        assert pending is not None
        old = Message(
            12,
            "⚔️ Раунд 4\nИгрок использует "
            + pending.decision.skill_name
            + "\nФонарщик получает 100 урона",
        )
        await self.dispatch(old)
        self.assertIs(self.controller.pending_combat_decision, pending)
        self.assertEqual(self.controller.combat_decisions, [])
        self.assertEqual(self.controller.memory.target_name, "Пепельник")

    async def test_late_finished_records_old_target_without_resetting_new_battle(self) -> None:
        await self.dispatch(Message(10, "Вы напали: Фонарщик"))
        await self.dispatch(Message(20, "Вы напали: Пепельник"))
        await self.dispatch(turn_message(21))
        pending = self.controller.pending_combat_decision
        old = Message(15, "Бой завершён\nПоражение")
        observation = self.port.observe_message(self.runtime.capture(old))
        assert observation is not None
        self.assertFalse(observation.authoritative)
        await self.port.handle_message(observation)
        self.assertEqual(self.runtime.outcomes[15].target_name, "Фонарщик")
        self.assertEqual(self.runtime.recoveries, [])
        self.assertEqual(self.controller.memory.target_name, "Пепельник")
        self.assertIs(self.controller.pending_combat_decision, pending)
        self.assertTrue(self.port.status().active)

    async def test_completed_battle_metadata_survives_reset_and_late_result_edit(self) -> None:
        await self.dispatch(Message(10, "Вы напали: Фонарщик"))
        await self.dispatch(Message(15, "Бой завершён\nПобеда"))
        await self.dispatch(Message(20, "Вы напали: Пепельник"))
        with patch.object(
            self.runtime, "record_battle", wraps=self.runtime.record_battle
        ) as record:
            await self.dispatch(Message(15, "Бой завершён\nПобеда\nДополнительная подпись"))
        self.assertEqual(record.call_args.args[0].target_name, "Фонарщик")
        self.assertEqual(self.controller.memory.target_name, "Пепельник")

    async def test_new_battle_can_reuse_completed_message_id_with_new_revision(self) -> None:
        first = Message(10, "Вы напали: Фонарщик", edit_date=self.runtime.clock)
        await self.dispatch(first)
        await self.dispatch(
            Message(10, "Бой завершён\nПобеда", edit_date=self.runtime.clock + timedelta(seconds=1))
        )
        await self.dispatch(
            Message(10, "Вы напали: Пепельник", edit_date=self.runtime.clock + timedelta(seconds=2))
        )
        message = turn_message(10)
        message.edit_date = self.runtime.clock + timedelta(seconds=3)
        await self.dispatch(message)
        self.assertEqual(self.controller.memory.target_name, "Пепельник")
        self.assertEqual(len(self.runtime.actions), 1)
        self.assertTrue(self.port.status().active)

    def test_names_prefer_exact_then_longest_bounded_match(self) -> None:
        runtime = Runtime(targets=TargetPolicy(("Волк", "Чёрный волк")))
        controller = LegacyCombatController(runtime, character_name="Игрок")
        self.assertEqual(controller.canonical_combat_enemy("Чёрный волк"), "Чёрный волк")
        self.assertEqual(controller.canonical_combat_enemy("☠️ Чёрный волк"), "Чёрный волк")
        self.assertEqual(controller.canonical_combat_enemy("Волкодав"), "Волкодав")


class CombatImportIsolationTests(unittest.TestCase):
    def test_neutral_contract_and_legacy_adapter_imports_are_isolated(self) -> None:
        project = Path(__file__).resolve().parents[1]
        cases = (
            ("combat", ("combat_rules", "combat_strategy", "combat_round", "skills")),
            ("legacy_combat_controller", ()),
        )
        for module, additional in cases:
            forbidden = {
                "telethon",
                "farmer",
                "supervisor",
                "storage",
                "legacy_map_controller",
                "navigator",
                *additional,
            }
            with self.subTest(module=module):
                script = f"""
import importlib.abc
import sys
class BlockRuntimeImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {forbidden!r}:
            raise AssertionError('Forbidden combat dependency: ' + fullname)
        return None
sys.meta_path.insert(0, BlockRuntimeImports())
sys.path.insert(0, sys.argv[1])
__import__({module!r})
"""
                completed = subprocess.run(
                    [sys.executable, "-I", "-B", "-c", script, str(project)],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=15,
                )
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
