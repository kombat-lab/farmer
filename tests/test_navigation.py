from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from combat_round import parse_combat_round
from legacy_map_controller import LegacyMapController
from navigator import SnakeNavigator
from settings_service import SettingsService
from storage import Storage
from tests.fog_test_fixtures import CHARACTER
from tests.legacy_fog_factory import legacy_farmer, legacy_runtime
from tests.map_runtime_harness import MapRuntimeHarness


class MovementRecoveryTests(unittest.TestCase):
    @staticmethod
    def apply_hex_move(position: tuple[int, int], button: str) -> tuple[int, int]:
        x, y = position
        if button == "⬅️":
            return x - 1, y
        if button == "➡️":
            return x + 1, y
        if y % 2 == 0:
            offsets = {
                "↖️": (-1, -1),
                "↗️": (0, -1),
                "↙️": (-1, 1),
                "↘️": (0, 1),
            }
        else:
            offsets = {
                "↖️": (0, -1),
                "↗️": (1, -1),
                "↙️": (0, 1),
                "↘️": (1, 1),
            }
        dx, dy = offsets[button]
        return x + dx, y + dy

    def test_vertical_buttons_follow_hex_row_parity(self) -> None:
        self.assertEqual(
            SnakeNavigator._primary_button_between((3, 10), (3, 9)),
            "↗️",
        )
        self.assertEqual(
            SnakeNavigator._primary_button_between((3, 10), (3, 11)),
            "↘️",
        )
        self.assertEqual(
            SnakeNavigator._primary_button_between((3, 11), (3, 10)),
            "↖️",
        )
        self.assertEqual(
            SnakeNavigator._primary_button_between((3, 9), (3, 10)),
            "↙️",
        )

    def test_12x12_route_matches_real_hex_transitions(self) -> None:
        navigator = SnakeNavigator(0, 8, 0, 8)
        navigator.use_location(
            "Мертвый лес",
            current_position=(0, 0),
            width=12,
            height=12,
        )
        position = (0, 0)

        for _ in range(180):
            if navigator.coverage_count == navigator.coverage_total:
                break
            plan = navigator.plan(position)
            actual = self.apply_hex_move(position, plan.button)
            self.assertEqual(actual, plan.destination)
            navigator.confirm_success(plan, actual)
            position = actual

        self.assertEqual(navigator.coverage_count, 144)
        self.assertEqual(navigator.coverage_total, 144)

    def test_second_enemy_is_inferred_from_the_received_round(self) -> None:
        from automation_policy import TargetPolicy
        from legacy_combat_controller import LegacyCombatController
        from tests.combat_runtime_harness import Context, Runtime

        runtime = Runtime(
            targets=TargetPolicy(("Пенёк", "Летучая мышь")),
            context=Context(active_target="Пенёк", battle_target="Пенёк", combat_enemies=["Пенёк"]),
        )
        controller = LegacyCombatController(runtime, character_name=CHARACTER)
        controller.memory.begin("Пенёк")
        round_state = parse_combat_round(
            """⚔️ Раун 31
🪬🧙Kombat
❤️ 250/755
Летучая мышь
❤️ 475/475
Выберите навык:
🔷 Мана: 8/12""",
            ["Атака аколита"],
        )

        enemies = controller.observed_combat_enemies(round_state)
        self.assertEqual(enemies, ("Летучая мышь",))
        self.assertTrue(
            controller.switch_combat_enemy(
                enemies[0],
                reason="тест",
            )
        )
        self.assertEqual(controller.memory.target_name, "Летучая мышь")
        self.assertEqual(runtime.context.active_target, "Летучая мышь")
        self.assertIn("Летучая мышь", runtime.context.combat_enemies)
        self.assertIn("Боевая модель переключена", runtime.logs[0])

    def test_9x9_sweep_from_mid_map_reaches_every_cell(self) -> None:
        for start in ((0, 0), (0, 1), (0, 4), (2, 1), (4, 4), (8, 8)):
            with self.subTest(start=start):
                navigator = SnakeNavigator(0, 8, 0, 8)
                navigator.use_location(
                    "Поляна",
                    current_position=start,
                    width=9,
                    height=9,
                )
                position = start
                moves = 0

                while not navigator.cycle_can_finish():
                    plan = navigator.plan(position)
                    position = plan.destination
                    navigator.confirm_success(plan, position)
                    moves += 1
                    self.assertLessEqual(moves, 100)

                self.assertEqual(navigator.coverage_count, 81)
                self.assertEqual(navigator.coverage_total, 81)
                self.assertEqual({y for _, y in navigator.visited_positions}, set(range(9)))

    def test_large_map_keeps_configured_move_limit(self) -> None:
        navigator = SnakeNavigator(0, 8, 0, 8)
        navigator.use_location(
            "Выжженное поле",
            current_position=(0, 0),
            width=12,
            height=12,
        )

        self.assertTrue(navigator.cycle_can_finish())

    def test_unsent_plan_does_not_poison_next_button_choice(self) -> None:
        navigator = SnakeNavigator(0, 8, 0, 8)
        first = navigator.plan((2, 1))

        self.assertTrue(navigator.cancel_last_plan(first))
        second = navigator.plan((2, 1))

        self.assertEqual(second.destination, first.destination)
        self.assertEqual(second.button, first.button)

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
        runtime = MapRuntimeHarness(moves_in_cycle=7)
        legacy = LegacyMapController(
            runtime, character_name=CHARACTER, min_x=0, max_x=8, min_y=0, max_y=8
        )
        runtime.context.pending_move = legacy.navigator.plan((8, 0))

        actual_position = (6, 0)
        self.assertNotEqual(actual_position, runtime.context.pending_move.destination)
        legacy.confirm_pending_move(actual_position)

        self.assertEqual(runtime.context.move_count, 1)
        self.assertEqual(runtime.moves_in_cycle, 8)


class NavigationModelUpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def test_incompatible_obstacles_are_cleared_only_once(self) -> None:
        with TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "test.sqlite3")
            await storage.remember_map_obstacle("Мертвый лес", (1, 10))
            await storage.remember_map_obstacle("Мертвый лес", (5, 11))
            with patch("tests.legacy_fog_factory.create_test_client", return_value=MagicMock()):
                farmer = legacy_farmer(storage, MagicMock(), SettingsService(storage))
            discovery = legacy_runtime(farmer).discovery

            self.assertEqual(await discovery.initialize(), 2)
            self.assertEqual(await storage.get_map_obstacles("Мертвый лес"), set())
            self.assertEqual(
                await storage.get_setting("navigation_model_version"),
                SnakeNavigator.MODEL_VERSION,
            )

            await storage.remember_map_obstacle("Мертвый лес", (4, 11))
            self.assertEqual(await discovery.initialize(), 0)
            self.assertEqual(
                await storage.get_map_obstacles("Мертвый лес"),
                {(4, 11)},
            )
            await storage.close()


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
        navigator.use_location("Поляна", blocked, current_position=(0, 0), width=3, height=3)
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
