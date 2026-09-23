from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    DisabledButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)

from bot_states import SettingsInput
from config import ADMIN_TELEGRAM_ID
from game_catalog import LOCATION_NAMES, get_monster_names
from middlewares import AdminOnlyMiddleware
from rich_messages import (
    combat_settings_rich,
    dashboard_controls,
    dashboard_hint,
    dashboard_rich,
    dashboard_state_rows,
    delay_settings_rich,
    edit_rich_with_fallback,
    events_rich,
    farm_settings_rich,
    input_prompt_rich,
    locations_rich,
    send_rich_with_fallback,
    settings_rich,
    stats_rich,
    targets_rich,
)
from settings_service import (
    MAX_CYCLES_COUNT,
    MAX_HEAL_THRESHOLD,
    MAX_MOVES_PER_CYCLE,
    DelayKind,
    SettingsService,
)
from storage import Storage
from supervisor import FarmerSupervisor

logger = logging.getLogger("fog_farmer")


@dataclass(frozen=True, slots=True)
class PanelView:
    html: str
    fallback_text: str
    fallback_markup: InlineKeyboardMarkup


@dataclass(frozen=True, slots=True)
class InputSpec:
    state: State
    title: str
    instruction: str
    current_value: object
    return_screen: str


def _inline_button(
    text: str,
    callback_data: str,
    *,
    style: str | None = None,
    disabled: bool = False,
) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=None if disabled else callback_data,
        style=style,
        disabled=DisabledButton() if disabled else None,
    )


def _inline_keyboard(
    rows: list[list[tuple[str, str, str | None, bool]]],
    *,
    force_reply: bool = False,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _inline_button(text, callback, style=style, disabled=disabled)
                for text, callback, style, disabled in row
            ]
            for row in rows
        ],
        force_reply=True if force_reply else None,
    )


def _main_navigation(active: str) -> list[tuple[str, str, str | None, bool]]:
    return [
        ("Обзор", "ui:home", "primary" if active == "home" else None, active == "home"),
        (
            "Статистика",
            "ui:stats",
            "primary" if active == "stats" else None,
            active == "stats",
        ),
        (
            "Настройки",
            "ui:settings",
            "primary" if active == "settings" else None,
            active == "settings",
        ),
        (
            "Журнал",
            "ui:events",
            "primary" if active == "events" else None,
            active == "events",
        ),
    ]


