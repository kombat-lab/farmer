from __future__ import annotations

import random
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from blessing import BlessingManager
from combat_round import CombatSide, parse_combat_round
from combat_strategy import CombatMemory, ObservedRange, SkillTarget, choose_combat_action
from config import DEFAULT_BATTLE_START_HP_PERCENT, DEFAULT_HEAL_THRESHOLD
from event_cache import BoundedKeyCache
from human_delays import HumanDelayModel, parse_remaining_seconds
from models import RuntimeContext
from navigator import SnakeNavigator
from parser import classify_message, extract_player_hp, parse_map
from rewards import BattleReward, parse_item_stack
from settings_service import SettingsService
from skills import HEALING_MANA_RESERVE, enough_health_for_battle
from storage import Storage
from targeting import select_combat_target
from telegram_safety import (
    RollingAttemptGuard,
    StateRefreshGate,
    TelegramActionLimiter,
    message_revision_key,
    message_state_key,
)

CHARACTER = "Kombat"
TARGETS = ["Черная мушка"]


class FakeButton:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeMessage:
    def __init__(
        self,
        text: str,
        buttons: list[list[str]],
        *,
        message_id: int = 1,
        edit_date: datetime | None = None,
    ) -> None:
        self.id = message_id
        self.edit_date = edit_date
        self.raw_text = text
        self.buttons = [[FakeButton(button) for button in row] for row in buttons]


