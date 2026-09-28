from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime

from combat_learning import (
    ActionProjection,
    ShadowCombatPlan,
    build_shadow_plan,
    select_combat_planner_decision,
)
from combat_round import parse_combat_round
from combat_strategy import (
    CombatDecision,
    CombatMemory,
    ObservedRange,
    SkillTarget,
    choose_combat_action,
)
from skills import HEALING_MANA_RESERVE, enough_health_for_battle, parse_skill_button
from tests.fog_test_fixtures import CHARACTER, FakeMessage


class SkillTests(unittest.TestCase):
    def test_magic_blocked_and_explicit_low_mana_buttons_are_unavailable(self) -> None:
        blocked = parse_skill_button("⏳ Лечение [Мана 4] (магия заблокирована)")
        low_mana = parse_skill_button("⏳ Обновление [Мана 4] (mana:3/4)")

        self.assertFalse(blocked.available)
        self.assertFalse(blocked.can_cast(12))
        self.assertFalse(low_mana.available)

    def test_battle_health_requirement_supports_50_and_100_percent(self) -> None:
        self.assertTrue(enough_health_for_battle(423, 845, 50))
        self.assertFalse(enough_health_for_battle(422, 845, 50))
        self.assertTrue(enough_health_for_battle(845, 845, 100))
        self.assertFalse(enough_health_for_battle(844, 845, 100))

    def test_holy_light_keeps_healing_mana_reserve(self) -> None:
        message = FakeMessage(
            "Мана: 6/11",
            [["Святое свечение (-3 маны)"], ["Атака аколита"]],
        )
        decision = choose_combat_action(
            message,
            memory=CombatMemory(target_name="Фонарщик", enemy_current_hp=800),
            current_hp=800,
            max_hp=845,
            heal_threshold=300,
        )
        self.assertEqual(HEALING_MANA_RESERVE, 4)
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.skill_name, "атака аколита")

    def test_healing_has_priority_below_threshold(self) -> None:
        message = FakeMessage(
            "Мана: 11/11",
            [["Лечение (-4 маны)"], ["Святое свечение (-3 маны)"]],
        )
        decision = choose_combat_action(
            message,
            memory=CombatMemory(target_name="Противник", enemy_current_hp=800),
            current_hp=180,
            max_hp=845,
            heal_threshold=300,
        )
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.skill_name, "лечение")
        self.assertIs(decision.target, SkillTarget.SELF)
        self.assertTrue(decision.urgent)


