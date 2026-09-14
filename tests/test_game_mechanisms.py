from __future__ import annotations

import unittest
from dataclasses import fields

from bounded_values import INT64_MAX
from game_mechanisms import (
    CycleDescriptor,
    MechanismServices,
    MechanismSnapshot,
    require_mechanism_runtime,
)
from liveness import LivenessPhase


class MechanismContractTests(unittest.TestCase):
    def test_cycle_descriptor_rejects_values_outside_int64(self) -> None:
        with self.assertRaises(ValueError):
            CycleDescriptor(INT64_MAX + 1, 1, INT64_MAX + 1, "units")

    def test_snapshot_rejects_invalid_persistence_and_liveness_values(self) -> None:
        valid: dict[str, object] = {
            "phase_name": "DISCOVERY",
            "position": (4, 5),
            "location_name": "future-zone",
            "current_hp": 8,
            "max_hp": 10,
            "active_target": "target",
            "total_progress_units": 12,
            "cycle_progress_units": 3,
            "liveness_phase": LivenessPhase.GENERAL,
            "liveness_suspended": False,
        }
        invalid = (
            ("phase_name", " DISCOVERY"),
            ("position", [4, 5]),
            ("location_name", " future-zone"),
            ("current_hp", 11),
            ("active_target", " target"),
            ("total_progress_units", -1),
            ("cycle_progress_units", INT64_MAX + 1),
            ("cycle_progress_units", 13),
            ("liveness_phase", "GENERAL"),
            ("liveness_suspended", 0),
        )
        for name, value in invalid:
            with self.subTest(name=name), self.assertRaises(ValueError):
                MechanismSnapshot(**{**valid, name: value})  # type: ignore[arg-type]

    def test_services_reject_non_callable_capability(self) -> None:
        capabilities = {
            field.name: (lambda *_args, **_kwargs: None)
            for field in fields(MechanismServices)
        }
        capabilities["running"] = None
        with self.assertRaisesRegex(ValueError, "running"):
            MechanismServices(**capabilities)  # type: ignore[arg-type]

    def test_incomplete_runtime_is_rejected_at_build_boundary(self) -> None:
        with self.assertRaisesRegex(TypeError, "Invalid mechanism runtime contract"):
            require_mechanism_runtime(object())
