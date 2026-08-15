from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from blessing import BlessingManager
from combat_strategy import CombatMemory, SkillTarget, choose_combat_action
from config import DEFAULT_BATTLE_START_HP_PERCENT, DEFAULT_HEAL_THRESHOLD
from event_cache import BoundedKeyCache
from models import RuntimeContext
from navigator import SnakeNavigator
from parser import classify_message, parse_map
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
            memory=CombatMemory(target_name="Обычный противник", enemy_current_hp=800),
            current_hp=300,
            max_hp=845,
            heal_threshold=300,
        )
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.skill_name, "лечение")
        self.assertIs(decision.target, SkillTarget.SELF)


class CombatStrategyTests(unittest.TestCase):
    def test_treatment_attacks_verified_undead_when_safe(self) -> None:
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

    def test_renewal_is_cast_before_health_becomes_critical(self) -> None:
        memory = CombatMemory(target_name="Фонарщик", enemy_current_hp=552)
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

    def test_critical_hit_does_not_raise_guaranteed_damage(self) -> None:
        memory = CombatMemory(target_name="Фонарщик", enemy_current_hp=120)
        memory.observe(
            """🪬🧍Kombat использует Лечение
Фонарщик получает 160 урона""",
            CHARACTER,
        )

        self.assertEqual(memory.damage_floor("лечение"), 89)

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
        self.assertEqual(memory.damage_floor("лечение"), 89)
        self.assertEqual(memory.incoming_damage.maximum, 57)

    def test_periodic_damage_and_renewal_are_included_in_forecast(self) -> None:
        memory = CombatMemory(target_name="Пепельник", enemy_current_hp=500)
        memory.observe(
            """🪬🧍Kombat получает 14 урона · Горение
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
        self.assertEqual(memory.predicted_incoming(), 84)
        self.assertEqual(memory.predicted_incoming(after_current_tick=True), 84)
        memory.periodic_damage_turns = 1
        self.assertEqual(memory.predicted_incoming(after_current_tick=True), 70)

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
