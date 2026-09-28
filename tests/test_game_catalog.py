from __future__ import annotations

import unittest

from game_catalog import ALL_MONSTER_NAMES, get_location, get_monster_names
from navigator import SnakeNavigator


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

    def test_scorched_field_target_priority_is_registered(self) -> None:
        self.assertEqual(
            get_monster_names("Выжженное поле"),
            (
                "Колокол пепла",
                "Пожиратель золы",
                "Фонарщик",
                "Пепельник",
                "Огненный птенец",
                "Саламандра",
                "Хрустящий",
                "Корень крематория",
            ),
        )
