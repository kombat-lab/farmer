from __future__ import annotations

import unittest

from navigator import SnakeNavigator
from parser import (
    classify_message,
    extract_player_hp,
    is_passive_health_notification,
    parse_map,
)
from tests.fog_test_fixtures import CHARACTER, TARGETS


class ParserTests(unittest.TestCase):
    def test_health_recovery_notifications_update_hp(self) -> None:
        self.assertEqual(
            extract_player_hp(
                "❤️ Ваше здоровье восстановилось до 554/780.",
                CHARACTER,
            ),
            (554, 780),
        )

    def test_health_recovery_notification_is_passive_ui_state(self) -> None:
        self.assertTrue(
            is_passive_health_notification("❤️ Ваше здоровье полностью восстановлено: 755/755.")
        )
        self.assertFalse(
            is_passive_health_notification("⚔️ Раунд 3\nKombat восстанавливает 40 HP · renew")
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
