from __future__ import annotations

import re
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram import Bot

from control_bot import ControlBot
from settings_service import SettingsService
from storage import Storage


class ControlInterfaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.storage = Storage(Path(":memory:"))
        self.settings = SettingsService(self.storage)
        await self.settings.load()
        self.supervisor = SimpleNamespace(
            status=AsyncMock(),
            skip_rest=AsyncMock(),
            resume=AsyncMock(),
            stop=AsyncMock(),
        )
        self.control = ControlBot(
            MagicMock(spec=Bot), self.storage, self.supervisor, self.settings
        )

    async def asyncTearDown(self) -> None:
        await self.storage.close()

    async def test_rest_controls_match_in_rich_and_fallback(self) -> None:
        self.supervisor.status.return_value = {
            "task_running": True,
            "game_state": "RESTING",
            "rest_token": "a" * 32,
            "rest_until": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            "current_hp": 0,
            "max_hp": 100,
            "active_target": "<mob>",
        }
        panel = await self.control._panel_view("home")
        rich_callbacks = re.findall(r'data="([^"]+)"', panel.html)
        fallback_callbacks = [
            button.callback_data
            for row in panel.fallback_markup.inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertEqual(rich_callbacks, fallback_callbacks)
        self.assertIn("rest:skip:" + "a" * 32, fallback_callbacks)
        self.assertNotIn("ctl:resume", fallback_callbacks)
        for content in (panel.html, panel.fallback_text):
            self.assertIn("Автопродолжение", content)
            self.assertIn("МСК", content)
            self.assertIn("0/100", content)
            self.assertIn("&lt;mob&gt;", content)
            self.assertNotIn("RESTING", content)
        self.assertIn("⏭ Пропустить передышку", panel.html)

    async def test_pause_stop_and_pending_pause_have_distinct_actions(self) -> None:
        cases = (
            ({"task_running": False, "game_state": "STOPPED"}, "ctl:start", "ctl:resume"),
            ({"task_running": True, "game_state": "PAUSED"}, "ctl:resume", "ctl:start"),
            (
                {"task_running": True, "game_state": "COMBAT", "pause_requested": 1},
                "ctl:stop",
                "ctl:pause",
            ),
        )
        for state, expected, absent in cases:
            with self.subTest(state=state):
                self.supervisor.status.return_value = state
                panel = await self.control._panel_view("home")
                callbacks = [
                    button.callback_data
                    for row in panel.fallback_markup.inline_keyboard
                    for button in row
                    if button.callback_data
                ]
                self.assertIn(expected, callbacks)
                self.assertNotIn(absent, callbacks)
                self.assertIn("ui:home", callbacks)

    async def test_unavailable_rest_has_no_live_skip_action(self) -> None:
        self.supervisor.status.return_value = {
            "task_running": True, "game_state": "RESTING", "rest_token": None,
        }
        panel = await self.control._panel_view("home")
        self.assertNotIn('data="rest:skip:', panel.html)
        self.assertNotIn('data="ctl:resume"', panel.html)

    async def test_skip_callback_passes_token_and_replaces_original_message(self) -> None:
        handler = next(
            entry.callback
            for entry in self.control.router.callback_query.handlers
            if entry.callback.__name__ == "skip_rest_handler"
        )
        self.control._edit_panel = AsyncMock()
        query = SimpleNamespace(data="rest:skip:token", answer=AsyncMock())
        state = SimpleNamespace(clear=AsyncMock())
        for success in (True, False):
            with self.subTest(success=success):
                self.supervisor.skip_rest.return_value = (success, "result")
                await handler(query, state)
                self.supervisor.skip_rest.assert_awaited_with("token")
                self.control._edit_panel.assert_awaited_with(
                    query, "home", notice="result", notice_error=not success
                )
        self.supervisor.resume.assert_not_awaited()

    async def test_failed_command_is_a_warning_in_both_formats(self) -> None:
        self.supervisor.status.return_value = {
            "task_running": True, "game_state": "PAUSED",
        }
        panel = await self.control._panel_view(
            "home", notice="Передышка уже закончилась.", notice_error=True
        )
        for content in (panel.html, panel.fallback_text):
            self.assertIn("⚠️ Передышка уже закончилась.", content)
            self.assertNotIn("✅ Передышка", content)


if __name__ == "__main__":
    unittest.main()
