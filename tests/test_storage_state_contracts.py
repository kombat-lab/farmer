from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import get_type_hints

from storage import Storage
from storage_types import FarmerState, FarmerStatePatch, RuntimeStatus


class StorageStateContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.storage = Storage(Path(":memory:"))

    async def asyncTearDown(self) -> None:
        await self.storage.close()

    async def test_state_and_dashboard_return_every_required_field(self) -> None:
        state = await self.storage.get_state()
        dashboard = await self.storage.get_statistics_dashboard()
        required = set(FarmerState.__required_keys__)
        self.assertEqual(set(state), required)
        self.assertEqual(set(dashboard["state"]), required)
        self.assertEqual(dashboard["state"], state)
        self.assertEqual(state["singleton"], 1)
        self.assertIsNone(state["current_hp"])
        self.assertIsNone(state["active_target"])

    async def test_partial_patch_preserves_omitted_and_clears_nullable_fields(self) -> None:
        await self.storage.update_state(moves=7, current_hp=400, active_target="Моль")
        partial: FarmerStatePatch = {"current_hp": None}
        await self.storage.update_state(**partial)
        state = await self.storage.get_state()
        self.assertEqual(state["moves"], 7)
        self.assertEqual(state["active_target"], "Моль")
        self.assertIsNone(state["current_hp"])
        await self.storage.update_state()
        self.assertEqual(await self.storage.get_state(), state)

    async def test_unknown_or_readonly_fields_reject_the_entire_patch(self) -> None:
        before = await self.storage.get_state()
        for unexpected in ({"typo": 1}, {"singleton": 2}, {"moves=999 --": 1}):
            with self.subTest(fields=unexpected):
                with self.assertRaisesRegex(ValueError, "Неизвестные поля состояния"):
                    await self.storage.update_state(moves=99, **unexpected)
                self.assertEqual(await self.storage.get_state(), before)

    async def test_missing_singleton_is_an_error_for_reads_dashboard_and_updates(self) -> None:
        self.storage.connection.execute("DELETE FROM farmer_state WHERE singleton=1")
        self.storage.connection.commit()
        with self.assertRaisesRegex(RuntimeError, "singleton=1"):
            await self.storage.get_state()
        with self.assertRaisesRegex(RuntimeError, "singleton=1"):
            await self.storage.get_statistics_dashboard()
        with self.assertRaisesRegex(RuntimeError, "singleton=1"):
            await self.storage.update_state(moves=99)
        self.assertFalse(self.storage.connection.in_transaction)

    def test_patch_matches_writable_read_model_fields_and_runtime_state_is_complete(self) -> None:
        read_fields = get_type_hints(FarmerState)
        patch_fields = get_type_hints(FarmerStatePatch)
        self.assertEqual(patch_fields, {
            name: field_type for name, field_type in read_fields.items() if name != "singleton"
        })
        self.assertFalse(FarmerState.__optional_keys__)
        self.assertFalse(FarmerStatePatch.__required_keys__)
        self.assertFalse(RuntimeStatus.__optional_keys__)
        self.assertGreater(RuntimeStatus.__required_keys__, FarmerState.__required_keys__)


@unittest.skipUnless(importlib.util.find_spec("mypy"), "mypy is available in the dev toolchain")
class StorageStateTypingTests(unittest.TestCase):
    def test_mypy_enforces_complete_reads_and_known_patch_fields(self) -> None:
        valid_source = """
from typing import assert_type
from storage import Storage
from storage_types import FarmerState, FarmerStatePatch, RuntimeStatus

async def use(storage: Storage, status: RuntimeStatus) -> None:
    patch: FarmerStatePatch = {"current_hp": None, "moves": 2}
    await storage.update_state(**patch)
    assert_type(await storage.get_state(), FarmerState)
    assert_type((await storage.get_state())["current_hp"], int | None)
    assert_type(status["task_running"], bool)
"""
        invalid_source = """
from storage import Storage
from storage_types import FarmerState, FarmerStatePatch

async def use(storage: Storage) -> None:
    await storage.update_state(typo=1)
    patch: FarmerStatePatch = {"singleton": 2}
    state: FarmerState = {}
"""
        project = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            command = [
                sys.executable, "-B", "-m", "mypy", "--follow-imports=silent",
                "--no-incremental", "--cache-dir", directory,
            ]
            valid = subprocess.run(
                [*command, "-c", valid_source], cwd=project, capture_output=True,
                text=True, check=False, timeout=30,
            )
            self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)
            invalid = subprocess.run(
                [*command, "-c", invalid_source], cwd=project, capture_output=True,
                text=True, check=False, timeout=30,
            )
            self.assertEqual(invalid.returncode, 1, invalid.stdout + invalid.stderr)
            self.assertIn('Unexpected keyword argument "typo"', invalid.stdout)
            self.assertIn('Extra key "singleton"', invalid.stdout)
            self.assertIn('Missing keys', invalid.stdout)


if __name__ == "__main__":
    unittest.main()
