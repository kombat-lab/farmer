from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from models import BotState as CompatibilityBotState
from runtime_state import BotState
from storage import Storage


class StorageSettingsStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = Storage(Path(":memory:"))

    async def asyncTearDown(self) -> None:
        await self.store.close()

    async def test_settings_replace_and_delete_are_one_atomic_write_unit(self) -> None:
        await self.store.set_settings({"known": 1, "deprecated": 2, "future": {"version": 9}})
        self.store.connection.executescript("""
            CREATE TRIGGER fail_delete AFTER DELETE ON settings
            WHEN OLD.key='deprecated'
            BEGIN SELECT RAISE(FAIL, 'delete failed'); END;
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            await self.store.set_and_delete_settings({"known": 3, "new": 4}, {"deprecated"})
        self.assertFalse(self.store.connection.in_transaction)
        await self.store.add_event("TEST", "unrelated write")
        self.assertEqual(
            await self.store.get_settings(), {"known": 1, "deprecated": 2, "future": {"version": 9}}
        )
        self.store.connection.execute("DROP TRIGGER fail_delete")
        await self.store.set_and_delete_settings({"known": 3, "new": 4}, {"deprecated"})
        self.assertEqual(
            await self.store.get_settings(), {"known": 3, "new": 4, "future": {"version": 9}}
        )

    async def test_silently_ignored_setting_write_or_delete_is_not_published(self) -> None:
        await self.store.set_settings({"known": 1, "deprecated": 2})
        for name, operation, when in (
            ("ignore_update", "UPDATE", "NEW.key='known'"),
            ("ignore_delete", "DELETE", "OLD.key='deprecated'"),
        ):
            with self.subTest(operation=operation):
                self.store.connection.executescript(f"""
                    CREATE TRIGGER {name} BEFORE {operation} ON settings
                    WHEN {when} BEGIN SELECT RAISE(IGNORE); END;
                """)
                with self.assertRaises(RuntimeError):
                    await self.store.set_and_delete_settings({"known": 3}, {"deprecated"})
                self.assertEqual(await self.store.get_settings(), {"known": 1, "deprecated": 2})
                self.assertFalse(self.store.connection.in_transaction)
                self.store.connection.execute(f"DROP TRIGGER {name}")

    async def test_settings_json_and_all_keys_validate_before_transaction(self) -> None:
        await self.store.set_settings({"known": 1, "deprecated": 2})
        statements = []
        self.store.connection.set_trace_callback(statements.append)
        for values, keys in (
            ({"known": 3, "bad": float("inf")}, {"deprecated"}),
            ({"": 3}, {"deprecated"}),
            ({"known": 3}, {""}),
            ({"known": 3}, {"known"}),
        ):
            with self.subTest(values=values, keys=keys), self.assertRaises(ValueError):
                await self.store.set_and_delete_settings(values, keys)
        self.store.connection.set_trace_callback(None)
        self.assertFalse(any(statement.startswith("BEGIN") for statement in statements))
        self.assertEqual(await self.store.get_settings(), {"known": 1, "deprecated": 2})

    async def test_invalid_state_patch_rejects_every_field_before_writing(self) -> None:
        before = await self.store.get_state()
        invalid = (
            {"moves": -1},
            {"moves_in_cycle": -1},
            {"current_hp": -1},
            {"max_hp": -1},
            {"current_cycle": 0},
            {"cycles_count": 0},
            {"moves_per_cycle": 0},
            {"session_id": 0},
            {"pause_requested": True},
            {"pause_requested": 2},
            {"moves": None},
            {"position_x": True},
            {"position_y": 2**63},
            {"game_state": ""},
            {"game_state": " "},
            {"game_state": " DISCOVERY"},
            {"game_state": "discovery"},
            {"game_state": "DISCOVERY-HUNT"},
            {"game_state": "DISCOVERY\nHUNT"},
            {"game_state": "A" * 65},
            {"game_state": True},
            {"process_status": "FUTURE"},
            {"last_action": 5},
            {"active_target": {}},
            {"last_error": []},
            {"last_progress_at": "2026-09-14T10:00:00"},
            {"rest_until": "bad"},
        )
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                await self.store.update_state(**{"last_action": "must not leak", **fields})
            self.assertEqual(await self.store.get_state(), before)
            self.assertFalse(self.store.connection.in_transaction)

    async def test_persisted_state_decoder_rejects_corruption_without_coercion(self) -> None:
        original = await self.store.get_state()
        invalid_values = (
            ("process_status", "FUTURE"),
            ("game_state", ""),
            ("game_state", " "),
            ("game_state", " DISCOVERY"),
            ("game_state", "discovery"),
            ("game_state", "DISCOVERY-HUNT"),
            ("game_state", "DISCOVERY\x00HUNT"),
            ("game_state", "A" * 65),
            ("game_state", True),
            ("position_x", 1.5),
            ("current_hp", -1),
            ("active_target", b"not-text"),
            ("moves", 1.5),
            ("last_progress_at", "2026-09-14T10:00:00"),
            ("session_id", 0),
            ("current_cycle", 0),
            ("moves_in_cycle", -1),
            ("pause_requested", 2),
        )
        for field, invalid in invalid_values:
            with self.subTest(field=field):
                self.store.connection.execute(
                    f"UPDATE farmer_state SET {field}=? WHERE singleton=1", (invalid,)
                )
                self.store.connection.commit()
                with self.assertRaises(ValueError):
                    await self.store.get_state()
                self.assertFalse(self.store.connection.in_transaction)
                self.store.connection.execute(
                    f"UPDATE farmer_state SET {field}=? WHERE singleton=1",
                    (original[field],),
                )
                self.store.connection.commit()

        self.store.connection.execute(
            "UPDATE farmer_state SET last_progress_at=? WHERE singleton=1",
            ("2026-09-14T12:00:00+03:00",),
        )
        self.store.connection.commit()
        self.assertEqual(
            (await self.store.get_state())["last_progress_at"],
            "2026-09-14T09:00:00+00:00",
        )

    async def test_unknown_canonical_mechanism_phase_roundtrips(self) -> None:
        await self.store.update_state(game_state="DISCOVERY")
        self.assertEqual((await self.store.get_state())["game_state"], "DISCOVERY")

    async def test_known_state_error_and_aware_timestamps_use_shared_contract(self) -> None:
        self.assertIs(CompatibilityBotState, BotState)
        await self.store.update_state(
            process_status="ERROR", game_state="ERROR", last_error="failure"
        )
        state = await self.store.get_state()
        self.assertEqual((state["process_status"], state["game_state"]), ("ERROR", "ERROR"))
        await self.store.update_state(
            process_status="RUNNING",
            game_state=BotState.COMBAT.name,
            pause_requested=1,
            current_hp=0,
            max_hp=0,
            current_cycle=1,
            cycles_count=1,
            position_x=-3,
            position_y=0,
            last_progress_at="2026-09-14T12:00:00+03:00",
            rest_until=None,
            last_error=None,
        )
        state = await self.store.get_state()
        self.assertEqual(state["last_progress_at"], "2026-09-14T09:00:00+00:00")
        self.assertEqual(state["game_state"], "COMBAT")
        self.assertEqual(state["pause_requested"], 1)


if __name__ == "__main__":
    unittest.main()
