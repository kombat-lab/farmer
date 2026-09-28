from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest

from game_catalog import LOCATION_NAMES, get_monster_names
from settings_service import FarmerSettings, SettingsService
from storage import Storage


class RichMessagePanelTests(unittest.TestCase):
    @staticmethod
    def _settings(snapshot: FarmerSettings | None = None) -> SettingsService:
        settings = SettingsService(Storage.__new__(Storage))
        if snapshot is not None:
            settings._snapshot = snapshot
        return settings

    def test_dashboard_uses_bot_api_10_3_controls_and_compact_tables(self) -> None:
        from rich_messages import dashboard_rich

        html = dashboard_rich(
            {
                "task_running": True,
                "game_state": "COMBAT",
                "location_name": "Мертвый лес",
                "position_x": 4,
                "position_y": 7,
                "current_hp": 712,
                "max_hp": 870,
                "active_target": "Черная мушка",
                "current_cycle": 1,
                "cycles_count": 1,
                "moves_in_cycle": 37,
                "moves_per_cycle": 96,
            },
            {
                "battle": {
                    "battles": 27,
                    "wins": 27,
                    "xp": 817,
                    "dust": 502,
                    "crystals": 3,
                },
                "drops": {"items": 16, "cards": 0},
            },
        )

        self.assertIn("<table bordered striped compact>", html)
        self.assertIn('<tg-button-row align="center">', html)
        self.assertIn('type="callback_data"', html)
        self.assertIn('style="danger"', html)
        self.assertIn("<blockquote expandable>", html)
        self.assertIn("Мертвый лес", html)

    def test_stats_omits_daily_telegram_history_but_keeps_restriction_control(self) -> None:
        from rich_messages import stats_rich

        html = stats_rich(
            {
                "battle": {
                    "battles": 0,
                    "wins": 0,
                    "defeats": 0,
                    "xp": 0,
                    "dust": 0,
                    "crystals": 0,
                },
                "drops": {"items": 0, "cards": 0},
                "state": {"moves": 0, "current_cycle": 1, "cycles_count": 1},
                "targets": [],
                "runtime_seconds": 0,
            }
        )

        self.assertNotIn("Telegram по дням", html)
        self.assertIn("Отметить ограничение", html)

    def test_active_targets_are_collapsed_and_selected_values_are_disabled(self) -> None:
        from rich_messages import combat_settings_rich, settings_rich

        settings = self._settings(replace(FarmerSettings(), battle_start_hp_percent=100))

        root = settings_rich(settings)
        combat = combat_settings_rich(settings)

        self.assertIn("<details><summary>🎯 Активные цели</summary>", root)
        self.assertNotIn("<details open><summary>🎯 Активные цели</summary>", root)
        self.assertIn('type="disabled" style="primary">100%</tg-button>', combat)
        self.assertIn('data="settings:hp:50"', combat)

    def test_callback_identifiers_fit_telegram_limit(self) -> None:
        import re

        from rich_messages import locations_rich, targets_rich

        locations = locations_rich([(name, 1, 2) for name in LOCATION_NAMES])
        targets = targets_rich(
            "Выжженное поле",
            [(name, True) for name in get_monster_names("Выжженное поле")],
            category_index=0,
        )

        callback_values = re.findall(r'data="([^"]+)"', locations + targets)
        self.assertTrue(callback_values)
        self.assertTrue(all(len(value.encode("utf-8")) <= 64 for value in callback_values))

    def test_target_buttons_are_grouped_by_selection_state(self) -> None:
        from rich_messages import locations_rich, targets_rich

        locations = locations_rich(
            [
                ("Полностью", 3, 3),
                ("Частично", 1, 3),
                ("Пусто", 0, 3),
            ]
        )
        targets = targets_rich(
            "Тестовая локация",
            [("Активный моб", True), ("Неактивный моб", False)],
            category_index=0,
        )

        self.assertIn("✅ Выбраны полностью", locations)
        self.assertIn("☑️ Выбраны частично", locations)
        self.assertIn("○ Не выбраны", locations)
        self.assertIn("Полностью · все", locations)
        self.assertIn('style="success"', locations)
        self.assertIn('style="primary"', locations)
        self.assertLess(locations.index("Полностью · все"), locations.index("Частично · 1/3"))
        self.assertLess(targets.index("✅ Активные"), targets.index("○ Не выбраны"))

    def test_aiogram_3_31_uses_typed_bot_api_10_3_fields(self) -> None:
        from aiogram.types import DisabledButton

        from control_bot import _inline_button, _inline_keyboard

        selected_button = _inline_button(
            "100%",
            "settings:hp:100",
            style="primary",
            disabled=True,
        )
        selected = selected_button.model_dump(exclude_none=True)
        prompt = _inline_keyboard(
            [[("Отмена", "input:cancel", "danger", False)]],
            force_reply=True,
        ).model_dump(exclude_none=True)

        self.assertIsInstance(selected_button.disabled, DisabledButton)
        self.assertEqual(selected["disabled"], {})
        self.assertNotIn("callback_data", selected)
        self.assertTrue(prompt["force_reply"])

    def test_aiogram_3_31_deserializes_bot_api_10_3_rich_blocks(self) -> None:
        from aiogram.types import (
            RichBlockButtons,
            RichBlockExpandableBlockQuotation,
            RichMessage,
        )

        message = RichMessage.model_validate(
            {
                "blocks": [
                    {
                        "type": "buttons",
                        "buttons": [
                            {
                                "text": "Домой",
                                "callback_data": "ui:home",
                                "style": "primary",
                            }
                        ],
                        "align": "center",
                    },
                    {
                        "type": "expandable_blockquote",
                        "text": "Диагностика",
                    },
                ]
            }
        )

        self.assertIsInstance(message.blocks[0], RichBlockButtons)
        self.assertIsInstance(message.blocks[1], RichBlockExpandableBlockQuotation)


class RichMessageDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_keeps_native_rich_buttons(self) -> None:
        from rich_messages import send_rich_with_fallback

        bot = SimpleNamespace(send_rich_message=AsyncMock())
        remove_keyboard = SimpleNamespace(name="remove")
        fallback_markup = SimpleNamespace(name="inline")

        await send_rich_with_fallback(
            bot,
            chat_id=42,
            html=(
                '<h2>Панель</h2><tg-button-row align="center">'
                '<tg-button type="callback_data" data="ui:home">Домой</tg-button>'
                "</tg-button-row>"
            ),
            fallback_text="Панель",
            reply_markup=remove_keyboard,
            fallback_reply_markup=fallback_markup,
        )

        arguments = bot.send_rich_message.await_args.kwargs
        self.assertIn("tg-button", arguments["rich_message"].html)
        self.assertIs(arguments["reply_markup"], remove_keyboard)

    async def test_edit_keeps_native_expandable_blockquote(self) -> None:
        from rich_messages import edit_rich_with_fallback

        bot = SimpleNamespace(edit_message_text=AsyncMock())
        markup = SimpleNamespace(name="inline")

        await edit_rich_with_fallback(
            bot,
            chat_id=42,
            message_id=7,
            html="<blockquote expandable>Диагностика</blockquote>",
            fallback_text="Диагностика",
            fallback_reply_markup=markup,
        )

        arguments = bot.edit_message_text.await_args.kwargs
        self.assertEqual(
            arguments["rich_message"].html,
            "<blockquote expandable>Диагностика</blockquote>",
        )
        self.assertNotIn("reply_markup", arguments)

    async def test_uneditable_panel_is_replaced_without_second_edit_attempt(self) -> None:
        from rich_messages import edit_rich_with_fallback

        replacement = SimpleNamespace(message_id=8)
        bot = SimpleNamespace(
            edit_message_text=AsyncMock(
                side_effect=TelegramBadRequest(
                    method=None,
                    message="Bad Request: message can't be edited",
                )
            ),
            send_rich_message=AsyncMock(return_value=replacement),
            send_message=AsyncMock(),
            delete_message=AsyncMock(return_value=True),
        )

        result = await edit_rich_with_fallback(
            bot,
            chat_id=42,
            message_id=7,
            html="<h2>Новая панель</h2>",
            fallback_text="Новая панель",
            fallback_reply_markup=None,
        )

        self.assertIs(result, replacement)
        bot.edit_message_text.assert_awaited_once()
        bot.send_rich_message.assert_awaited_once()
        bot.delete_message.assert_awaited_once_with(chat_id=42, message_id=7)
