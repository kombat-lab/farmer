from __future__ import annotations

import unittest

from combat_round import CombatSide, parse_combat_round
from combat_strategy import (
    CombatDecision,
    CombatMemory,
    SkillTarget,
    build_decision_trace,
    choose_combat_action,
    is_periodic_effect,
    resolve_decision_trace,
)
from targeting import select_combat_target
from tests.fog_test_fixtures import CHARACTER, FakeMessage


class CombatRoundModelTests(unittest.TestCase):
    def test_failed_skill_is_parsed_and_clears_pending_action(self) -> None:
        parsed = parse_combat_round("⚔️ Раунд 8\nKombat: Лечение (неудача: магия заблокирована)")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.failed_skill_uses[0].skill, "Лечение")
        self.assertEqual(parsed.failed_skill_uses[0].reason, "магия заблокирована")

        memory = CombatMemory(target_name="Фонарщик", pending_skill="лечение")
        memory.pending_target = SkillTarget.ENEMY
        memory.observe("⚔️ Раунд 8\nKombat: Лечение (неудача: магия заблокирована)", CHARACTER)
        self.assertIsNone(memory.pending_skill)
        self.assertIsNone(memory.pending_target)

    def test_named_poison_is_included_in_survival_forecast(self) -> None:
        self.assertTrue(is_periodic_effect("Змеиный яд"))
        self.assertTrue(is_periodic_effect("Раскаленное ядро [добивание]"))
        memory = CombatMemory(target_name="Древесная змея")
        memory.observe(
            """⚔️ Раунд 6
Правая сторона
🪬🧍Kombat получает 24 урона · Змеиный яд
🪬🧍Kombat
❤️ 500/780
🐍 Змеиный яд · 2 хода""",
            CHARACTER,
        )
        self.assertEqual(memory.periodic_damage, 24)
        self.assertEqual(memory.periodic_damage_turns, 2)

    def test_full_player_round_is_parsed_into_typed_state(self) -> None:
        parsed = parse_combat_round(
            """⚔️ Раунд 29
Левая сторона
🪬🧍Kombat восстанавливает 40 HP · renew
🪬🧍Kombat использует Лечение
🪬🧍Kombat восстанавливает 124 HP

🔷 Мана: 4 → 0

🪬🧍Kombat
❤️ 203/780
✦ Стойкость веры · 2 хода
🦵 Калечение · 1 ход
✦ Обновление · 2 хода

📊 Урон за раунд: 0"""
        )

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.number, 29)
        self.assertIs(parsed.side, CombatSide.LEFT)
        self.assertEqual((parsed.mana_before, parsed.mana_after), (4, 0))
        self.assertEqual(parsed.current_mana, 0)
        self.assertEqual(parsed.total_damage, 0)
        self.assertEqual(parsed.skill_uses[0].skill, "Лечение")
        self.assertEqual(
            [(event.amount, event.effect) for event in parsed.healing],
            [(40, "renew"), (124, None)],
        )
        player = parsed.combatant(CHARACTER)
        self.assertIsNotNone(player)
        assert player is not None
        self.assertEqual((player.current_hp, player.max_hp), (203, 780))
        self.assertEqual(
            [(effect.name, effect.turns) for effect in player.effects],
            [("Стойкость веры", 2), ("Калечение", 1), ("Обновление", 2)],
        )

    def test_enemy_round_records_damage_effects_and_defeat(self) -> None:
        parsed = parse_combat_round(
            """⚔️ Раунд 7
Правая сторона
Пепельник атакует 🪬🧍Kombat
🪬🧍Kombat получает 85 урона 💢 крит
🔥 Наложено: Горение на 3 хода
💫 Пепельник ушла из-под удара
💀 Фонарщик повержен
🪬🧍Kombat
❤️ 303/870
🔥 Горение · 3 хода
📊 Урон за раунд: 85"""
        )

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertIs(parsed.side, CombatSide.RIGHT)
        self.assertEqual(parsed.attacks[0].actor, "Пепельник")
        self.assertEqual(parsed.attacks[0].target, "🪬🧍Kombat")
        self.assertEqual(parsed.damage[0].amount, 85)
        self.assertTrue(parsed.damage[0].critical)
        self.assertEqual(
            (parsed.applied_effects[0].name, parsed.applied_effects[0].turns),
            ("Горение", 3),
        )
        self.assertEqual(parsed.applied_effects[0].target, "🪬🧍Kombat")
        self.assertEqual(parsed.dodged, ("Пепельник",))
        self.assertEqual(parsed.defeated, ("Фонарщик",))

    def test_multi_hit_total_and_powerful_critical_are_parsed(self) -> None:
        parsed = parse_combat_round(
            "⚔️ Раунд 4\nФонарщик получает 61 / 29 / 29 = 119 урона ❗️Мощный крит"
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.damage[0].amount, 119)
        self.assertTrue(parsed.damage[0].critical)

    def test_player_prompt_includes_cooldowns_costs_and_timer(self) -> None:
        parsed = parse_combat_round(
            """🎯 Раунд 8
Ход Kombat
🔷 Мана: 6/12
⏳ Осталось: 18 сек.
Выбран навык: Лечение""",
            (
                "Обновление [Мана 4] (CD: 2)",
                "Лечение [Мана 4]",
                "Святое свечение [Мана 3]",
                "Атака аколита",
            ),
        )

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.turn_actor, "Kombat")
        self.assertEqual((parsed.current_mana, parsed.max_mana), (6, 12))
        self.assertEqual(parsed.remaining_seconds, 18)
        self.assertEqual(len(parsed.available_skills), 4)
        self.assertEqual(
            set(parsed.castable_skills()),
            {"лечение", "святое свечение", "атака аколита"},
        )

    def test_strategy_uses_the_already_parsed_round(self) -> None:
        message = FakeMessage("текст намеренно без маны", [["не навык"]])
        round_state = parse_combat_round(
            "🎯 Раунд 3\nХод Kombat\n🔷 Мана: 6/12",
            ("Святое свечение [Мана 3]", "Атака аколита"),
        )
        assert round_state is not None
        decision = choose_combat_action(
            message,
            memory=CombatMemory(target_name="Фонарщик", enemy_current_hp=800),
            current_hp=780,
            max_hp=780,
            heal_threshold=300,
            round_state=round_state,
        )

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.skill_name, "атака аколита")

    def test_round_history_is_archived_when_the_next_encounter_starts(self) -> None:
        memory = CombatMemory(target_name="Фонарщик")
        memory.observe("⚔️ Раунд 1\nФонарщик атакует Kombat\nKombat получает 60 урона", CHARACTER)
        memory.begin("Другой моб")

        self.assertEqual(len(memory.last_battle_rounds), 1)
        self.assertEqual(memory.last_battle_rounds[0].number, 1)
        self.assertEqual(memory.round_history, [])

    def test_decision_trace_explains_and_serializes_the_plan(self) -> None:
        memory = CombatMemory(target_name="Фонарщик", enemy_current_hp=552, enemy_max_hp=1025)
        memory.incoming_damage.add(58)
        memory.incoming_damage.add(64)
        round_state = parse_combat_round(
            "🎯 Раунд 8\nХод Kombat\n🔷 Мана: 6/12",
            ("Лечение [Мана 4]", "Атака аколита"),
        )
        assert round_state is not None
        decision = choose_combat_action(
            FakeMessage("", []),
            memory=memory,
            current_hp=493,
            max_hp=780,
            heal_threshold=300,
            round_state=round_state,
        )
        assert decision is not None
        trace = build_decision_trace(
            created_at="2026-08-15T20:00:00+00:00",
            telegram_message_id=17,
            memory=memory,
            round_state=round_state,
            current_hp=493,
            max_hp=780,
            decision=decision,
        )

        payload = trace.as_payload()
        self.assertEqual(payload["target_name"], "Фонарщик")
        self.assertEqual(payload["round_number"], 8)
        self.assertEqual(payload["incoming_damage"]["samples"], 2)
        self.assertEqual(payload["decision"]["skill_name"], decision.skill_name)
        self.assertIn("[COMBAT_PLAN]", trace.format_log())
        self.assertIn("причина:", trace.format_log())

    def test_treatment_trace_records_actual_self_heal_without_losing_plan(self) -> None:
        memory = CombatMemory(
            target_name="Черная мушка",
            enemy_current_hp=88,
            enemy_max_hp=475,
        )
        round_state = parse_combat_round(
            "🎯 Раунд 15\nХод Kombat\n🔷 Мана: 6/12",
            ("Лечение [Мана 4]", "Атака аколита"),
        )
        assert round_state is not None
        trace = build_decision_trace(
            created_at="2026-08-16T09:31:17+00:00",
            telegram_message_id=19,
            memory=memory,
            round_state=round_state,
            current_hp=694,
            max_hp=780,
            decision=CombatDecision(
                "лечение",
                SkillTarget.ENEMY,
                "ожидалась атака нежити",
            ),
        )
        result = parse_combat_round(
            """⚔️ Раунд 15
🪬🧍Kombat восстанавливает 40 HP · renew
🪬🧍Kombat использует Лечение
🪬🧍Kombat восстанавливает 46 HP
🪬🧍Kombat
❤️ 780/780"""
        )
        assert result is not None

        resolved = resolve_decision_trace(trace, result, CHARACTER)
        payload = resolved.as_payload()

        self.assertEqual(payload["decision"]["target"], "enemy")
        self.assertEqual(payload["outcome"]["target"], "self")
        self.assertEqual(payload["outcome"]["effect"], "healing")
        self.assertEqual(payload["outcome"]["amount"], 46)

    def test_trace_records_an_enemy_dodge_as_a_resolved_attack(self) -> None:
        memory = CombatMemory(
            target_name="Пепельник",
            enemy_current_hp=116,
            enemy_max_hp=920,
        )
        trace = build_decision_trace(
            created_at="2026-08-22T10:00:27+00:00",
            telegram_message_id=2949780,
            memory=memory,
            round_state=None,
            current_hp=154,
            max_hp=830,
            decision=CombatDecision(
                "лечение",
                SkillTarget.ENEMY,
                "атака нежити",
            ),
        )
        result = parse_combat_round(
            """⚔️ Раунд 10
🪬🧍Kombat использует Лечение
⚡️ Пепельник увернулся"""
        )
        assert result is not None

        resolved = resolve_decision_trace(trace, result, CHARACTER)

        self.assertIs(resolved.actual_target, SkillTarget.ENEMY)
        self.assertEqual(resolved.actual_effect, "dodged")
        self.assertEqual(resolved.actual_amount, 0)

    def test_confirmed_enemy_treatment_without_damage_keeps_its_target(self) -> None:
        memory = CombatMemory(
            target_name="Пепельник",
            enemy_current_hp=116,
            enemy_max_hp=920,
        )
        trace = build_decision_trace(
            created_at="2026-08-22T10:00:27+00:00",
            telegram_message_id=2949780,
            memory=memory,
            round_state=None,
            current_hp=154,
            max_hp=830,
            decision=CombatDecision(
                "лечение",
                SkillTarget.ENEMY,
                "атака нежити",
            ),
        )
        result = parse_combat_round(
            """⚔️ Раунд 10
🪬🧍Kombat использует Лечение"""
        )
        assert result is not None

        resolved = resolve_decision_trace(trace, result, CHARACTER)

        self.assertIs(resolved.actual_target, SkillTarget.ENEMY)
        self.assertEqual(resolved.actual_effect, "no_effect")
        self.assertEqual(resolved.actual_amount, 0)

    def test_target_selector_obeys_skill_intent(self) -> None:
        message = FakeMessage(
            "Выберите цель для «Лечение»",
            [["🎯 Kombat [493/780]"], ["🎯 Фонарщик [552/1025]"], ["↩️ Отмена"]],
        )
        self_target, self_position = select_combat_target(
            message,
            ["Фонарщик"],
            preferred_target="self",
            character_name=CHARACTER,
        )
        enemy_target, enemy_position = select_combat_target(
            message,
            ["Фонарщик"],
            preferred_target="enemy",
            character_name=CHARACTER,
        )

        self.assertIn("Kombat", self_target or "")
        self.assertEqual(self_position, (0, 0))
        self.assertEqual(enemy_target, "Фонарщик")
        self.assertEqual(enemy_position, (1, 0))
