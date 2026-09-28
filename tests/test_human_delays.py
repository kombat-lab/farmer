from __future__ import annotations

import random
import unittest

from automation_policy import (
    DelayRange,
)
from farmer import Farmer
from human_delays import ActivityBreakPlanner, HumanDelayModel, parse_remaining_seconds
from models import ActionType


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

    def test_observation_mode_keeps_configured_action_delays(self) -> None:
        farmer = Farmer.__new__(Farmer)
        farmer.delay_model = HumanDelayModel(random.Random(9))
        delay = DelayRange(10, 20)
        delays = [farmer.action_delay(delay_range=delay) for _action in ActionType]

        self.assertTrue(all(10.0 <= delay <= 20.0 for delay in delays))