class CombatStrategyTests(unittest.TestCase):
    @staticmethod
    def unsafe_race_memory() -> CombatMemory:
        memory = CombatMemory(target_name="Фонарщик", enemy_current_hp=800)
        memory.incoming_damage.add(60)
        memory.incoming_damage.add(62)
        memory.outgoing_damage.setdefault("лечение", ObservedRange()).add(90)
        memory.outgoing_damage["лечение"].add(92)
        return memory

    def test_available_treatment_attacks_enemy_when_safe(self) -> None:
        memory = CombatMemory()
        memory.begin("Фонарщик", "Фонарщик\n1025❤️ из 1025❤️")
        memory.confirm_treatment_enemy()
        memory.enemy_current_hp = 552
        decision = choose_combat_action(
            FakeMessage("Мана: 8/12", [["Лечение [Мана 4]"], ["Атака аколита"]]),
            memory=memory,
            current_hp=493,
            max_hp=780,
            heal_threshold=300,
        )

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.skill_name, "лечение")
        self.assertIs(decision.target, SkillTarget.ENEMY)

    def test_treatment_is_not_used_as_attack_for_unconfirmed_living_enemy(self) -> None:
        memory = CombatMemory(
            target_name="Черная мушка",
            enemy_current_hp=88,
            enemy_max_hp=475,
            renewal_turns=1,
        )
        for value in (35, 38):
            memory.incoming_damage.add(value)
        for value in (2, 44):
            memory.outgoing_damage.setdefault("атака аколита", ObservedRange()).add(value)
        for value in (4, 67):
            memory.outgoing_damage.setdefault("святое свечение", ObservedRange()).add(value)
        memory.renewal_healing.add(40)
        memory.direct_healing.add(124)

        decision = choose_combat_action(
            FakeMessage(
                "Мана: 6/12",
                [
                    ["Лечение [Мана 4]"],
                    ["Обновление [Мана 4] · CD: 1"],
                    ["Святое свечение [Мана 3]"],
                    ["Атака аколита"],
                ],
            ),
            memory=memory,
            current_hp=694,
            max_hp=780,
            heal_threshold=500,
        )

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.skill_name, "атака аколита")
        self.assertIs(decision.target, SkillTarget.ENEMY)

    def test_revoked_treatment_target_clears_stale_damage_capability(self) -> None:
        memory = CombatMemory(target_name="Изменившийся моб")
        memory.confirm_treatment_enemy()
        memory.outgoing_damage["лечение"] = ObservedRange()
        memory.outgoing_damage["лечение"].add(100)

        memory.revoke_treatment_enemy()

        self.assertFalse(memory.treatment_can_target_enemy())
        self.assertNotIn("лечение", memory.outgoing_damage)

    def test_treatment_targets_player_when_next_hit_is_dangerous(self) -> None:
        memory = CombatMemory(target_name="Фонарщик", enemy_current_hp=552)
        decision = choose_combat_action(
            FakeMessage("Мана: 8/12", [["Лечение [Мана 4]"], ["Атака аколита"]]),
            memory=memory,
            current_hp=110,
            max_hp=780,
            heal_threshold=300,
        )

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.skill_name, "лечение")
        self.assertIs(decision.target, SkillTarget.SELF)

    def test_unsafe_race_bootstraps_unknown_self_healing(self) -> None:
        memory = self.unsafe_race_memory()
        decision = choose_combat_action(
            FakeMessage("Мана: 4/12", [["Лечение [Мана 4]"], ["Атака аколита"]]),
            memory=memory,
            current_hp=400,
            max_hp=780,
            heal_threshold=500,
        )

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.skill_name, "лечение")
        self.assertIs(decision.target, SkillTarget.SELF)
        self.assertIn("уточнит его силу", decision.reason)

    def test_known_self_healing_can_improve_an_unsafe_race(self) -> None:
        memory = self.unsafe_race_memory()
        memory.direct_healing.add(124)
        decision = choose_combat_action(
            FakeMessage("Мана: 4/12", [["Лечение [Мана 4]"], ["Атака аколита"]]),
            memory=memory,
            current_hp=400,
            max_hp=780,
            heal_threshold=500,
        )

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.skill_name, "лечение")
        self.assertIs(decision.target, SkillTarget.SELF)
        self.assertIn("увеличивает запас ходов", decision.reason)

    def test_renewal_is_cast_before_health_becomes_critical(self) -> None:
        memory = CombatMemory(target_name="Фонарщик", enemy_current_hp=552)
        memory.incoming_damage.add(57)
        memory.incoming_damage.add(60)
        memory.outgoing_damage.setdefault("святое свечение", ObservedRange()).add(80)
        memory.outgoing_damage["святое свечение"].add(82)
        memory.renewal_healing.add(40)
        decision = choose_combat_action(
            FakeMessage(
                "Мана: 8/12",
                [["Обновление [Мана 4]"], ["Святое свечение [Мана 3]"], ["Атака аколита"]],
            ),
            memory=memory,
            current_hp=430,
            max_hp=780,
            heal_threshold=300,
        )

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.skill_name, "обновление")
        self.assertIs(decision.target, SkillTarget.SELF)

    def test_lethal_holy_light_ignores_mana_reserve(self) -> None:
        memory = CombatMemory(target_name="Фонарщик", enemy_current_hp=74)
        memory.outgoing_damage.setdefault("святое свечение", ObservedRange()).add(77)
        memory.outgoing_damage["святое свечение"].add(80)
        decision = choose_combat_action(
            FakeMessage("Мана: 4/12", [["Святое свечение [Мана 3]"], ["Атака аколита"]]),
            memory=memory,
            current_hp=200,
            max_hp=780,
            heal_threshold=300,
        )

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.skill_name, "святое свечение")
        self.assertIs(decision.target, SkillTarget.ENEMY)
        self.assertTrue(decision.urgent)

    def test_delayed_renewal_is_not_used_when_the_next_hit_is_lethal(self) -> None:
        memory = CombatMemory(
            target_name="Пепельник",
            enemy_current_hp=9,
            enemy_max_hp=920,
        )
        for value in (37, 51, 55, 61):
            memory.incoming_damage.add(value)
        memory.outgoing_damage.setdefault("атака аколита", ObservedRange()).add(42)
        memory.renewal_healing.add(48)
        memory.renewal_healing.add(48)

        decision = choose_combat_action(
            FakeMessage(
                "Мана: 4/13",
                [
                    ["Атака аколита"],
                    ["Святое свечение [Мана 3] (CD: 1)"],
                    ["Лечение [Мана 4] (CD: 2)"],
                    ["Обновление [Мана 4]"],
                ],
            ),
            memory=memory,
            current_hp=48,
            max_hp=830,
            heal_threshold=415,
        )

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.skill_name, "атака аколита")
        self.assertIs(decision.target, SkillTarget.ENEMY)
        self.assertTrue(decision.urgent)
        self.assertIn("отложенное лечение не успеет", decision.reason)

    def test_critical_hit_does_not_raise_guaranteed_damage(self) -> None:
        memory = CombatMemory(target_name="Фонарщик", enemy_current_hp=120)
        memory.observe(
            """🪬🧍Kombat использует Лечение
Фонарщик получает 160 урона ❗️Мощный крит""",
            CHARACTER,
        )
        memory.observe(
            """🪬🧍Kombat использует Лечение
Фонарщик получает 158 урона 💢 крит""",
            CHARACTER,
        )

        self.assertEqual(memory.damage_floor("лечение"), 0)

    def test_periodic_enemy_damage_is_not_attributed_to_player_skill(self) -> None:
        memory = CombatMemory(target_name="Фонарщик")
        for direct in (32, 34):
            memory.observe(
                f"""🪬🧍Kombat использует Атака аколита
Фонарщик получает {direct} урона
Фонарщик получает 15 урона · Горение""",
                CHARACTER,
            )

        observed = memory.outgoing_damage["атака аколита"]
        self.assertEqual((observed.minimum, observed.maximum, observed.samples), (32, 34, 2))
        self.assertEqual(memory.damage_floor("атака аколита"), 32)

    def test_critical_incoming_hits_have_separate_risk_model(self) -> None:
        memory = CombatMemory(target_name="Фонарщик")
        for _ in range(9):
            memory.observe("🪬🧍Kombat получает 60 урона", CHARACTER)
        memory.observe("🪬🧍Kombat получает 107 урона 💢 крит", CHARACTER)

        self.assertEqual(memory.incoming_damage.samples, 9)
        self.assertEqual(memory.critical_incoming_damage.samples, 1)
        self.assertEqual(memory.critical_incoming_rate(), 0.1)
        self.assertEqual(memory.expected_incoming(), 72)
        self.assertEqual(memory.predicted_incoming(), 134)

    def test_enemy_turn_forecast_uses_stable_cooldown_cycle(self) -> None:
        memory = CombatMemory(target_name="Фонарщик", enemy_current_hp=768)
        for value in (30, 32):
            memory.outgoing_damage.setdefault("атака аколита", ObservedRange()).add(value)
        for value in (80, 82):
            memory.outgoing_damage.setdefault("святое свечение", ObservedRange()).add(value)
        memory.skill_cooldowns["святое свечение"] = 1

        basic_only = choose_combat_action(
            FakeMessage("Мана: 4/12", [["Атака аколита"]]),
            memory=memory,
            current_hp=700,
            max_hp=780,
            heal_threshold=300,
        )
        with_holy = choose_combat_action(
            FakeMessage("Мана: 8/12", [["Святое свечение [Мана 3]"], ["Атака аколита"]]),
            memory=memory,
            current_hp=700,
            max_hp=780,
            heal_threshold=300,
        )

        self.assertEqual(memory.sustainable_damage_floor(), 55)
        self.assertIsNotNone(basic_only)
        self.assertIsNotNone(with_holy)
        assert basic_only is not None and with_holy is not None
        self.assertIn("до победы≈14 ход.", basic_only.reason)
        self.assertIn("до победы≈14 ход.", with_holy.reason)

    def test_shadow_planner_rejects_partial_self_heal_when_enemy_is_lethal(self) -> None:
        memory = CombatMemory(
            target_name="Черная мушка",
            enemy_current_hp=88,
            enemy_max_hp=475,
        )
        for value in (35, 38, 36, 39):
            memory.incoming_damage.add(value)
        for value in (43, 44, 45):
            memory.outgoing_damage.setdefault("атака аколита", ObservedRange()).add(value)
        for value in (94, 97, 96):
            memory.outgoing_damage.setdefault("лечение", ObservedRange()).add(value)
        memory.direct_healing.add(130)
        memory.direct_healing.add(130)
        memory.confirm_treatment_enemy("Черная мушка")
        message = FakeMessage(
            "🎯 Раунд 15\nХод Kombat\n🔷 Мана: 6/12\n⏳ Осталось: 23 сек",
            [["Лечение [Мана 4]"], ["Атака аколита"]],
        )
        round_state = parse_combat_round(
            message.raw_text,
            [button.text for row in message.buttons for button in row],
        )
        self.assertIsNotNone(round_state)
        plan = build_shadow_plan(
            message,
            memory=memory,
            current_hp=694,
            max_hp=780,
            executed=CombatDecision("лечение", SkillTarget.SELF, "старое решение"),
            round_state=round_state,
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertTrue(plan.confident)
        self.assertFalse(plan.agrees)
        self.assertEqual(plan.recommendation.skill_name, "лечение")
        self.assertIs(plan.recommendation.target, SkillTarget.ENEMY)
        self.assertEqual(plan.candidates[0].projected_enemy_hp, 0)

    def test_shadow_planner_prefers_the_only_safe_three_turn_action(self) -> None:
        memory = CombatMemory(
            target_name="Пепельник",
            enemy_current_hp=80,
            enemy_max_hp=920,
        )
        for _ in range(4):
            memory.incoming_damage.add(100)
        for value in (40, 42):
            memory.outgoing_damage.setdefault("атака аколита", ObservedRange()).add(value)
        memory.direct_healing.add(200)
        memory.direct_healing.add(200)
        message = FakeMessage(
            "Мана: 4/13",
            [["Лечение [Мана 4]"], ["Атака аколита"]],
        )

        plan = build_shadow_plan(
            message,
            memory=memory,
            current_hp=130,
            max_hp=830,
            executed=CombatDecision("атака аколита", SkillTarget.ENEMY, "старое решение"),
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertTrue(plan.confident)
        self.assertEqual(plan.recommendation.skill_name, "лечение")
        self.assertIs(plan.recommendation.target, SkillTarget.SELF)
        self.assertFalse(plan.candidates[0].unsafe)
        self.assertTrue(
            next(
                candidate
                for candidate in plan.candidates
                if candidate.skill_name == "атака аколита"
            ).unsafe
        )

    def test_shadow_planner_is_not_confident_when_every_action_projects_death(
        self,
    ) -> None:
        memory = CombatMemory(
            target_name="Пепельник",
            enemy_current_hp=500,
            enemy_max_hp=920,
        )
        for _ in range(4):
            memory.incoming_damage.add(100)
        for value in (40, 42):
            memory.outgoing_damage.setdefault("атака аколита", ObservedRange()).add(value)
        memory.direct_healing.add(20)
        memory.direct_healing.add(20)
        executed = CombatDecision("лечение", SkillTarget.SELF, "старое решение")

        plan = build_shadow_plan(
            FakeMessage(
                "Мана: 4/13",
                [["Лечение [Мана 4]"], ["Атака аколита"]],
            ),
            memory=memory,
            current_hp=80,
            max_hp=830,
            executed=executed,
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertFalse(plan.confident)
        self.assertIs(plan.recommendation, executed)
        self.assertTrue(all(candidate.unsafe for candidate in plan.candidates))
        self.assertFalse(plan.as_payload()["has_safe_candidate"])
        self.assertIn("безопасного плана", plan.format_log())

    def test_shadow_planner_does_not_spend_mana_without_tempo_gain(self) -> None:
        memory = CombatMemory(
            target_name="Крапива-жгучка",
            enemy_current_hp=165,
            enemy_max_hp=165,
        )
        for value in (21, 22, 23, 24):
            memory.incoming_damage.add(value)
        for value in (55, 57):
            memory.outgoing_damage.setdefault("атака аколита", ObservedRange()).add(value)
        for value in (85, 88):
            memory.outgoing_damage.setdefault("святое свечение", ObservedRange()).add(value)
        message = FakeMessage(
            "🔷 Мана: 12/12",
            [["Святое свечение [Мана 3]"], ["Атака аколита"]],
        )

        plan = build_shadow_plan(
            message,
            memory=memory,
            current_hp=700,
            max_hp=755,
            executed=CombatDecision("атака аколита", SkillTarget.ENEMY, "экономия маны"),
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertTrue(plan.confident)
        self.assertTrue(plan.agrees)
        holy = next(
            candidate for candidate in plan.candidates if candidate.skill_name == "святое свечение"
        )
        self.assertTrue(holy.mana_dominated)

    def test_shadow_planner_spends_mana_when_it_prevents_an_enemy_hit(self) -> None:
        memory = CombatMemory(
            target_name="Крапива-жгучка",
            enemy_current_hp=85,
            enemy_max_hp=165,
        )
        for value in (21, 22, 23, 24):
            memory.incoming_damage.add(value)
        for value in (55, 57):
            memory.outgoing_damage.setdefault("атака аколита", ObservedRange()).add(value)
        for value in (85, 88):
            memory.outgoing_damage.setdefault("святое свечение", ObservedRange()).add(value)
        message = FakeMessage(
            "🔷 Мана: 12/12",
            [["Святое свечение [Мана 3]"], ["Атака аколита"]],
        )

        plan = build_shadow_plan(
            message,
            memory=memory,
            current_hp=700,
            max_hp=755,
            executed=CombatDecision("атака аколита", SkillTarget.ENEMY, "старое решение"),
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertTrue(plan.confident)
        self.assertEqual(plan.recommendation.skill_name, "святое свечение")
        self.assertFalse(plan.candidates[0].mana_dominated)

    def test_shadow_planner_keeps_full_tempo_beyond_horizon(self) -> None:
        memory = CombatMemory(
            target_name="Туманный Жгун",
            enemy_current_hp=745,
            enemy_max_hp=745,
        )
        for value in (45, 48, 50, 52):
            memory.incoming_damage.add(value)
        for value in (26, 28):
            memory.outgoing_damage.setdefault("атака аколита", ObservedRange()).add(value)
        for value in (62, 64):
            memory.outgoing_damage.setdefault("святое свечение", ObservedRange()).add(value)

        plan = build_shadow_plan(
            FakeMessage(
                "🔷 Мана: 12/12",
                [["Святое свечение [Мана 3]"], ["Атака аколита"]],
            ),
            memory=memory,
            current_hp=700,
            max_hp=755,
            executed=CombatDecision("святое свечение", SkillTarget.ENEMY, "лучший урон"),
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        holy = next(
            candidate for candidate in plan.candidates if candidate.skill_name == "святое свечение"
        )
        basic = next(
            candidate for candidate in plan.candidates if candidate.skill_name == "атака аколита"
        )
        self.assertLess(holy.expected_enemy_hits, basic.expected_enemy_hits)
        self.assertFalse(holy.mana_dominated)
        self.assertEqual(plan.recommendation.skill_name, "святое свечение")

    def test_shadow_planner_proves_ash_bell_is_not_survivable_with_known_stats(
        self,
    ) -> None:
        memory = CombatMemory(
            target_name="Колокол пепла",
            enemy_current_hp=1400,
            enemy_max_hp=1400,
        )
        for value in (88, 90, 93, 104):
            memory.incoming_damage.add(value)
        for skill_name, values in {
            "атака аколита": (40, 42),
            "святое свечение": (42, 44),
        }.items():
            for value in values:
                memory.outgoing_damage.setdefault(skill_name, ObservedRange()).add(value)
        for value in (144, 144):
            memory.direct_healing.add(value)
        for value in (47, 47):
            memory.renewal_healing.add(value)
        executed = CombatDecision(
            "святое свечение",
            SkillTarget.ENEMY,
            "прежнее решение",
        )

        plan = build_shadow_plan(
            FakeMessage(
                "🔷 Мана: 12/12",
                [
                    ["Лечение [Мана 4]"],
                    ["Обновление [Мана 4]"],
                    ["Святое свечение [Мана 3]"],
                    ["Атака аколита"],
                ],
            ),
            memory=memory,
            current_hp=750,
            max_hp=750,
            executed=executed,
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.horizon, 24)
        self.assertFalse(plan.has_safe_candidate)
        self.assertFalse(plan.confident)
        self.assertIs(plan.recommendation, executed)
        self.assertTrue(all(candidate.unsafe for candidate in plan.candidates))
        self.assertIn("обновление→self", plan.candidates[0].sequence)
        self.assertIn("лечение→self", plan.candidates[0].sequence)

    def test_guarded_planner_only_replaces_an_unsafe_baseline(self) -> None:
        memory = CombatMemory(
            target_name="Пепельник",
            enemy_current_hp=80,
            enemy_max_hp=920,
        )
        for _ in range(4):
            memory.incoming_damage.add(100)
        for value in (40, 42):
            memory.outgoing_damage.setdefault("атака аколита", ObservedRange()).add(value)
        for value in (200, 200):
            memory.direct_healing.add(value)
        baseline = CombatDecision(
            "атака аколита",
            SkillTarget.ENEMY,
            "прежнее решение",
        )
        plan = build_shadow_plan(
            FakeMessage(
                "🔷 Мана: 4/13",
                [["Лечение [Мана 4]"], ["Атака аколита"]],
            ),
            memory=memory,
            current_hp=130,
            max_hp=830,
            executed=baseline,
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertTrue(plan.confident)
        self.assertIs(select_combat_planner_decision(plan, "shadow"), baseline)
        guarded = select_combat_planner_decision(plan, "guarded")
        self.assertEqual(guarded.skill_name, "лечение")
        self.assertIs(guarded.target, SkillTarget.SELF)
        self.assertIs(
            select_combat_planner_decision(plan, "active"),
            plan.recommendation,
        )
        controlled = plan.with_execution(guarded, mode="guarded")
        self.assertTrue(controlled.controls_action)
        self.assertEqual(controlled.as_payload()["control_mode"], "guarded")

    def test_guarded_planner_does_not_heal_only_for_a_larger_hp_reserve(
        self,
    ) -> None:
        baseline = CombatDecision(
            "атака аколита",
            SkillTarget.ENEMY,
            "безопасная атака",
        )
        healing = CombatDecision(
            "лечение",
            SkillTarget.SELF,
            "больше запаса HP",
        )

        def projection(
            decision: CombatDecision,
            *,
            survival_margin: int,
        ) -> ActionProjection:
            return ActionProjection(
                skill_name=decision.skill_name,
                target=decision.target,
                mana_cost=0 if decision is baseline else 4,
                score=float(survival_margin),
                projected_player_hp=500,
                projected_enemy_hp=300,
                expected_enemy_hits=3,
                projected_turns=4,
                projected_mana=8,
                survival_margin=survival_margin,
                sequence=(f"{decision.skill_name}→{decision.target.value}",),
                unsafe=False,
                mana_dominated=False,
                effect_samples=4,
                unknown_actions=0,
                reason="test",
            )

        plan = ShadowCombatPlan(
            horizon=6,
            recommendation=healing,
            baseline=baseline,
            executed=baseline,
            confident=True,
            candidates=(
                projection(healing, survival_margin=200),
                projection(baseline, survival_margin=20),
            ),
        )

        self.assertIs(select_combat_planner_decision(plan, "guarded"), baseline)
        self.assertIs(select_combat_planner_decision(plan, "active"), healing)

    def test_real_round_updates_local_damage_model(self) -> None:
        memory = CombatMemory()
        memory.begin("Фонарщик", "Вы напали:\nФонарщик\n1025❤️ из 1025❤️")
        memory.observe(
            """⚔️ Раунд 1
🪬🧍Kombat использует Лечение
Фонарщик получает 90 урона
Фонарщик
❤️ 935/1025
Фонарщик атакует 🪬🧍Kombat
🪬🧍Kombat получает 57 урона
🪬🧍Kombat
❤️ 723/780""",
            CHARACTER,
        )

        self.assertEqual(memory.enemy_current_hp, 935)
        self.assertEqual(memory.outgoing_damage["лечение"].minimum, 90)
        self.assertEqual(memory.damage_floor("лечение"), 0)
        self.assertEqual(memory.incoming_damage.maximum, 57)
        self.assertEqual(memory.expected_incoming(), 69)
        self.assertEqual(memory.predicted_incoming(), 77)

    def test_finishing_damage_does_not_lower_learned_skill_floor(self) -> None:
        memory = CombatMemory(target_name="Черная мушка", enemy_current_hp=2)
        memory.outgoing_damage["атака аколита"] = ObservedRange()
        memory.outgoing_damage["атака аколита"].add(42)
        memory.outgoing_damage["атака аколита"].add(44)
        memory.pending_skill = "атака аколита"

        memory.observe(
            """⚔️ Раунд 18
🪬🧍Kombat использует Атака аколита
Черная мушка получает 2 урона
Черная мушка
❤️ 0/475""",
            CHARACTER,
        )

        observed = memory.outgoing_damage["атака аколита"]
        self.assertEqual((observed.minimum, observed.samples), (42, 2))

    def test_capped_direct_heal_does_not_lower_known_healing_power(self) -> None:
        memory = CombatMemory(target_name="Черная мушка")
        memory.direct_healing.add(124)
        memory.knowledge.add_direct_healing(124)

        memory.observe(
            """⚔️ Раунд 15
Левая сторона
🪬🧍Kombat восстанавливает 40 HP · renew
🪬🧍Kombat использует Лечение
🪬🧍Kombat восстанавливает 46 HP
🪬🧍Kombat
❤️ 780/780
✦ Обновление · 1 ход""",
            CHARACTER,
        )

        self.assertEqual(memory.renewal_tick(), 40)
        self.assertEqual(memory.direct_heal(), 124)
        self.assertEqual(memory.knowledge.direct_healing, [124])

    def test_unseen_monster_has_no_invented_damage(self) -> None:
        tier_one = CombatMemory(target_name="Слабый моб")
        tier_three = CombatMemory(target_name="Сильный моб")

        self.assertIsNone(tier_one.expected_incoming())
        self.assertIsNone(tier_one.predicted_incoming())
        self.assertIsNone(tier_three.expected_incoming())
        self.assertIsNone(tier_three.predicted_incoming())

    def test_incoming_forecast_adapts_to_observed_monster_damage(self) -> None:
        weak = CombatMemory(target_name="Слабый моб")
        weak.incoming_damage.add(10)
        weak.incoming_damage.add(12)
        strong = CombatMemory(target_name="Сильный моб")
        strong.incoming_damage.add(100)
        strong.incoming_damage.add(120)

        self.assertEqual(weak.expected_incoming(), 13)
        self.assertEqual(weak.predicted_incoming(), 15)
        self.assertEqual(strong.expected_incoming(), 127)
        self.assertEqual(strong.predicted_incoming(), 150)

    def test_recent_damage_is_reused_only_for_the_same_monster(self) -> None:
        memory = CombatMemory()
        memory.begin("Слабый моб")
        memory.observe("🪬🧍Kombat получает 12 урона", CHARACTER)

        memory.begin("Слабый моб")
        self.assertEqual(memory.expected_incoming(), 15)
        memory.begin("Другой моб")
        self.assertIsNone(memory.expected_incoming())

    def test_threshold_is_soft_when_damage_race_is_safe(self) -> None:
        memory = CombatMemory(target_name="Фонарщик", enemy_current_hp=300)
        memory.confirm_treatment_enemy()
        decision = choose_combat_action(
            FakeMessage("Мана: 8/12", [["Лечение [Мана 4]"], ["Атака аколита"]]),
            memory=memory,
            current_hp=400,
            max_hp=780,
            heal_threshold=500,
        )

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.skill_name, "лечение")
        self.assertIs(decision.target, SkillTarget.ENEMY)

    def test_direct_heal_replaces_renewal_when_renewal_cannot_fix_forecast(self) -> None:
        memory = CombatMemory(target_name="Фонарщик", enemy_current_hp=653)
        memory.incoming_damage.add(57)
        memory.incoming_damage.add(60)
        memory.outgoing_damage.setdefault("святое свечение", ObservedRange()).add(80)
        memory.outgoing_damage["святое свечение"].add(82)
        memory.renewal_healing.add(40)
        decision = choose_combat_action(
            FakeMessage(
                "Мана: 8/12",
                [
                    ["Обновление [Мана 4]"],
                    ["Лечение [Мана 4]"],
                    ["Святое свечение [Мана 3]"],
                    ["Атака аколита"],
                ],
            ),
            memory=memory,
            current_hp=461,
            max_hp=780,
            heal_threshold=500,
        )

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.skill_name, "лечение")
        self.assertIs(decision.target, SkillTarget.SELF)

    def test_high_soft_threshold_does_not_force_early_healing(self) -> None:
        memory = CombatMemory(target_name="Фонарщик", enemy_current_hp=855)
        decision = choose_combat_action(
            FakeMessage(
                "Мана: 8/12",
                [
                    ["Обновление [Мана 4]"],
                    ["Лечение [Мана 4]"],
                    ["Святое свечение [Мана 3]"],
                    ["Атака аколита"],
                ],
            ),
            memory=memory,
            current_hp=590,
            max_hp=780,
            heal_threshold=500,
        )

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.skill_name, "святое свечение")

    def test_periodic_damage_and_renewal_are_included_in_forecast(self) -> None:
        memory = CombatMemory(target_name="Пепельник", enemy_current_hp=500)
        memory.observe(
            """🪬🧍Kombat получает 50 урона
🪬🧍Kombat получает 14 урона · Горение
🪬🧍Kombat восстанавливает 40 HP · renew
🪬🧍Kombat
❤️ 300/870
✦ Обновление · 2 хода
🔥 Горение · 2 хода""",
            CHARACTER,
        )

        self.assertEqual(memory.renewal_turns, 2)
        self.assertEqual(memory.renewal_tick(), 40)
        self.assertEqual(memory.periodic_damage_turns, 2)
        self.assertEqual(memory.predicted_incoming(), 82)
        self.assertEqual(memory.predicted_incoming(after_current_tick=True), 82)
        memory.periodic_damage_turns = 1
        self.assertEqual(memory.predicted_incoming(after_current_tick=True), 68)

    def test_real_round_learns_direct_and_renewal_healing(self) -> None:
        memory = CombatMemory(target_name="Фонарщик")
        memory.observe(
            """⚔️ Раунд 29
Левая сторона
🪬🧍Kombat восстанавливает 40 HP · renew
🪬🧍Kombat использует Лечение
🪬🧍Kombat восстанавливает 124 HP
🪬🧍Kombat
❤️ 203/780
✦ Обновление · 2 хода""",
            CHARACTER,
        )

        self.assertEqual(memory.renewal_tick(), 40)
        self.assertEqual(memory.direct_heal(), 124)
        self.assertEqual(memory.renewal_turns, 2)


@dataclass(frozen=True)
class ForecastButton:
    text: str


@dataclass(frozen=True)
class ForecastMessage:
    buttons: tuple[tuple[ForecastButton, ...], ...]
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
        message = ForecastMessage(
            ((ForecastButton("Атака аколита"), ForecastButton("Лечение [Мана 4]")),)
        )
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
        message = ForecastMessage(((ForecastButton("Атака аколита"),),))
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
        message = ForecastMessage(((ForecastButton("Атака аколита"),),))
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
        message = ForecastMessage(
            ((ForecastButton("Атака аколита"), ForecastButton("Лечение [Мана 4]")),)
        )
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
