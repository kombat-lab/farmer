from __future__ import annotations

import random
import sqlite3
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from blessing import BlessingManager
from combat_learning import build_shadow_plan
from combat_round import CombatSide, parse_combat_round
from combat_strategy import (
    CombatDecision,
    CombatMemory,
    ObservedRange,
    RecentCombatKnowledge,
    SkillTarget,
    build_decision_trace,
    choose_combat_action,
    is_periodic_effect,
    resolve_decision_trace,
)
from config import (
    ACTIVITY_PROFILE_FAST,
    ACTIVITY_PROFILE_NORMAL,
    DEFAULT_BATTLE_START_HP_PERCENT,
    DEFAULT_HEAL_THRESHOLD,
)
from event_cache import BoundedKeyCache
from farmer import Farmer
from game_catalog import ALL_MONSTER_NAMES, get_location, get_monster_names
from human_delays import ActivityBreakPlanner, HumanDelayModel, parse_remaining_seconds
from models import ActionType, BotState, RuntimeContext
from navigator import SnakeNavigator
from parser import classify_message, extract_player_hp, parse_map
from rewards import BattleReward, parse_item_stack
from settings_service import SettingsService
from skills import HEALING_MANA_RESERVE, enough_health_for_battle, parse_skill_button
from storage import Storage
from targeting import select_combat_target
from telegram_safety import (
    RollingAttemptGuard,
    StateRefreshGate,
    TelegramActionLimiter,
    TelegramActionTelemetry,
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
            "🗺️ Темный грот\nПозиция: (1, 0)\n"
            "Монстры на клетке: 1 (Черная мушка)\nKombat (845/845)"
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

    def test_map_size_is_parsed_from_game_message(self) -> None:
        parsed = parse_map(
            "🗺️ Темный грот\nПозиция: (12, 14)\nРазмер: 15x15\n"
            "Монстры на клетке: 0\nKombat (845/845)",
            TARGETS,
            CHARACTER,
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual((parsed.width, parsed.height), (15, 15))

        navigator = SnakeNavigator(0, 8, 0, 8)
        navigator.use_location(
            parsed.location_name or "Темный грот",
            current_position=parsed.position,
            width=parsed.width,
            height=parsed.height,
        )
        self.assertEqual((navigator.max_x, navigator.max_y), (14, 14))
        navigator.validate_position((12, 14))

    def test_blocked_movement_is_exposed_as_data(self) -> None:
        parsed = parse_map(
            "🗺️ Мертвый лес\nПозиция: (5, 0)\nМонстры на клетке: 0\nСтатус: Туда пройти нельзя",
            TARGETS,
            CHARACTER,
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertTrue(parsed.movement_blocked)


class GameCatalogTests(unittest.TestCase):
    def test_desert_plain_and_targets_are_registered(self) -> None:
        location = get_location("Пустынная равнина")

        self.assertIsNotNone(location)
        self.assertEqual(
            get_monster_names("Пустынная равнина"),
            (
                "Хранитель дюн",
                "Камнешкурый варан",
                "Скорпион",
                "Кактус",
                "Стервятник",
                "Гремучая змея",
                "Пыльник",
            ),
        )
        self.assertIn("Пыльник", ALL_MONSTER_NAMES)

    def test_known_large_location_keeps_fallback_geometry(self) -> None:
        navigator = SnakeNavigator(0, 8, 0, 8)
        navigator.use_location("Выжженное поле")

        self.assertEqual((navigator.max_x, navigator.max_y), (11, 11))


class SkillTests(unittest.TestCase):
    def test_magic_blocked_and_explicit_low_mana_buttons_are_unavailable(self) -> None:
        blocked = parse_skill_button(
            "⏳ Лечение [Мана 4] (магия заблокирована)"
        )
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
            memory.outgoing_damage.setdefault("атака аколита", ObservedRange()).add(
                value
            )
        for value in (4, 67):
            memory.outgoing_damage.setdefault("святое свечение", ObservedRange()).add(
                value
            )
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
            memory.outgoing_damage.setdefault(
                "атака аколита", ObservedRange()
            ).add(value)
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
            enemy_current_hp=500,
            enemy_max_hp=920,
        )
        for _ in range(4):
            memory.incoming_damage.add(100)
        for value in (40, 42):
            memory.outgoing_damage.setdefault(
                "атака аколита", ObservedRange()
            ).add(value)
        memory.direct_healing.add(200)
        memory.direct_healing.add(200)
        message = FakeMessage(
            "Мана: 4/13",
            [["Лечение [Мана 4]"], ["Атака аколита"]],
        )

        plan = build_shadow_plan(
            message,
            memory=memory,
            current_hp=250,
            max_hp=830,
            executed=CombatDecision(
                "атака аколита", SkillTarget.ENEMY, "старое решение"
            ),
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
            memory.outgoing_damage.setdefault(
                "атака аколита", ObservedRange()
            ).add(value)
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


class CombatRoundModelTests(unittest.TestCase):
    def test_failed_skill_is_parsed_and_clears_pending_action(self) -> None:
        parsed = parse_combat_round(
            "⚔️ Раунд 8\nKombat: Лечение (неудача: магия заблокирована)"
        )
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


class ModelTests(unittest.TestCase):
    def test_runtime_context_lists_are_not_shared(self) -> None:
        first = RuntimeContext()
        second = RuntimeContext()
        first.combat_enemies.append("Черная мушка")
        self.assertEqual(second.combat_enemies, [])


class MovementRecoveryTests(unittest.TestCase):
    def test_obstacle_route_leaves_top_left_entrance_down_right_first(self) -> None:
        navigator = SnakeNavigator(0, 8, 0, 8)
        navigator.use_location(
            "Выжженное поле",
            {(7, 7)},
            current_position=(0, 0),
            width=12,
            height=12,
        )

        plan = navigator.plan((0, 0))

        self.assertEqual(plan.destination, (0, 1))
        self.assertEqual(plan.button, "↘️")

    def test_real_position_outside_stale_component_rebuilds_route(self) -> None:
        navigator = SnakeNavigator(0, 8, 0, 8)
        false_wall = {(5, y) for y in range(12)}
        navigator.use_location(
            "Мертвый лес",
            false_wall,
            current_position=(0, 0),
            width=12,
            height=12,
        )
        self.assertNotIn((11, 10), navigator.position_to_indices)

        recovered = navigator.recover_from_actual_transition((4, 10), (11, 10))

        self.assertTrue(recovered)
        self.assertIn((11, 10), navigator.position_to_indices)
        self.assertEqual(navigator.runtime_blocked, set())
        self.assertEqual(navigator.take_recovery_discarded_obstacles(), false_wall)
        self.assertEqual(navigator.plan((11, 10)).origin, (11, 10))

    def test_current_position_cannot_remain_a_learned_obstacle(self) -> None:
        navigator = SnakeNavigator(0, 8, 0, 8)
        navigator.use_location(
            "Мертвый лес",
            {(11, 10)},
            current_position=(11, 10),
            width=12,
            height=12,
        )

        self.assertIn((11, 10), navigator.position_to_indices)
        self.assertNotIn((11, 10), navigator.runtime_blocked)
        self.assertEqual(
            navigator.take_recovery_discarded_obstacles(),
            {(11, 10)},
        )

    def test_fallback_button_does_not_create_an_ambiguous_obstacle(self) -> None:
        navigator = SnakeNavigator(0, 8, 0, 8)
        navigator.use_location(
            "Мертвый лес",
            current_position=(11, 0),
            width=12,
            height=12,
        )
        first = navigator.plan((11, 0))
        navigator.reject_last_plan((11, 0), mark_destination_blocked=False)
        fallback = navigator.plan((11, 0))
        self.assertNotEqual(first.button, fallback.button)

        navigator.reject_last_plan((11, 0), mark_destination_blocked=True)

        self.assertNotIn(fallback.destination, navigator.runtime_blocked)

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

    def test_unknown_obstacle_is_learned_on_large_maps(self) -> None:
        for location_name in ("Мертвый лес", "Выжженное поле"):
            with self.subTest(location=location_name):
                navigator = SnakeNavigator(0, 8, 0, 8)
                navigator.use_location(location_name)

                self.assertEqual((navigator.max_x, navigator.max_y), (11, 11))
                self.assertEqual(navigator.blocked_cells, frozenset())
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

    def test_recovered_move_counts_toward_current_cycle(self) -> None:
        farmer = Farmer.__new__(Farmer)
        farmer.navigator = SnakeNavigator(0, 8, 0, 8)
        farmer.context = RuntimeContext()
        farmer.context.pending_move = farmer.navigator.plan((8, 0))
        farmer.moves_in_cycle = 7
        farmer.mark_progress = lambda _reason: None
        farmer.log = lambda _message: None

        actual_position = (6, 0)
        self.assertNotEqual(actual_position, farmer.context.pending_move.destination)
        farmer.confirm_pending_move(actual_position)

        self.assertEqual(farmer.context.move_count, 1)
        self.assertEqual(farmer.moves_in_cycle, 8)


class TelegramSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_outgoing_action_telemetry_uses_only_local_clock(self) -> None:
        now = 100.0
        telemetry = TelegramActionTelemetry(clock=lambda: now)
        telemetry.record("inline_callback")
        now = 150.0
        snapshot = telemetry.record("map_message")

        self.assertEqual(snapshot["total"], 2)
        self.assertEqual(snapshot["last_minute"], 2)
        self.assertEqual(
            snapshot["by_kind"],
            {"inline_callback": 1, "map_message": 1},
        )
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

    def test_activity_break_is_armed_by_moves_or_elapsed_time(self) -> None:
        now = 100.0
        planner = ActivityBreakPlanner(random.Random(7), clock=lambda: now)
        arguments = {
            "moves_min": 25,
            "moves_max": 40,
            "work_min": 1500.0,
            "work_max": 2700.0,
        }

        self.assertFalse(planner.is_due(0, **arguments))
        assert planner.next_move is not None and planner.deadline is not None
        self.assertTrue(25 <= planner.next_move <= 40)
        self.assertTrue(1600.0 <= planner.deadline <= 2800.0)
        self.assertTrue(planner.is_due(planner.next_move, **arguments))
        self.assertFalse(planner.is_due(planner.next_move, **arguments))

        planner.complete(planner.next_move, **arguments)
        self.assertFalse(planner.break_pending)
        self.assertGreater(planner.next_move or 0, 40)

        now = planner.deadline or now
        self.assertTrue(planner.is_due(0, **arguments))

    def test_activity_break_duration_stays_inside_profile(self) -> None:
        planner = ActivityBreakPlanner(random.Random(3))
        durations = [planner.duration(240.0, 480.0) for _ in range(100)]

        self.assertTrue(all(240.0 <= duration <= 480.0 for duration in durations))

    def test_fast_profile_uses_small_action_delays(self) -> None:
        farmer = Farmer.__new__(Farmer)
        farmer.delay_model = HumanDelayModel(random.Random(9))
        farmer.settings = SimpleNamespace(
            values=SimpleNamespace(
                activity_profile=ACTIVITY_PROFILE_FAST,
                move_delay_min=10.0,
                move_delay_max=20.0,
                attack_delay_min=10.0,
                attack_delay_max=20.0,
                target_delay_min=10.0,
                target_delay_max=20.0,
                skill_delay_min=10.0,
                skill_delay_max=20.0,
            )
        )

        delays = [farmer.action_delay(action) for action in ActionType]

        self.assertTrue(all(0.0 <= delay <= 1.2 for delay in delays))


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
            self.assertNotIn("max_moves", columns)
            event_columns = {
                str(row["name"])
                for row in storage.connection.execute(
                    "PRAGMA table_info(events)"
                ).fetchall()
            }
            self.assertNotIn("notified", event_columns)
            tables = {
                str(row["name"])
                for row in storage.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertIn("combat_decisions", tables)
            self.assertIn("combat_knowledge", tables)
            self.assertIn("combat_battle_analysis", tables)
            self.assertNotIn("combat_strategy_stats", tables)
            self.assertNotIn("combat_policy_stats", tables)
            await storage.close()

    async def test_opening_database_removes_obsolete_combat_tables(self) -> None:
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
            tables = {
                str(row["name"])
                for row in storage.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }

            self.assertNotIn("combat_strategy_stats", tables)
            self.assertNotIn("combat_policy_stats", tables)
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

            await storage.record_battle(
                telegram_message_id=100,
                session_id=session_id,
                target_name="Пепельник",
                result="VICTORY",
                combat_decisions=(trace(3, 101),),
            )
            await storage.record_battle(
                telegram_message_id=200,
                session_id=session_id,
                target_name="Пепельник",
                result="VICTORY",
                combat_decisions=(trace(4, 201),),
            )
            await storage.add_event("LOW_HP_WAIT_STARTED", "noise")
            await storage.add_event("WATCHDOG_TRIGGERED", "keep")

            cleanup = await storage.cleanup_old_data(retention_days=3650)

            versions = [
                int(row[0])
                for row in storage.connection.execute(
                    "SELECT json_extract(trace_json, '$.model_version') "
                    "FROM combat_decisions"
                ).fetchall()
            ]
            self.assertEqual(versions, [4])
            self.assertEqual(cleanup["combat_decisions"], 1)
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
            await storage.cleanup_old_data(retention_days=3650)
            free_before = int(
                storage.connection.execute("PRAGMA freelist_count").fetchone()[0]
            )

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
            await storage.save_combat_knowledge(780, knowledge.as_payload())
            await storage.close()

            reopened = Storage(path)
            profiles = await reopened.load_combat_knowledge()
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
            self.assertEqual(settings.values.activity_profile, ACTIVITY_PROFILE_NORMAL)
            self.assertNotIn("removed_setting", await storage.get_settings())
            changes_after_first_load = storage.connection.total_changes
            await settings.load()
            self.assertEqual(storage.connection.total_changes, changes_after_first_load)
            await storage.close()

    async def test_runtime_control_setting_is_not_removed_by_ui_settings(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.sqlite3")
            await storage.set_setting("farmer_stop_requested", True)

            await SettingsService(storage).load()

            self.assertTrue(await storage.get_setting("farmer_stop_requested"))
            await storage.close()

    async def test_activity_profile_is_validated_and_persisted(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "test.sqlite3"
            storage = Storage(path)
            settings = SettingsService(storage)
            await settings.load()
            await settings.set_activity_profile(ACTIVITY_PROFILE_FAST)
            self.assertEqual(settings.values.activity_profile, ACTIVITY_PROFILE_FAST)
            await storage.close()

            reopened = Storage(path)
            loaded = SettingsService(reopened)
            await loaded.load()
            self.assertEqual(loaded.values.activity_profile, ACTIVITY_PROFILE_FAST)
            with self.assertRaises(ValueError):
                await loaded.set_activity_profile("unknown")
            await reopened.close()

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
                ["Костяной заяц"],
            )
            self.assertTrue(
                await loaded.remove_treatment_enemy_target("КОСТЯНОЙ ЗАЯЦ")
            )
            self.assertEqual(loaded.values.treatment_enemy_targets, [])
            await reopened.close()

    async def test_activity_break_resumes_with_one_state_refresh(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.sqlite3")
            farmer = Farmer.__new__(Farmer)
            farmer.running = True
            farmer.state = BotState.ACTIVITY_BREAK
            farmer.moves_in_cycle = 31
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
            await storage.record_battle(
                telegram_message_id=11,
                session_id=session_id,
                target_name="Фонарщик",
                result="VICTORY",
                combat_decisions=(trace,),
            )

            decisions = await storage.get_combat_decisions("Фонарщик")
            self.assertEqual(len(decisions), 1)
            self.assertEqual(decisions[0]["result"], "VICTORY")
            self.assertEqual(decisions[0]["chosen_skill"], "Лечение")
            self.assertEqual(decisions[0]["trace"]["round_number"], 8)

            faster_trace = dict(trace)
            faster_trace["telegram_message_id"] = 12
            faster_trace["round_number"] = 6
            await storage.record_battle(
                telegram_message_id=13,
                session_id=session_id,
                target_name="Фонарщик",
                result="VICTORY",
                combat_decisions=(faster_trace,),
            )
            learning = await storage.get_combat_learning_stats(
                target_name="Фонарщик"
            )
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

            await storage.record_battle(
                telegram_message_id=21,
                session_id=session_id,
                target_name="Черная мушка",
                result="VICTORY",
                combat_decisions=(trace,),
            )

            decisions = await storage.get_combat_decisions("Черная мушка")
            policies = await storage.get_combat_learning_overview(
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
            await storage.record_battle(
                telegram_message_id=43,
                session_id=session_id,
                target_name="Фонарщик",
                result="VICTORY",
                combat_decisions=traces,
            )

            rows = await storage.get_combat_learning_stats(
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
            overview = await storage.get_combat_learning_overview(
                target_name="Фонарщик",
                profile_max_hp=880,
            )
            self.assertEqual(overview[0]["battles"], 1)
            self.assertEqual(overview[0]["shadow_agreement_rate"], 0.5)

            storage.connection.execute("DELETE FROM combat_battle_analysis")
            storage.connection.commit()
            self.assertEqual(await storage.backfill_combat_battle_analysis(), 1)
            self.assertEqual(await storage.backfill_combat_battle_analysis(), 0)
            storage.connection.execute(
                "UPDATE battles SET happened_at='2020-01-01T00:00:00+00:00'"
            )
            storage.connection.commit()
            await storage.cleanup_old_data(retention_days=1)
            self.assertEqual(
                storage.connection.execute("SELECT COUNT(*) FROM battles").fetchone()[0],
                0,
            )
            self.assertEqual(
                len(
                    await storage.get_combat_learning_stats(
                        target_name="Фонарщик",
                        profile_max_hp=880,
                    )
                ),
                1,
            )
            await storage.close()

    async def test_unknown_settings_are_removed_once(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.sqlite3")
            await storage.set_settings(
                {"max_hp": 400, "max_mana": 11, "heal_amount": 141}
            )
            settings = SettingsService(storage)

            await settings.load()

            stored = await storage.get_settings()
            self.assertNotIn("max_hp", stored)
            self.assertNotIn("max_mana", stored)
            self.assertNotIn("heal_amount", stored)
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
            await storage.record_battle(
                telegram_message_id=100,
                session_id=session_id,
                target_name="Фонарщик",
                result="VICTORY",
                combat_decisions=first,
            )
            await storage.record_battle(
                telegram_message_id=101,
                session_id=session_id,
                target_name="Фонарщик",
                result="VICTORY",
                combat_decisions=second,
            )

            policies = await storage.get_combat_learning_overview(
                target_name="Фонарщик"
            )
            self.assertEqual(len(policies), 1)
            self.assertEqual(policies[0]["battles"], 2)
            self.assertEqual(policies[0]["offensive_ratio"], 0.5)
            await storage.close()

    async def test_stop_persists_final_movement_snapshot(self) -> None:
        from statistics import FarmStatistics

        class DisconnectedClient:
            def is_connected(self) -> bool:
                return False

        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.sqlite3")
            session_id = await storage.start_session(cycles_count=1, moves_per_cycle=80)
            farmer = Farmer.__new__(Farmer)
            farmer.running = True
            farmer.state = BotState.MAP
            farmer.stop_reason = None
            farmer.pending_progress_reason = None
            farmer.progress_persist_task = None
            farmer.context = RuntimeContext(
                current_position=(4, 5),
                current_hp=780,
                max_hp=780,
                move_count=89,
            )
            farmer.moves_in_cycle = 80
            farmer.current_cycle = 1
            farmer.session_id = session_id
            farmer.settings = SimpleNamespace(
                values=SimpleNamespace(moves_per_cycle=80, cycles_count=1)
            )
            farmer.statistics = FarmStatistics()
            farmer.storage = storage
            farmer.client = DisconnectedClient()
            farmer.worker_task = None
            farmer.watchdog_task = None
            farmer.recovery_task = None
            farmer.rest_task = None
            farmer.activity_break_task = None

            await farmer.stop("тестовая остановка")

            state = await storage.get_state()
            self.assertEqual(state["moves"], 89)
            self.assertEqual(state["moves_in_cycle"], 80)
            self.assertEqual(state["position_x"], 4)
            self.assertEqual(state["position_y"], 5)
            await storage.close()

    def test_runtime_statistics_count_stack_quantity(self) -> None:
        from statistics import FarmStatistics

        stats = FarmStatistics()
        reward = BattleReward(dust=0, xp=0, items=("Осколок x3",))
        stats.add_victory(1, reward)
        self.assertEqual(stats.session_report().drops, {"Осколок": 3})


if __name__ == "__main__":
    unittest.main()