class ParserTests(unittest.TestCase):
    def test_health_recovery_notifications_update_hp(self) -> None:
        self.assertEqual(
            extract_player_hp(
                "❤️ Ваше здоровье восстановилось до 554/780.",
                CHARACTER,
            ),
            (554, 780),
        )
        self.assertEqual(
            extract_player_hp(
                "❤️ Ваше здоровье полностью восстановлено: 780/780.",
                CHARACTER,
            ),
            (780, 780),
        )

    def test_map_parser_is_pure_and_does_not_switch_navigator(self) -> None:
        navigator = SnakeNavigator(0, 8, 0, 8)
        self.assertIsNone(navigator.location_name)

        text = (
            "🗺️ Темный грот\nПозиция: (1, 0)\nМонстры на клетке: 1 (Черная мушка)\nKombat (845/845)"
        )
        parsed = parse_map(text, TARGETS, CHARACTER)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.location_name, "Темный грот")
        self.assertIsNone(navigator.location_name)
        self.assertEqual(
            classify_message(text, TARGETS, CHARACTER).name,
            "MAP",
        )
        self.assertIsNone(navigator.location_name)

    def test_blocked_movement_is_exposed_as_data(self) -> None:
        parsed = parse_map(
            "🗺️ Мертвый лес\nПозиция: (5, 0)\nМонстры на клетке: 0\nСтатус: Туда пройти нельзя",
            TARGETS,
            CHARACTER,
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertTrue(parsed.movement_blocked)


class SkillTests(unittest.TestCase):
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


class CombatRoundModelTests(unittest.TestCase):
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


class ModelTests(unittest.TestCase):
    def test_runtime_context_lists_are_not_shared(self) -> None:
        first = RuntimeContext()
        second = RuntimeContext()
        first.combat_enemies.append("Черная мушка")
        self.assertEqual(second.combat_enemies, [])


class MovementRecoveryTests(unittest.TestCase):
    def test_failed_move_is_replanned_locally_with_another_button(self) -> None:
        navigator = SnakeNavigator(0, 8, 0, 8)
        origin = (8, 0)
        first = navigator.plan(origin)
        navigator.reject_last_plan(origin, mark_destination_blocked=False)
        second = navigator.plan(origin)

        self.assertNotEqual(first.button, second.button)
        self.assertIn(first.button, navigator.failed_buttons[origin])

    def test_all_move_buttons_are_tried_before_candidates_repeat(self) -> None:
        navigator = SnakeNavigator(0, 8, 0, 8)
        origin = (8, 0)
        buttons: list[str] = []

        for _ in navigator.ALL_MOVE_BUTTONS:
            plan = navigator.plan(origin)
            buttons.append(plan.button)
            exhausted = navigator.reject_last_plan(origin, mark_destination_blocked=False)
            if exhausted:
                break

        self.assertEqual(len(buttons), len(set(buttons)))
        self.assertTrue(exhausted)

    def test_unknown_obstacle_is_learned_on_burned_field(self) -> None:
        navigator = SnakeNavigator(0, 8, 0, 8)
        navigator.use_location("Выжженное поле")

        self.assertEqual((navigator.max_x, navigator.max_y), (11, 11))
        plan = navigator.plan((11, 0))
        navigator.reject_last_plan((11, 0), mark_destination_blocked=True)

        self.assertIn(plan.destination, navigator.runtime_blocked)
        self.assertTrue(navigator.obstacle_mode)
        self.assertNotIn(plan.destination, navigator.position_to_indices)

    def test_route_stays_in_reachable_component_after_learned_wall(self) -> None:
        navigator = SnakeNavigator(0, 8, 0, 8)
        wall = {(5, y) for y in range(12)}
        navigator.use_location(
            "Выжженное поле",
            wall,
            current_position=(11, 0),
        )

        self.assertTrue(navigator.route)
        self.assertTrue(all(x > 5 for x, _ in navigator.route))
        self.assertEqual(navigator.plan((11, 0)).origin, (11, 0))


class TelegramSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_noop_edit_has_same_semantic_state(self) -> None:
        first = FakeMessage(
            "Ход игрока",
            [["Атака"]],
            edit_date=datetime(2026, 8, 15, tzinfo=UTC),
        )
        second = FakeMessage(
            "Ход игрока",
            [["Атака"]],
            edit_date=datetime(2026, 8, 15, tzinfo=UTC) + timedelta(seconds=1),
        )

        self.assertEqual(message_state_key(first), message_state_key(second))
        self.assertNotEqual(message_revision_key(first), message_revision_key(second))

    def test_state_refresh_is_reserved_once_per_inbound_generation(self) -> None:
        gate = StateRefreshGate()
        self.assertTrue(gate.reserve(7))
        self.assertFalse(gate.reserve(7))
        self.assertTrue(gate.reserve(8))

    def test_recovery_attempts_are_bounded_in_a_rolling_window(self) -> None:
        now = 0.0
        guard = RollingAttemptGuard(
            max_attempts=3,
            window_seconds=600.0,
            clock=lambda: now,
        )

        self.assertTrue(guard.allow())
        self.assertTrue(guard.allow())
        self.assertTrue(guard.allow())
        self.assertFalse(guard.allow())
        now = 601.0
        self.assertTrue(guard.allow())

    async def test_action_limiter_serializes_and_caps_a_window(self) -> None:
        now = 0.0

        def clock() -> float:
            return now

        async def sleep(seconds: float) -> None:
            nonlocal now
            now += seconds

        limiter = TelegramActionLimiter(
            min_interval=1.0,
            max_actions=2,
            window_seconds=10.0,
            clock=clock,
            sleep=sleep,
        )

        self.assertEqual(await limiter.acquire(), 0.0)
        self.assertEqual(await limiter.acquire(), 1.0)
        self.assertEqual(await limiter.acquire(), 9.0)
        self.assertEqual(now, 10.0)
        self.assertFalse(limiter.pending)


class HumanDelayTests(unittest.TestCase):
    def test_delays_stay_inside_configured_ranges(self) -> None:
        model = HumanDelayModel(random.Random(7))
        normal = [model.action_delay(2.0, 7.0) for _ in range(100)]
        urgent = [model.action_delay(2.0, 7.0, urgent=True) for _ in range(100)]

        self.assertTrue(all(2.0 <= delay <= 7.0 for delay in normal))
        self.assertTrue(all(2.0 <= delay <= 4.0 for delay in urgent))
        self.assertLess(sum(normal) / len(normal), 4.8)

    def test_turn_timer_caps_delay_with_safety_margin(self) -> None:
        model = HumanDelayModel(random.Random(3))

        self.assertLessEqual(model.action_delay(2.0, 7.0, remaining_seconds=7), 1.0)
        self.assertEqual(parse_remaining_seconds("⏳ Осталось: 24 сек"), 24)

    def test_long_pause_cannot_repeat_every_move(self) -> None:
        model = HumanDelayModel(random.Random(1))

        self.assertFalse(model.should_take_long_pause(1.0))
        self.assertFalse(model.should_take_long_pause(1.0))
        self.assertTrue(model.should_take_long_pause(1.0))


class SharedComponentTests(unittest.IsolatedAsyncioTestCase):
    async def test_blessing_flow_uses_single_manager(self) -> None:
        manager = BlessingManager()
        actions: list[str] = []

        async def click_button(message, **kwargs) -> bool:
            actions.append(str(kwargs["description"]))
            return True

        opened = await manager.try_open_from_map(
            object(),
            click_button=click_button,
            log=lambda text: None,
            mark_progress=lambda text: None,
        )
        handled = await manager.handle_menu(
            object(),
            find_button=lambda message, **kwargs: (0, 0),
            click_button=click_button,
            mark_progress=lambda text: None,
        )
        confirmed = manager.confirm_from_text(
            "Благословение: +5 ко всем характеристикам на 30 мин",
            log=lambda text: None,
            mark_progress=lambda text: None,
        )

        self.assertTrue(opened)
        self.assertTrue(handled)
        self.assertTrue(confirmed)
        self.assertEqual(actions, ["Небоевые навыки", "Благословение"])
        self.assertFalse(manager.refresh_in_progress)

    async def test_bounded_cache_evicts_oldest_key(self) -> None:
        cache = BoundedKeyCache(max_size=2)
        self.assertTrue(cache.remember((1,)))
        self.assertTrue(cache.remember((2,)))
        self.assertFalse(cache.remember((2,)))
        self.assertTrue(cache.remember((3,)))
        self.assertNotIn((1,), cache)
        self.assertIn((2,), cache)


class RewardTests(unittest.TestCase):
    def test_item_stack_suffix_is_parsed(self) -> None:
        self.assertEqual(parse_item_stack("Золотой хитин x3"), ("Золотой хитин", 3))
        self.assertEqual(parse_item_stack("Осколок х3"), ("Осколок", 3))
        self.assertEqual(parse_item_stack("Обычный предмет"), ("Обычный предмет", 1))


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_database_uses_current_schema_directly(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.sqlite3")
            columns = {
                str(row["name"])
                for row in storage.connection.execute(
                    "PRAGMA table_info(farmer_state)"
                ).fetchall()
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
            await storage.close()

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

    async def test_unknown_setting_is_ignored_without_extra_writes(self) -> None:
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
            changes_after_first_load = storage.connection.total_changes
            await settings.load()
            self.assertEqual(storage.connection.total_changes, changes_after_first_load)
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
            await storage.record_battle(
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

    def test_runtime_statistics_count_stack_quantity(self) -> None:
        from statistics import FarmStatistics

        stats = FarmStatistics()
        reward = BattleReward(dust=0, xp=0, items=("Осколок x3",))
        stats.add_victory(1, reward)
        self.assertEqual(stats.session_report().drops, {"Осколок": 3})


if __name__ == "__main__":
    unittest.main()
