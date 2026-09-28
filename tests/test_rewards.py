from __future__ import annotations

import unittest

from rewards import parse_battle_reward, parse_item_stack


class RewardTests(unittest.TestCase):
    def test_item_stack_suffix_is_parsed(self) -> None:
        self.assertEqual(parse_item_stack("Золотой хитин x3"), ("Золотой хитин", 3))
        self.assertEqual(parse_item_stack("Осколок х3"), ("Осколок", 3))
        self.assertEqual(parse_item_stack("Обычный предмет"), ("Обычный предмет", 1))

    def test_mist_crystals_are_parsed_as_currency(self) -> None:
        reward = parse_battle_reward(
            """Бой завершён

🏆 Победа
• + 7 ед. (🪬 1) Туманной пыли✨
• + 14 XP (🪬 3)
💎Туманные кристаллы: 1"""
        )

        self.assertEqual(reward.dust, 7)
        self.assertEqual(reward.xp, 14)
        self.assertEqual(reward.crystals, 1)
        self.assertEqual(reward.items, ())

    def test_crystal_line_after_items_header_is_not_an_item(self) -> None:
        reward = parse_battle_reward(
            """🏆 Победа
Предметы:
◼️ Кучка пепла
💎 Туманные кристаллы: 2"""
        )

        self.assertEqual(reward.crystals, 2)
        self.assertEqual(reward.items, ("◼️ Кучка пепла",))
