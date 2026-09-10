from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime

from combat_round import parse_combat_round
from parser import extract_player_hp
from targeting import analyze_map_targets, select_combat_target


@dataclass(frozen=True)
class Button:
    text: str


@dataclass(frozen=True)
class Message:
    buttons: tuple[tuple[Button, ...], ...]
    raw_text: str | None = ""
    id: int = 1
    edit_date: datetime | None = None

    async def click(self, row: int, column: int) -> object:
        raise AssertionError("A selector must not perform Telegram I/O")


def message(*rows: tuple[str, ...]) -> Message:
    return Message(tuple(tuple(Button(text) for text in row) for row in rows))


class TargetingRegressions(unittest.TestCase):
    def test_disabled_monster_with_a_longer_name_is_not_selected(self) -> None:
        result = analyze_map_targets(
            message(("Золотой бронзовик [300/300]",)), ["Бронзовик"]
        )
        self.assertIsNone(result.selected_target)
        self.assertIsNone(result.selected_position)
        self.assertEqual(result.target_counts, {})

    def test_result_carries_the_exact_verified_button_position(self) -> None:
        result = analyze_map_targets(
            message(
                ("Золотой бронзовик [300/300]", "Бронзовик [100/100] (занят)"),
                ("PvP: Бронзовик [1/100]", "🎯 Бронзовик [80/100]"),
            ),
            ["Бронзовик"],
        )
        self.assertEqual(result.selected_target, "Бронзовик")
        self.assertEqual(result.selected_position, (1, 1))
        self.assertEqual(result.target_counts, {"Бронзовик": (2, 1)})

    def test_all_occupied_targets_have_no_selected_button(self) -> None:
        result = analyze_map_targets(
            message(("🎯 Бронзовик [100/100] — занят",)), ["Бронзовик"]
        )
        self.assertTrue(result.all_matching_targets_are_occupied)
        self.assertIsNone(result.selected_position)

    def test_self_target_requires_the_whole_character_name(self) -> None:
        target, position = select_combat_target(
            message(("SuperKombat [1/100]", "🪬🧍Kombat [50/100]")),
            [], preferred_target="self", character_name="Kombat",
        )
        self.assertEqual(position, (0, 1))
        self.assertEqual(target, "🪬🧍Kombat")

    def test_longer_enemy_name_is_not_relabelled_as_configured_target(self) -> None:
        target, position = select_combat_target(
            message(("Золотой бронзовик [10/300]", "Бронзовик [100/100]")),
            ["Бронзовик"], preferred_target="enemy", character_name="Kombat",
        )
        self.assertEqual((target, position), ("Золотой бронзовик", (0, 0)))

    def test_unrecognised_menu_button_is_not_an_enemy_target(self) -> None:
        _, position = select_combat_target(
            message(("Информация о навыке", "↩️ Отмена")),
            ["Фонарщик"], preferred_target="enemy", character_name="Kombat",
        )
        self.assertIsNone(position)


class ParticipantParsingRegressions(unittest.TestCase):
    def test_action_mentions_cannot_capture_enemy_health(self) -> None:
        text = (
            "⚔️ Раунд 3\nKombat атакует Фонарщик\nФонарщик\n❤️ 300/300\n\n"
            "🪬🧍Kombat\n❤️ 50/100"
        )
        self.assertEqual(extract_player_hp(text, "Kombat"), (50, 100))

    def test_health_requires_exact_player_name(self) -> None:
        self.assertIsNone(extract_player_hp("SuperKombat (300/300)", "Kombat"))
        self.assertIsNone(extract_player_hp("SuperKombat\n❤️ 300/300", "Kombat"))
        self.assertEqual(extract_player_hp("🪬Kombat (50/100)", "Kombat"), (50, 100))

    def test_effects_stop_at_next_participant_without_blank_separator(self) -> None:
        parsed = parse_combat_round(
            "⚔️ Раунд 3\nKombat\n❤️ 100/300\n🔥 Горение · 1 ход\n"
            "Фонарщик\n❤️ 300/300\n✦ Обновление · 3 хода"
        )
        assert parsed is not None
        player = parsed.combatant("Kombat")
        enemy = parsed.combatant("Фонарщик")
        assert player is not None and enemy is not None
        self.assertEqual([effect.name for effect in player.effects], ["Горение"])
        self.assertEqual([effect.name for effect in enemy.effects], ["Обновление"])
        self.assertIsNone(parsed.combatant("Фонар"))
