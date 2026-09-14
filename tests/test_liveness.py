from __future__ import annotations

import unittest
from dataclasses import replace

from legacy_liveness import liveness_is_suspended, liveness_phase
from liveness import LivenessPhase, LivenessPolicy, ProgressMonitor
from models import BotState


class LivenessTests(unittest.TestCase):
    def test_monitor_uses_injected_clock_and_phase_policy(self) -> None:
        now = 10.0
        monitor = ProgressMonitor(clock=lambda: now)
        policy = LivenessPolicy(30, 5, 10, 15, 20)

        now = 14.9
        self.assertFalse(monitor.should_recover(LivenessPhase.DISCOVERY_ACTION, policy))
        now = 15.0
        self.assertTrue(monitor.should_recover(LivenessPhase.DISCOVERY_ACTION, policy))
        self.assertEqual(monitor.begin_recovery_attempt(), 1)
        monitor.mark_progress("server confirmation")
        self.assertEqual(monitor.recovery_attempts, 0)
        self.assertEqual(monitor.reason, "server confirmation")

    def test_every_timeout_rejects_nonfinite_or_nonpositive_values(self) -> None:
        valid = LivenessPolicy(30, 5, 10, 15, 20)
        fields = (
            "general_timeout",
            "discovery_timeout",
            "target_timeout",
            "combat_timeout",
            "recovery_timeout",
        )
        for name in fields:
            for invalid in (0, -1, True, "5", float("nan"), float("inf"), float("-inf")):
                with self.subTest(field=name, value=invalid):
                    with self.assertRaisesRegex(ValueError, name):
                        replace(valid, **{name: invalid})

    def test_monitor_state_is_read_only_and_restore_is_validated(self) -> None:
        monitor = ProgressMonitor(clock=lambda: 10.0)
        with self.assertRaises(AttributeError):
            monitor.recovery_attempts = 2
        with self.assertRaises(AttributeError):
            monitor.last_progress_at = 1.0
        monitor.restore(last_progress_at=3.0, recovery_attempts=2, reason=" restored ")
        self.assertEqual(monitor.last_progress_at, 3.0)
        self.assertEqual(monitor.recovery_attempts, 2)
        self.assertEqual(monitor.reason, "restored")
        self.assertEqual(monitor.generation, 1)
        for invalid in (-1, True, 1.5):
            with self.subTest(recovery_attempts=invalid), self.assertRaises(ValueError):
                monitor.restore(recovery_attempts=invalid)

    def test_monitor_rejects_invalid_clock_and_reason(self) -> None:
        for value in (True, -1, float("nan"), float("inf"), "1"):
            with self.subTest(clock_value=value), self.assertRaises(ValueError):
                ProgressMonitor(clock=lambda value=value: value)
        monitor = ProgressMonitor(clock=lambda: 1.0)
        with self.assertRaises(ValueError):
            monitor.mark_progress("  ")

    def test_legacy_adapter_owns_bot_state_mapping(self) -> None:
        self.assertEqual(liveness_phase(BotState.MOVING), LivenessPhase.DISCOVERY_ACTION)
        self.assertEqual(liveness_phase(BotState.COMBAT), LivenessPhase.COMBAT)
        self.assertEqual(liveness_phase(BotState.MAP), LivenessPhase.GENERAL)
        self.assertTrue(liveness_is_suspended(BotState.PAUSED))
        self.assertFalse(liveness_is_suspended(BotState.MAP))


if __name__ == "__main__":
    unittest.main()
