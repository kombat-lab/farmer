from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime

from combat_learning import build_shadow_plan, select_combat_planner_decision
from combat_strategy import CombatMemory, ObservedRange, SkillTarget, choose_combat_action


@dataclass(frozen=True)
class Button:
    text: str


@dataclass(frozen=True)
class Message:
    buttons: tuple[tuple[Button, ...], ...]
    raw_text: str | None = "Мана: 12/12"
    id: int = 1
    edit_date: datetime | None = None

    async def click(self, row: int, column: int) -> object:
        raise AssertionError("A combat forecast must not perform Telegram I/O")


def prepared_memory(enemy_hp: int = 180) -> CombatMemory:
    memory = CombatMemory(target_name="Фонарщик", enemy_current_hp=enemy_hp)
    for _ in range(4):
        memory.incoming_damage.add(20)
    for _ in range(2):
        memory.outgoing_damage.setdefault("атака аколита", ObservedRange()).add(60)
        memory.direct_healing.add(80)
    return memory


class CombatTickRegressions(unittest.TestCase):
    def test_active_planner_preserves_emergency_heal_for_last_poison_tick(self) -> None:
        memory = prepared_memory(enemy_hp=100)
        memory.periodic_damage = 70
        memory.periodic_damage_turns = 1
        message = Message(((Button("Атака аколита"), Button("Лечение [Мана 4]")),))
        baseline = choose_combat_action(
            message, memory=memory, current_hp=90, max_hp=200, heal_threshold=80
        )
        assert baseline is not None
        self.assertEqual((baseline.skill_name, baseline.target), ("лечение", SkillTarget.SELF))
        plan = build_shadow_plan(
            message, memory=memory, current_hp=90, max_hp=200, executed=baseline
        )
        assert plan is not None
        attack = next(item for item in plan.candidates if item.skill_name == "атака аколита")
        self.assertTrue(attack.unsafe)
        self.assertEqual(attack.projected_player_hp, 0)
        active = select_combat_planner_decision(plan, "active")
        self.assertEqual((active.skill_name, active.target), ("лечение", SkillTarget.SELF))

    def test_each_remaining_poison_tick_is_charged_once_then_expires(self) -> None:
        message = Message(((Button("Атака аколита"),),))
        for turns in range(6):
            with self.subTest(turns=turns):
                memory = prepared_memory()
                memory.periodic_damage = 17
                memory.periodic_damage_turns = turns
                baseline = choose_combat_action(
                    message, memory=memory, current_hp=700, max_hp=1000, heal_threshold=300
                )
                assert baseline is not None
                plan = build_shadow_plan(
                    message, memory=memory, current_hp=700, max_hp=1000, executed=baseline
                )
                assert plan is not None
                result = plan.candidates[0]
                # Three attacks finish the enemy; only two replies occur.
                self.assertEqual(result.projected_turns, 3)
                self.assertEqual(result.projected_player_hp, 700 - 2 * 22 - turns * 17)
                self.assertFalse(result.unsafe)

    def test_pending_heal_is_capped_before_pending_poison(self) -> None:
        message = Message(((Button("Атака аколита"),),))
        memory = prepared_memory()
        memory.periodic_damage = 17
        memory.periodic_damage_turns = 1
        memory.renewal_healing.add(40)
        memory.renewal_turns = 1
        baseline = choose_combat_action(
            message, memory=memory, current_hp=995, max_hp=1000, heal_threshold=300
        )
        assert baseline is not None
        plan = build_shadow_plan(
            message, memory=memory, current_hp=995, max_hp=1000, executed=baseline
        )
        assert plan is not None
        self.assertEqual(plan.candidates[0].projected_player_hp, 1000 - 17 - 2 * 22)

    def test_lethal_pending_poison_cannot_be_undone_by_a_simulated_heal(self) -> None:
        message = Message(((Button("Атака аколита"), Button("Лечение [Мана 4]")),))
        memory = prepared_memory()
        memory.periodic_damage = 70
        memory.periodic_damage_turns = 1
        baseline = choose_combat_action(
            message, memory=memory, current_hp=70, max_hp=200, heal_threshold=80
        )
        assert baseline is not None
        plan = build_shadow_plan(
            message, memory=memory, current_hp=70, max_hp=200, executed=baseline
        )
        assert plan is not None
        self.assertTrue(all(item.unsafe for item in plan.candidates))
        self.assertTrue(all(item.projected_player_hp == 0 for item in plan.candidates))
        self.assertFalse(plan.confident)
