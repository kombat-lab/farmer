from __future__ import annotations

import unittest

from navigator import SnakeNavigator


def actual_hex_move(position: tuple[int, int], button: str) -> tuple[int, int]:
    x, y = position
    offsets = {
        "⬅️": (-1, 0),
        "➡️": (1, 0),
        "↖️": (-1 if y % 2 == 0 else 0, -1),
        "↗️": (0 if y % 2 == 0 else 1, -1),
        "↙️": (-1 if y % 2 == 0 else 0, 1),
        "↘️": (0 if y % 2 == 0 else 1, 1),
    }
    dx, dy = offsets[button]
    return x + dx, y + dy


class HexNavigationRegressions(unittest.TestCase):
    def test_diagonal_passage_connects_both_sides_of_obstacles(self) -> None:
        blocked = {(1, 0), (1, 1), (0, 2)}
        navigator = SnakeNavigator(0, 2, 0, 2)
        navigator.use_location(
            "Поляна", blocked, current_position=(0, 0), width=3, height=3
        )
        expected = {(0, 0), (0, 1), (1, 2), (2, 0), (2, 1), (2, 2)}
        self.assertEqual(set(navigator.route), expected)
        position = (0, 0)
        for _ in range(30):
            if navigator.coverage_count == len(expected):
                break
            plan = navigator.plan(position)
            position = actual_hex_move(position, plan.button)
            self.assertEqual(position, plan.destination)
            self.assertNotIn(position, blocked)
            navigator.confirm_success(plan, position)
        self.assertEqual(navigator.visited_positions, expected)

    def test_hex_edges_and_reverse_buttons_agree_on_both_row_parities(self) -> None:
        navigator = SnakeNavigator(0, 4, 0, 4)
        for origin in ((2, 1), (2, 2)):
            actual_neighbours = {
                actual_hex_move(origin, button) for button in navigator.ALL_MOVE_BUTTONS
            }
            self.assertEqual(set(navigator._neighbors(origin)), actual_neighbours)
            for destination in actual_neighbours:
                outward = navigator._primary_button_between(origin, destination)
                inward = navigator._primary_button_between(destination, origin)
                self.assertEqual(actual_hex_move(origin, outward), destination)
                self.assertEqual(actual_hex_move(destination, inward), origin)
