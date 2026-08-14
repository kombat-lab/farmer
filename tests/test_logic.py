from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from models import RuntimeContext
from navigator import SnakeNavigator
from parser import classify_message, parse_map
from rewards import BattleReward, parse_item_stack
from skills import HEALING_MANA_RESERVE, choose_skill
from storage import Storage

CHARACTER = "Kombat"
TARGETS = ["Черная мушка"]


class FakeButton:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeMessage:
    def __init__(self, text: str, buttons: list[list[str]]) -> None:
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
    def test_holy_light_keeps_healing_mana_reserve(self) -> None:
        message = FakeMessage(
            "Мана: 6/11",
            [["Святое свечение (-3 маны)"], ["Атака аколита"]],
        )
        self.assertEqual(HEALING_MANA_RESERVE, 4)
        self.assertEqual(
            choose_skill(message, current_hp=800, heal_threshold=300),
            "атака аколита",
        )

    def test_healing_has_priority_below_threshold(self) -> None:
        message = FakeMessage(
            "Мана: 11/11",
            [["Лечение (-4 маны)"], ["Святое свечение (-3 маны)"]],
        )
        self.assertEqual(
            choose_skill(message, current_hp=300, heal_threshold=300),
            "лечение",
        )


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


class RewardTests(unittest.TestCase):
    def test_item_stack_suffix_is_parsed(self) -> None:
        self.assertEqual(parse_item_stack("Золотой хитин x3"), ("Золотой хитин", 3))
        self.assertEqual(parse_item_stack("Осколок х3"), ("Осколок", 3))
        self.assertEqual(parse_item_stack("Обычный предмет"), ("Обычный предмет", 1))


class StorageTests(unittest.IsolatedAsyncioTestCase):
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
        stats.add_victory(1, "Цель", reward)
        self.assertEqual(stats.session_report().drops, {"Осколок": 3})


if __name__ == "__main__":
    unittest.main()
