from __future__ import annotations

import math
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from automation_policy import TargetPolicy
from bounded_values import INT64_MAX
from combat_round import parse_combat_round, same_combatant_name
from combat_strategy import (
    CombatDecision,
    CombatMemory,
    ObservedRange,
    RecentCombatKnowledge,
    SkillTarget,
    build_decision_trace,
    resolve_decision_trace,
)
from legacy_combat_controller import LegacyCombatController
from tests.combat_runtime_harness import Button, Message, Runtime


async def dispatch(
    controller: LegacyCombatController,
    runtime: Runtime,
    message: Message,
) -> None:
    observation = controller.observe_message(runtime.capture(message))
    assert observation is not None
    assert await controller.handle_message(observation)


def prepare_player_turn(
    controller: LegacyCombatController,
    runtime: Runtime,
    *,
    character_name: str,
    target_name: str,
) -> None:
    runtime.context.current_hp = 150
    runtime.context.max_hp = 400
    runtime.context.active_target = target_name
    runtime.context.add_combat_enemy(target_name)
    controller.activate_combat_profile(400)
    controller.memory.begin(target_name)
    controller.memory.enemy_current_hp = 300
    controller.memory.enemy_max_hp = 300
    for _ in range(4):
        controller.memory.incoming_damage.add(40)
    for _ in range(2):
        controller.memory.outgoing_damage.setdefault(
            "атака аколита", ObservedRange()
        ).add(60)
        controller.memory.outgoing_damage.setdefault(
            "святое свечение", ObservedRange()
        ).add(100)
        controller.memory.direct_healing.add(140)


class CombatKnowledgeHardeningTests(unittest.TestCase):
    def test_payload_drops_unbounded_non_integer_and_negative_values(self) -> None:
        too_large = INT64_MAX + 1
        payload: dict[object, object] = {
            "version": 1,
            "sample_limit": True,
            "incoming": {
                "  Фонарщик  ": [
                    True,
                    False,
                    -1,
                    0,
                    31,
                    INT64_MAX,
                    too_large,
                    10**400,
                    math.inf,
                    math.nan,
                ],
                False: [42],
            },
            "critical_incoming": {"Фонарщик": [52]},
            "outgoing": {
                "Фонарщик": {
                    " Лечение ": [True, -5, 73, too_large],
                    False: [91],
                },
                False: {"Лечение": [88]},
            },
            "skill_cooldowns": {
                " Лечение ": 3,
                "bool": True,
                "negative": -1,
                "huge": 10**400,
                "too_large": too_large,
            },
            "direct_healing": [True, -1, 101, 10**400],
            "renewal_healing": [math.inf, 11],
            "treatment_enemy_targets": [True, "  Фонарщик  ", ""],
        }

        knowledge = RecentCombatKnowledge.from_payload(payload)

        self.assertEqual(knowledge.sample_limit, 12)
        self.assertEqual(knowledge.incoming, {"фонарщик": [31, INT64_MAX]})
        self.assertEqual(knowledge.critical_incoming, {"фонарщик": [52]})
        self.assertEqual(knowledge.outgoing, {"фонарщик": {"лечение": [73]}})
        self.assertEqual(knowledge.skill_cooldowns, {"лечение": 3})
        self.assertEqual(knowledge.direct_healing, [101])
        self.assertEqual(knowledge.renewal_healing, [11])
        self.assertEqual(knowledge.treatment_enemy_targets, {"фонарщик"})

        memory = CombatMemory(target_name="Фонарщик")
        knowledge.load_into(memory)
        self.assertGreater(memory.predicted_incoming(after_current_tick=True) or 0, 0)

    def test_direct_observation_ranges_reject_bool_and_unbounded_values(self) -> None:
        observed = ObservedRange()
        for value in (True, False, 0, -1, INT64_MAX + 1, 10**400):
            observed.add(value)
        observed.add(INT64_MAX)

        knowledge = RecentCombatKnowledge()
        for value in (True, False, 0, -1, INT64_MAX + 1, 10**400):
            knowledge.add_incoming("Фонарщик", value)
            knowledge.observe_cooldown("Лечение", value)

        self.assertEqual((observed.minimum, observed.maximum, observed.samples), (
            INT64_MAX,
            INT64_MAX,
            1,
        ))
        self.assertEqual(knowledge.incoming, {})
        self.assertEqual(knowledge.skill_cooldowns, {"лечение": 0})

    def test_invalid_sample_limits_fall_back_without_bool_coercion(self) -> None:
        for raw_limit in (True, False, 0, -1, 101, 10**400, 12.0, math.inf):
            with self.subTest(raw_limit=raw_limit):
                restored = RecentCombatKnowledge.from_payload(
                    {"version": 1, "sample_limit": raw_limit}
                )
                self.assertEqual(restored.sample_limit, 12)


