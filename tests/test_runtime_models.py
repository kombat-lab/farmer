from __future__ import annotations

import unittest
from types import SimpleNamespace

from automation_policy import (
    DelayRange,
    IntegerRange,
    LegacyMapPolicy,
)
from legacy_fog_mechanisms import LegacyFoGMechanismRuntime
from models import RuntimeContext


class ModelTests(unittest.TestCase):
    def test_runtime_context_lists_are_not_shared(self) -> None:
        first = RuntimeContext()
        second = RuntimeContext()
        first.combat_enemies.append("Черная мушка")
        self.assertEqual(second.combat_enemies, [])

    def test_cycle_move_target_uses_configured_range(self) -> None:
        policy = LegacyMapPolicy(
            moves_per_cycle=IntegerRange(97, 97),
            blessing_enabled=False,
            move_delay=DelayRange(0, 0),
            open_attack_delay=DelayRange(0, 0),
            target_selection_delay=DelayRange(0, 0),
        )
        runtime = LegacyFoGMechanismRuntime.__new__(LegacyFoGMechanismRuntime)
        runtime._settings = SimpleNamespace(legacy_map_policy=lambda: policy)
        runtime._initialized = True
        runtime._cycle = None
        runtime.moves_in_cycle = 12

        descriptor = runtime.start_cycle(1)
        self.assertEqual(descriptor.target, 97)
        self.assertEqual((descriptor.minimum, descriptor.maximum), (97, 97))
        self.assertEqual(runtime.moves_in_cycle, 0)