class ControlBot:
    def __init__(
        self,
        bot: Bot,
        storage: Storage,
        supervisor: FarmerSupervisor,
        settings: SettingsService,
    ) -> None:
        self.bot = bot
        self.storage = storage
        self.supervisor = supervisor
        self.settings = settings
        self.router = Router(name="control")
        self.dispatcher = Dispatcher(storage=MemoryStorage())
        self.polling_task: asyncio.Task[None] | None = None
        middleware = AdminOnlyMiddleware(ADMIN_TELEGRAM_ID)
        self.router.message.outer_middleware(middleware)
        self.router.callback_query.outer_middleware(middleware)
        self._register_handlers()
        self.dispatcher.include_router(self.router)

    async def _panel_view(
        self,
        screen: str,
        *,
        notice: str | None = None,
        notice_error: bool = False,
    ) -> PanelView:
        if screen == "home":
            state = await self.supervisor.status()
            dashboard = await self.storage.get_statistics_dashboard()
            controls = dashboard_controls(state)
            controls.append(_main_navigation("home"))
            fallback = "FoG Farmer\n\n"
            if notice:
                fallback += f"{'⚠️' if notice_error else '✅'} {escape(notice)}\n\n"
            fallback += "\n".join(
                f"{escape(str(label))}: {escape(str(value))}"
                for label, value in dashboard_state_rows(state)
            )
            fallback += f"\n\n{escape(dashboard_hint(state))}\n\n"
            fallback += self.storage.format_statistics_text(dashboard)
            return PanelView(
                dashboard_rich(state, dashboard, notice=notice, notice_error=notice_error),
                fallback,
                _inline_keyboard(controls),
            )

        if screen == "stats":
            dashboard = await self.storage.get_statistics_dashboard()
            session = await self.storage.get_current_session()
            drops = await self.storage.get_drops(session.session_id)
            return PanelView(
                stats_rich(dashboard, drops, notice=notice),
                self.storage.format_statistics_text(dashboard),
                _inline_keyboard([
                    [
                        ("↻ Обновить", "ui:stats", "primary", False),
                        ("⛔ Отметить ограничение", "telegram:mark", "danger", False),
                    ],
                    _main_navigation("stats"),
                ]),
            )

        if screen == "events":
            events = await self.storage.get_events(15)
            fallback = "📋 Журнал\n\n" + (
                "\n".join(
                    f"{str(row['created_at'])[:19]} · {row['level']} · {row['message']}"
                    for row in events
                )
                if events
                else "Событий пока нет."
            )
            return PanelView(
                events_rich(events, notice=notice),
                fallback,
                _inline_keyboard([
                    [("↻ Обновить", "ui:events", "primary", False)],
                    _main_navigation("events"),
                ]),
            )

        if screen == "settings":
            return PanelView(
                settings_rich(self.settings, notice=notice),
                "⚙️ Настройки\n\nВыберите раздел.",
                _inline_keyboard([
                    [
                        ("🗺 План фарма", "settings:farm", "primary", False),
                        ("❤️ Бой", "settings:combat", "primary", False),
                    ],
                    [
                        ("🎯 Выбор мобов", "targets:locations", None, False),
                        ("⏱ Темп и передышки", "settings:delays", None, False),
                    ],
                    _main_navigation("settings"),
                ]),
            )

        if screen == "settings:farm":
            return PanelView(
                farm_settings_rich(self.settings, notice=notice),
                "🗺 План фарма\n\nИзмените циклы, диапазон ходов или активные цели.",
                _inline_keyboard([
                    [
                        ("Изменить циклы", "input:cycles", "primary", False),
                        ("Изменить ходы", "input:moves", "primary", False),
                    ],
                    [("🎯 Выбрать мобов", "targets:locations", None, False)],
                    [("← Настройки", "ui:settings", None, False)],
                ]),
            )

        if screen == "settings:combat":
            selected_hp = self.settings.values.battle_start_hp_percent
            return PanelView(
                combat_settings_rich(self.settings, notice=notice),
                "❤️ Персонаж и бой\n\n"
                f"Лечиться при {self.settings.values.heal_threshold} HP и ниже.\n"
                f"Входить в новый бой при {selected_hp}% HP.\n"
                "Кнопки 50% и 100% меняют здоровье для входа в бой.",
                _inline_keyboard([
                    [
                        ("❤️ Порог лечения", "input:heal", "primary", False),
                        (
                            "✨ Выключить благословение"
                            if self.settings.values.blessing_enabled
                            else "✨ Включить благословение",
                            "settings:blessing", None, False,
                        ),
                    ],
                    [("🧠 Боевой движок", "settings:planner", "primary", False)],
                    [
                        (
                            "50%",
                            "settings:hp:50",
                            "primary" if selected_hp == 50 else None,
                            selected_hp == 50,
                        ),
                        (
                            "100%",
                            "settings:hp:100",
                            "primary" if selected_hp == 100 else None,
                            selected_hp == 100,
                        ),
                    ],
                    [("← Настройки", "ui:settings", None, False)],
                ]),
            )

        if screen == "settings:delays":
            return PanelView(
                delay_settings_rich(self.settings, notice=notice),
                "⏱ Темп и передышки\n\nВыберите параметр. "
                "Передышка между циклами задаётся в минутах, остальные задержки — в секундах.",
                _inline_keyboard([
                    [
                        ("Перемещение", "input:delay:move_delay", None, False),
                        ("Нападение", "input:delay:attack_delay", None, False),
                    ],
                    [
                        ("Выбор моба", "input:delay:target_delay", None, False),
                        ("Применение навыка", "input:delay:skill_delay", None, False),
                    ],
                    [
                        ("Короткая пауза", "input:delay:long_pause", None, False),
                        ("Шанс паузы", "input:chance", None, False),
                    ],
                    [("Передышка между циклами", "input:delay:cycle_rest", None, False)],
                    [("← Настройки", "ui:settings", None, False)],
                ]),
            )

        if screen == "targets:locations":
            locations = [
                (category, *self.settings.get_category_enabled_count(category))
                for category in LOCATION_NAMES
            ]
            button_rows: list[list[tuple[str, str, str | None, bool]]] = []
            location_groups = (
                (
                    [
                        (index, category, enabled, total)
                        for index, (category, enabled, total) in enumerate(locations)
                        if total > 0 and enabled == total
                    ],
                    "success",
                ),
                (
                    [
                        (index, category, enabled, total)
                        for index, (category, enabled, total) in enumerate(locations)
                        if 0 < enabled < total
                    ],
                    "primary",
                ),
                (
                    [
                        (index, category, enabled, total)
                        for index, (category, enabled, total) in enumerate(locations)
                        if enabled == 0
                    ],
                    None,
                ),
            )
            for group, style in location_groups:
                location_buttons = [
                    (
                        f"{category} · "
                        f"{'все' if total and enabled == total else f'{enabled}/{total}'}",
                        f"targets:location:{index}",
                        style,
                        False,
                    )
                    for index, category, enabled, total in group
                ]
                for index in range(0, len(location_buttons), 2):
                    button_rows.append(location_buttons[index : index + 2])
            button_rows.append([("← Настройки", "ui:settings", None, False)])
            return PanelView(
                locations_rich(locations, notice=notice),
                "🎯 Активные цели\n\nВыберите локацию.",
                _inline_keyboard(button_rows),
            )

        if screen.startswith("targets:location:"):
            category_index = self._parse_index(screen.rsplit(":", 1)[-1], len(LOCATION_NAMES))
            category = LOCATION_NAMES[category_index]
            selected = set(self.settings.values.enabled_targets)
            targets = [(name, name in selected) for name in get_monster_names(category)]
            all_enabled = bool(targets) and all(enabled for _, enabled in targets)
            target_button_rows: list[list[tuple[str, str, str | None, bool]]] = [[
                (
                    "Снять всех" if all_enabled else "Выбрать всех",
                    f"targets:all:{category_index}:{0 if all_enabled else 1}",
                    "danger" if all_enabled else "success",
                    False,
                )
            ]]
            for enabled_group, style in ((True, "success"), (False, None)):
                target_buttons: list[tuple[str, str, str | None, bool]] = [
                    (
                        name,
                        f"targets:one:{category_index}:{target_index}",
                        style,
                        False,
                    )
                    for target_index, (name, enabled) in enumerate(targets)
                    if enabled is enabled_group
                ]
                for index in range(0, len(target_buttons), 2):
                    target_button_rows.append(target_buttons[index : index + 2])
            target_button_rows.append([("← Локации", "targets:locations", None, False)])
            return PanelView(
                targets_rich(
                    category,
                    targets,
                    category_index=category_index,
                    notice=notice,
                ),
                f"🎯 {category}\n\n"
                f"Выбрано: {sum(enabled for _, enabled in targets)}/{len(targets)}",
                _inline_keyboard(target_button_rows),
            )

        raise ValueError(f"Неизвестный экран панели: {screen}")

    @staticmethod
    def _parse_index(value: str, size: int) -> int:
        try:
            index = int(value)
        except ValueError as error:
            raise ValueError("Некорректный индекс панели") from error
        if not 0 <= index < size:
            raise ValueError("Индекс панели вне диапазона")
        return index

    async def _send_panel(self, message: Message, *, notice: str | None = None) -> None:
        view = await self._panel_view("home", notice=notice)
        try:
            await send_rich_with_fallback(
                self.bot,
                chat_id=message.chat.id,
                html=view.html,
                fallback_text=view.fallback_text,
                reply_markup=ReplyKeyboardRemove(),
                fallback_reply_markup=view.fallback_markup,
            )
        except TelegramNetworkError:
            logger.warning("Не удалось отправить панель из-за сетевой ошибки")

    async def _edit_panel(
        self,
        query: CallbackQuery,
        screen: str,
        *,
        notice: str | None = None,
        notice_error: bool = False,
    ) -> None:
        message = query.message
        if message is None:
            return
        view = await self._panel_view(screen, notice=notice, notice_error=notice_error)
        await edit_rich_with_fallback(
            self.bot,
            chat_id=message.chat.id,
            message_id=message.message_id,
            html=view.html,
            fallback_text=view.fallback_text,
            fallback_reply_markup=view.fallback_markup,
        )

    async def _edit_stored_panel(
        self,
        message: Message,
        state: FSMContext,
        screen: str,
        *,
        notice: str | None = None,
    ) -> None:
        data = await state.get_data()
        chat_id = int(data.get("panel_chat_id") or message.chat.id)
        panel_message_id = data.get("panel_message_id")
        view = await self._panel_view(screen, notice=notice)
        if panel_message_id is None:
            sent_panel = await send_rich_with_fallback(
                self.bot,
                chat_id=chat_id,
                html=view.html,
                fallback_text=view.fallback_text,
                reply_markup=ReplyKeyboardRemove(),
                fallback_reply_markup=view.fallback_markup,
            )
            await state.update_data(panel_message_id=sent_panel.message_id)
            return
        result = await edit_rich_with_fallback(
            self.bot,
            chat_id=chat_id,
            message_id=int(panel_message_id),
            html=view.html,
            fallback_text=view.fallback_text,
            fallback_reply_markup=view.fallback_markup,
        )
        replacement_id = getattr(result, "message_id", None)
        if replacement_id is not None and int(replacement_id) != int(panel_message_id):
            await state.update_data(panel_message_id=int(replacement_id))

    def _input_spec(self, kind: str) -> InputSpec:
        s = self.settings.values
        if kind == "cycles":
            return InputSpec(
                SettingsInput.cycles_count,
                "🔄 Количество циклов",
                f"Введите целое число от 1 до {MAX_CYCLES_COUNT}. Например: 1",
                s.cycles_count,
                "settings:farm",
            )
        if kind == "moves":
            return InputSpec(
                SettingsInput.moves_per_cycle,
                "👣 Диапазон ходов",
                f"Введите минимум и максимум от 1 до {MAX_MOVES_PER_CYCLE}. Например: 80 120",
                f"{s.moves_per_cycle_min}–{s.moves_per_cycle_max}",
                "settings:farm",
            )
        if kind == "heal":
            return InputSpec(
                SettingsInput.character_value,
                "❤️ Порог лечения",
                f"Введите порог лечения от 1 до {MAX_HEAL_THRESHOLD} HP.",
                s.heal_threshold,
                "settings:combat",
            )
        if kind == "chance":
            return InputSpec(
                SettingsInput.long_pause_chance,
                "☕ Шанс короткой паузы",
                "Введите вероятность от 0 до 100 процентов.",
                f"{s.long_pause_chance * 100:g}%",
                "settings:delays",
            )
        if kind.startswith("delay:"):
            key = kind.removeprefix("delay:")
            labels = {
                "move_delay": "Перемещение",
                "attack_delay": "Открытие нападения",
                "target_delay": "Выбор цели",
                "skill_delay": "Использование навыка",
                "long_pause": "Короткая пауза",
                "cycle_rest": "Передышка между циклами",
            }
            if key not in labels:
                raise ValueError("Неизвестная задержка")
            delay_kind = DelayKind(key)
            minimum, maximum = self.settings.get_delay_range(delay_kind)
            divisor = delay_kind.input_multiplier
            unit = "минуты" if key == "cycle_rest" else "секунды"
            return InputSpec(
                SettingsInput.delay_range,
                f"⏱ {labels[key]}",
                f"Введите минимум и максимум от 0 до "
                f"{delay_kind.limit_seconds / divisor:g} ({unit}). Например: 5 15",
                f"{minimum / divisor:g}–{maximum / divisor:g}",
                "settings:delays",
            )
        raise ValueError("Неизвестный параметр ввода")

    async def _show_input_prompt(
        self,
        query: CallbackQuery,
        state: FSMContext,
        kind: str,
        *,
        error: str | None = None,
    ) -> None:
        message = query.message
        if message is None:
            return
        spec = self._input_spec(kind)
        await state.set_state(spec.state)
        await state.update_data(
            input_kind=kind,
            return_screen=spec.return_screen,
            panel_chat_id=message.chat.id,
            panel_message_id=message.message_id,
        )
        html = input_prompt_rich(
            spec.title,
            spec.instruction,
            current_value=spec.current_value,
            error=error,
        )
        fallback_markup = _inline_keyboard(
            [[("Отменить", "input:cancel", "danger", False)]],
            force_reply=True,
        )
        result = await edit_rich_with_fallback(
            self.bot,
            chat_id=message.chat.id,
            message_id=message.message_id,
            html=html,
            fallback_text=f"{spec.title}\n\n{spec.instruction}",
            fallback_reply_markup=fallback_markup,
        )
        replacement_id = getattr(result, "message_id", None)
        if replacement_id is not None and int(replacement_id) != message.message_id:
            await state.update_data(panel_message_id=int(replacement_id))

    async def _retry_input(
        self,
        message: Message,
        state: FSMContext,
        error: str,
    ) -> None:
        data = await state.get_data()
        kind = str(data["input_kind"])
        spec = self._input_spec(kind)
        panel_message_id = data.get("panel_message_id")
        if panel_message_id is not None:
            result = await edit_rich_with_fallback(
                self.bot,
                chat_id=int(data.get("panel_chat_id") or message.chat.id),
                message_id=int(panel_message_id),
                html=input_prompt_rich(
                    spec.title,
                    spec.instruction,
                    current_value=spec.current_value,
                    error=error,
                ),
                fallback_text=f"{error}\n\n{spec.instruction}",
                fallback_reply_markup=_inline_keyboard(
                    [[("Отменить", "input:cancel", "danger", False)]],
                    force_reply=True,
                ),
            )
            replacement_id = getattr(result, "message_id", None)
            if replacement_id is not None and int(replacement_id) != int(panel_message_id):
                await state.update_data(panel_message_id=int(replacement_id))
        with suppress(TelegramBadRequest):
            await message.delete()

    async def _finish_input(
        self,
        message: Message,
        state: FSMContext,
        notice: str,
    ) -> None:
        data = await state.get_data()
        return_screen = str(data.get("return_screen") or "settings")
        await self._edit_stored_panel(message, state, return_screen, notice=notice)
        await state.clear()
        with suppress(TelegramBadRequest):
            await message.delete()

    def _register_handlers(self) -> None:
        r = self.router

        @r.message(Command("start"))
        @r.message(Command("menu"))
        async def start_handler(message: Message, state: FSMContext) -> None:
            await state.clear()
            await self._send_panel(message)

        @r.callback_query(F.data.in_({"ui:home", "ui:stats", "ui:settings", "ui:events"}))
        async def navigation_handler(query: CallbackQuery, state: FSMContext) -> None:
            await query.answer()
            await state.clear()
            await self._edit_panel(query, str(query.data).removeprefix("ui:"))

        @r.callback_query(F.data.in_({"settings:farm", "settings:combat", "settings:delays"}))
        async def settings_section_handler(query: CallbackQuery, state: FSMContext) -> None:
            await query.answer()
            await state.clear()
            await self._edit_panel(query, str(query.data))

        @r.callback_query(F.data == "telegram:mark")
        async def telegram_restriction_handler(query: CallbackQuery) -> None:
            bucket = datetime.now(UTC).replace(
                minute=0, second=0, microsecond=0
            ).isoformat()
            await self.storage.increment_telegram_activity(
                bucket,
                {"manual_restriction_marks": 1},
            )
            await self.storage.add_event(
                "TELEGRAM_RESTRICTION_MARKED",
                "Пользователь отметил наблюдаемое ограничение игрового бота",
                level="WARNING",
            )
            await query.answer("Ограничение зафиксировано", show_alert=True)
            await self._edit_panel(
                query,
                "stats",
                notice="Ограничение отмечено в текущем часовом интервале.",
            )

        @r.callback_query(F.data.startswith("rest:skip:"))
        async def skip_rest_handler(query: CallbackQuery, state: FSMContext) -> None:
            await query.answer()
            await state.clear()
            token = str(query.data).removeprefix("rest:skip:")
            success, result = await self.supervisor.skip_rest(token)
            await self._edit_panel(query, "home", notice=result, notice_error=not success)

        @r.callback_query(F.data.in_({"ctl:start", "ctl:pause", "ctl:resume", "ctl:stop"}))
        async def control_handler(query: CallbackQuery, state: FSMContext) -> None:
            await query.answer()
            await state.clear()
            action = str(query.data).removeprefix("ctl:")
            if action == "start":
                success, result = await self.supervisor.start()
            elif action == "pause":
                success, result = await self.supervisor.pause()
            elif action == "resume":
                success, result = await self.supervisor.resume()
            else:
                success, result = await self.supervisor.stop()
            await self._edit_panel(query, "home", notice=result, notice_error=not success)

        @r.callback_query(F.data.startswith("settings:hp:"))
        async def battle_hp_handler(query: CallbackQuery) -> None:
            percent_text = str(query.data).rsplit(":", 1)[-1]
            if percent_text not in {"50", "100"}:
                await query.answer("Некорректное значение", show_alert=True)
                return
            await query.answer()
            percent = int(percent_text)
            await self.settings.set_value("battle_start_hp_percent", percent)
            await self._edit_panel(
                query,
                "settings:combat",
                notice=f"Новые бои разрешены при {percent}% HP.",
            )

        @r.callback_query(F.data == "settings:blessing")
        async def blessing_handler(query: CallbackQuery) -> None:
            await query.answer()
            enabled = await self.settings.toggle_blessing()
            await self._edit_panel(
                query,
                "settings:combat",
                notice=f"Благословение {'включено' if enabled else 'выключено'}.",
            )

        @r.callback_query(F.data == "settings:planner")
        async def combat_planner_handler(query: CallbackQuery) -> None:
            labels = {
                "shadow": "Тень: только анализ",
                "guarded": "Осторожный: только доказуемые замены",
                "active": "Активный: экспериментальный планировщик",
            }
            await query.answer()
            mode = await self.settings.cycle_combat_planner_mode()
            await self._edit_panel(
                query,
                "settings:combat",
                notice=labels[mode],
            )

        @r.callback_query(F.data == "targets:locations")
        async def locations_handler(query: CallbackQuery, state: FSMContext) -> None:
            await query.answer()
            await state.clear()
            await self._edit_panel(query, "targets:locations")

        @r.callback_query(F.data.startswith("targets:location:"))
        async def location_handler(query: CallbackQuery) -> None:
            try:
                index = self._parse_index(str(query.data).rsplit(":", 1)[-1], len(LOCATION_NAMES))
            except ValueError:
                await query.answer("Локация не найдена", show_alert=True)
                return
            await query.answer()
            await self._edit_panel(query, f"targets:location:{index}")

        @r.callback_query(F.data.startswith("targets:all:"))
        async def category_toggle_handler(query: CallbackQuery) -> None:
            parts = str(query.data).split(":")
            try:
                index = self._parse_index(parts[2], len(LOCATION_NAMES))
                if parts[3] not in {"0", "1"}:
                    raise ValueError
                enabled = parts[3] == "1"
            except (ValueError, IndexError):
                await query.answer("Команда повреждена", show_alert=True)
                return
            await query.answer()
            category = LOCATION_NAMES[index]
            await self.settings.set_category_enabled(category, enabled)
            await self._edit_panel(
                query,
                f"targets:location:{index}",
                notice=f"{category}: {'выбраны все цели' if enabled else 'все цели сняты'}.",
            )

        @r.callback_query(F.data.startswith("targets:one:"))
        async def target_toggle_handler(query: CallbackQuery) -> None:
            parts = str(query.data).split(":")
            try:
                category_index = self._parse_index(parts[2], len(LOCATION_NAMES))
                category = LOCATION_NAMES[category_index]
                targets = get_monster_names(category)
                target_index = self._parse_index(parts[3], len(targets))
            except (ValueError, IndexError):
                await query.answer("Цель не найдена", show_alert=True)
                return
            await query.answer()
            target = targets[target_index]
            enabled = await self.settings.toggle_target(target)
            await self._edit_panel(
                query,
                f"targets:location:{category_index}",
                notice=f"{target}: {'включён' if enabled else 'отключён'}.",
            )

        @r.callback_query(F.data.startswith("input:"))
        async def input_prompt_handler(query: CallbackQuery, state: FSMContext) -> None:
            kind = str(query.data).removeprefix("input:")
            if kind == "cancel":
                await query.answer()
                data = await state.get_data()
                return_screen = str(data.get("return_screen") or "settings")
                await state.clear()
                await self._edit_panel(query, return_screen, notice="Ввод отменён.")
                return
            try:
                self._input_spec(kind)
            except ValueError:
                await query.answer("Параметр не найден", show_alert=True)
                return
            await query.answer()
            await self._show_input_prompt(query, state, kind)

        @r.message(SettingsInput.cycles_count)
        async def cycles_input(message: Message, state: FSMContext) -> None:
            try:
                value = int(message.text or "")
                await self.settings.set_cycles_count(value)
            except ValueError as error:
                await self._retry_input(message, state, str(error))
                return
            await self._finish_input(message, state, f"Количество циклов: {value}.")

        @r.message(SettingsInput.moves_per_cycle)
        async def moves_input(message: Message, state: FSMContext) -> None:
            try:
                minimum, maximum = self.settings.parse_moves_range(message.text or "")
            except ValueError:
                await self._retry_input(
                    message,
                    state,
                    "Введите два целых числа. Например: 80 120.",
                )
                return
            await self.settings.set_moves_range(minimum, maximum)
            await self._finish_input(message, state, f"Диапазон ходов: {minimum}–{maximum}.")

        @r.message(SettingsInput.character_value)
        async def character_input(message: Message, state: FSMContext) -> None:
            try:
                value = int(message.text or "")
                await self.settings.set_heal_threshold(value)
            except ValueError as error:
                await self._retry_input(message, state, str(error))
                return
            await self._finish_input(message, state, f"Порог лечения: {value} HP.")

        @r.message(SettingsInput.delay_range)
        async def delay_input(message: Message, state: FSMContext) -> None:
            data = await state.get_data()
            try:
                kind = DelayKind(str(data["input_kind"]).removeprefix("delay:"))
                minimum, maximum = self.settings.parse_delay_range(kind, message.text or "")
                await self.settings.set_delay_range(kind, minimum, maximum)
            except ValueError as error:
                await self._retry_input(message, state, str(error))
                return
            await self._finish_input(
                message,
                state,
                f"Диапазон сохранён: {minimum / kind.input_multiplier:g}–"
                f"{maximum / kind.input_multiplier:g}.",
            )

        @r.message(SettingsInput.long_pause_chance)
        async def chance_input(message: Message, state: FSMContext) -> None:
            try:
                value = float((message.text or "").replace(",", "."))
                await self.settings.set_long_pause_chance(value / 100)
            except ValueError:
                await self._retry_input(message, state, "Введите число от 0 до 100.")
                return
            await self._finish_input(message, state, f"Шанс короткой паузы: {value:g}%.")

        @r.message()
        async def fallback_handler(message: Message, state: FSMContext) -> None:
            await state.clear()
            logger.info(
                "Получено сообщение вне режима ввода: chat_id=%s, message_id=%s",
                message.chat.id,
                message.message_id,
            )
            await self._send_panel(message, notice="Используйте кнопки внутри панели.")

    async def start(self) -> None:
        await self.bot.delete_webhook(drop_pending_updates=True)
        with suppress(TelegramNetworkError):
            await self.bot.set_my_commands(
                [
                    BotCommand(command="menu", description="Открыть панель управления"),
                    BotCommand(command="start", description="Запустить интерфейс"),
                ]
            )
        self.polling_task = asyncio.create_task(
            self.dispatcher.start_polling(
                self.bot,
                allowed_updates=self.dispatcher.resolve_used_update_types(),
                # A single administrator's updates must finish in order. This also
                # lets stop_polling cancel/join the active handler before DB shutdown.
                handle_as_tasks=False,
                handle_signals=False,
                close_bot_session=False,
            ),
            name="aiogram-control-bot",
        )

    async def stop(self) -> None:
        self.supervisor.begin_shutdown()
        polling_task = self.polling_task
        if polling_task is None:
            return
        if not polling_task.done():
            # start() schedules polling; give it a chance to acquire its running lock.
            await asyncio.sleep(0)
            if not polling_task.done():
                try:
                    await self.dispatcher.stop_polling()
                except RuntimeError:
                    # The dispatcher never acquired its lock, so there are no child
                    # polling tasks to drain. Cancelling an active dispatcher directly
                    # would skip aiogram's public graceful-stop path.
                    polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Polling панели управления завершился с ошибкой")
        finally:
            if polling_task.done():
                self.polling_task = None
