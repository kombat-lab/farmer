from __future__ import annotations

import unittest

from blessing import BlessingManager
from game_input import ActionOutcome


class BlessingTests(unittest.IsolatedAsyncioTestCase):
    async def test_blessing_flow_uses_single_manager(self) -> None:
        manager = BlessingManager()
        actions: list[str] = []

        async def click_button(**kwargs) -> ActionOutcome:
            actions.append(str(kwargs["description"]))
            return ActionOutcome.SENT

        opened = await manager.try_open_from_map(
            click_button=click_button,
            log=lambda text: None,
            mark_progress=lambda text: None,
        )
        handled = await manager.handle_menu(
            object(),
            find_button=lambda message, **kwargs: (0, 0),
            click_button=click_button,
            mark_progress=lambda text: None,
        )
        confirmed = manager.confirm_from_text(
            "Благословение: +5 ко всем характеристикам на 30 мин",
            log=lambda text: None,
            mark_progress=lambda text: None,
        )

        self.assertTrue(opened)
        self.assertTrue(handled)
        self.assertTrue(confirmed)
        self.assertEqual(actions, ["Небоевые навыки", "Благословение"])
        self.assertFalse(manager.refresh_in_progress)