class CombatantIdentityHardeningTests(unittest.IsolatedAsyncioTestCase):
    def test_identity_is_complete_normalized_name_not_substring(self) -> None:
        self.assertTrue(same_combatant_name("🪬🧍Волк", " волк "))
        self.assertFalse(same_combatant_name("Волк", "Чёрный волк"))
        self.assertFalse(same_combatant_name("", "Волк"))

    def test_enemy_with_player_name_suffix_is_not_player_recipient(self) -> None:
        memory = CombatMemory(target_name="Чёрный волк")
        memory.observe(
            "⚔️ Раунд 1\n"
            "Чёрный волк использует Лечение\n"
            "Чёрный волк получает 75 урона\n"
            "Чёрный волк восстанавливает 45 HP",
            "Волк",
        )

        self.assertEqual(memory.incoming_damage.samples, 0)
        self.assertEqual(memory.critical_incoming_damage.samples, 0)
        self.assertEqual(memory.direct_healing.samples, 0)
        self.assertEqual(memory.renewal_healing.samples, 0)

    def test_trace_resolves_enemy_damage_when_enemy_contains_player_name(self) -> None:
        memory = CombatMemory(target_name="Чёрный волк")
        trace = build_decision_trace(
            created_at=datetime(2026, 9, 14, tzinfo=UTC).isoformat(),
            telegram_message_id=7,
            memory=memory,
            round_state=None,
            current_hp=100,
            max_hp=100,
            decision=CombatDecision("Лечение", SkillTarget.ENEMY, "test"),
        )
        round_state = parse_combat_round(
            "⚔️ Раунд 2\nЧёрный волк получает 77 урона"
        )
        assert round_state is not None

        resolved = resolve_decision_trace(trace, round_state, "Волк")

        self.assertIs(resolved.actual_target, SkillTarget.ENEMY)
        self.assertEqual(resolved.actual_effect, "damage")
        self.assertEqual(resolved.actual_amount, 77)

    def test_controller_observes_enemy_whose_name_contains_player_name(self) -> None:
        runtime = Runtime(targets=TargetPolicy(("Волк", "Чёрный волк")))
        controller = LegacyCombatController(runtime, character_name="Волк")
        round_state = parse_combat_round(
            "⚔️ Раунд 2\n"
            "Чёрный волк атакует Волк\n"
            "Чёрный волк\n"
            "❤️ 200/300\n"
            "Волк\n"
            "❤️ 100/200"
        )
        assert round_state is not None

        self.assertEqual(
            controller.observed_combat_enemies(round_state),
            ("Чёрный волк",),
        )

    async def test_enemy_skill_does_not_confirm_players_pending_decision(self) -> None:
        runtime = Runtime(targets=TargetPolicy(("Чёрный волк",)))
        controller = LegacyCombatController(runtime, character_name="Волк")
        prepare_player_turn(
            controller,
            runtime,
            character_name="Волк",
            target_name="Чёрный волк",
        )
        stamp = runtime.clock
        await dispatch(
            controller,
            runtime,
            Message(
                20,
                "⚔️ Раунд 3\nХод Волк\nМана: 12/12\nВыберите навык:",
                [[
                    Button("Атака аколита"),
                    Button("Лечение [Мана 4]"),
                    Button("Святое свечение [Мана 3]"),
                ]],
                edit_date=stamp,
            ),
        )
        pending = controller.pending_combat_decision
        assert pending is not None

        await dispatch(
            controller,
            runtime,
            Message(
                21,
                "⚔️ Раунд 4\n"
                f"Чёрный волк использует {pending.decision.skill_name}\n"
                "Чёрный волк получает 20 урона",
                edit_date=stamp,
            ),
        )

        self.assertIs(controller.pending_combat_decision, pending)
        self.assertEqual(controller.combat_decisions, [])


class CombatCausalityHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_later_accepted_start_with_lower_message_id_begins_new_epoch(self) -> None:
        runtime = Runtime()
        controller = LegacyCombatController(runtime, character_name="Игрок")

        await dispatch(controller, runtime, Message(200, "Вы напали: Фонарщик"))
        await dispatch(controller, runtime, Message(100, "Вы напали: Пепельник"))

        self.assertEqual(controller.memory.target_name, "Пепельник")
        self.assertEqual(controller.status().enemy_names, ("Пепельник",))
        self.assertTrue(controller.status().active)

    async def test_lower_message_id_same_timestamp_uses_accepted_sequence(self) -> None:
        runtime = Runtime()
        controller = LegacyCombatController(runtime, character_name="Игрок")
        prepare_player_turn(
            controller,
            runtime,
            character_name="Игрок",
            target_name="Фонарщик",
        )
        stamp = runtime.clock
        await dispatch(
            controller,
            runtime,
            Message(
                42,
                "⚔️ Раунд 3\nХод Игрок\nМана: 12/12\nВыберите навык:",
                [[
                    Button("Атака аколита"),
                    Button("Лечение [Мана 4]"),
                    Button("Святое свечение [Мана 3]"),
                ]],
                edit_date=stamp,
            ),
        )
        pending = controller.pending_combat_decision
        assert pending is not None

        await dispatch(
            controller,
            runtime,
            Message(
                41,
                "⚔️ Раунд 4\n"
                f"Игрок использует {pending.decision.skill_name}\n"
                "Фонарщик получает 50 урона",
                edit_date=stamp,
            ),
        )

        self.assertIsNone(controller.pending_combat_decision)
        self.assertEqual(len(controller.combat_decisions), 1)

    async def test_earlier_sequence_cannot_create_epoch_after_newer_orphan_result(self) -> None:
        runtime = Runtime()
        controller = LegacyCombatController(runtime, character_name="Игрок")
        old_start = runtime.capture(Message(10, "Вы напали: Фонарщик"))
        newer_finish = runtime.capture(Message(20, "Бой завершён\nПобеда"))
        old_observation = controller.observe_message(old_start)
        finish_observation = controller.observe_message(newer_finish)
        assert old_observation is not None and finish_observation is not None

        await controller.handle_message(finish_observation)
        await controller.handle_message(old_observation)

        self.assertFalse(controller.status().active)
        self.assertIsNone(controller.memory.target_name)
        self.assertEqual(runtime.record_calls, 1)

    async def test_earlier_accepted_event_handled_late_cannot_replace_new_epoch(self) -> None:
        runtime = Runtime()
        controller = LegacyCombatController(runtime, character_name="Игрок")
        old_event = runtime.capture(Message(10, "Вы напали: Фонарщик"))
        new_event = runtime.capture(Message(20, "Вы напали: Пепельник"))
        old_observation = controller.observe_message(old_event)
        new_observation = controller.observe_message(new_event)
        assert old_observation is not None and new_observation is not None

        await controller.handle_message(new_observation)
        await controller.handle_message(old_observation)

        self.assertEqual(controller.memory.target_name, "Пепельник")
        self.assertEqual(controller.status().enemy_names, ("Пепельник",))


class CombatOutcomeLifecycleHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_distinct_source_revisions_reusing_message_id_both_record(self) -> None:
        runtime = Runtime()
        controller = LegacyCombatController(runtime, character_name="Игрок")
        stamp = runtime.clock

        await dispatch(
            controller,
            runtime,
            Message(10, "Вы напали: Фонарщик", edit_date=stamp),
        )
        first_finish = Message(
            10,
            "Бой завершён\nПобеда",
            edit_date=stamp + timedelta(seconds=1),
        )
        await dispatch(controller, runtime, first_finish)
        await dispatch(
            controller,
            runtime,
            Message(
                10,
                "Вы напали: Пепельник",
                edit_date=stamp + timedelta(seconds=2),
            ),
        )
        await dispatch(controller, runtime, first_finish)
        self.assertEqual(controller.memory.target_name, "Пепельник")
        self.assertTrue(controller.status().active)
        self.assertEqual(runtime.record_calls, 1)

        second_finish = Message(
            10,
            "Бой завершён\nПобеда",
            edit_date=stamp + timedelta(seconds=3),
        )
        await dispatch(controller, runtime, second_finish)
        await dispatch(controller, runtime, second_finish)

        self.assertEqual(runtime.record_calls, 2)
        self.assertEqual(len(runtime.outcomes), 2)
        self.assertEqual(len(runtime.victories), 2)
        self.assertEqual(
            len({outcome.source_event_id for outcome in runtime.outcomes.values()}),
            2,
        )
        self.assertEqual(
            {outcome.source_message_id for outcome in runtime.outcomes.values()},
            {10},
        )

    async def test_optional_knowledge_failure_cannot_block_defeat_lifecycle(self) -> None:
        runtime = Runtime()
        controller = LegacyCombatController(runtime, character_name="Игрок")
        await dispatch(controller, runtime, Message(1, "Вы напали: Фонарщик"))
        controller.activate_combat_profile(400)

        with patch.object(
            runtime,
            "save_combat_knowledge",
            new=AsyncMock(side_effect=OSError("optional store unavailable")),
        ):
            await dispatch(controller, runtime, Message(2, "Бой завершён\nПоражение"))

        self.assertFalse(controller.status().active)
        self.assertIsNone(controller.memory.target_name)
        self.assertEqual(runtime.recoveries, ["Фонарщик"])
        self.assertEqual(runtime.record_calls, 1)
        self.assertTrue(
            any("боев" in message.casefold() and "памят" in message.casefold()
                for message in runtime.logs)
        )

        await controller.persist()
        self.assertEqual(len(runtime.saves), 1)
