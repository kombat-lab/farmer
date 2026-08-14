from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from auto_buff import AutoBuff
from bot_states import SettingsInput
from config import ADMIN_TELEGRAM_ID, TARGET_MONSTER_CATEGORIES
from middlewares import AdminOnlyMiddleware
from rich_messages import (
    drops_rich,
    events_rich,
    send_rich_with_fallback,
    settings_rich,
    stats_rich,
    status_rich,
)
from settings_service import SettingsService
from storage import Storage
from supervisor import FarmerSupervisor

logger = logging.getLogger("fog_farmer")

INFO_ROWS = [
    ["Состояние", "Статистика", "Дроп"],
    ["⚠️ События", "✨ Авто баф", "⚙️ Настройки"],
]
SETTINGS_ROWS = [
    ["Количество циклов", "Ходов в цикле"],
    ["Выбор мобов", "❤️ Персонаж"],
    ["⏱ Задержки"],
    ["↩️ Главное меню"],
]
CHARACTER_ROWS = [
    ["❤️ Порог лечения", "Максимум маны"],
    ["Сила лечения"],
    ["✨ Благословение"],
    ["↩️ Настройки"],
]
DELAYS_ROWS = [
    ["Перемещение", "⚔️ Открытие нападения"],
    ["Выбор цели", "✨ Использование навыка"],
    ["☕ Длинная пауза", "Шанс длинной паузы"],
    ["Передышка между циклами"],
    ["↩️ Настройки"],
]

AUTO_BUFF_BACK = "↩️ Главное меню"
TARGETS_ENABLE_ALL_PREFIX = "✅ Выбрать всех"
TARGETS_DISABLE_ALL_PREFIX = "❌ Снять всех"
LOCATION_BUTTON_PREFIX = "📍 "
BACK_TO_LOCATIONS_BUTTON = "↩️ Выбор локации"
CANCEL_BUTTON = "❌ Отмена"


def normalize_button_text(value: str | None) -> str:
    """Нормализует текст кнопок Telegram.

    Удаляет variation selector у emoji, неразрывные пробелы и лишние пробелы.
    Благодаря этому старые клавиатуры с ведущими пробелами тоже продолжают работать.
    """
    if not value:
        return ""
    return " ".join(
        value.replace("\ufe0f", "")
        .replace("\u00a0", " ")
        .replace("\u2007", " ")
        .replace("\u202f", " ")
        .strip()
        .split()
    )


def text_is(*expected_values: str):
    expected = {normalize_button_text(value) for value in expected_values}
    return F.text.func(lambda value: normalize_button_text(value) in expected)


def keyboard(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=value) for value in row] for row in rows],
        resize_keyboard=True,
        one_time_keyboard=True,
        is_persistent=False,
        input_field_placeholder="Выберите действие",
    )


def location_button(category: str) -> str:
    return f"{LOCATION_BUTTON_PREFIX}{category}"


def category_from_location_button(text: str | None) -> str | None:
    normalized = normalize_button_text(text)

    # Новый формат: «📍 Название».
    marker = normalize_button_text(LOCATION_BUTTON_PREFIX)
    if normalized.startswith(marker):
        normalized = normalize_button_text(normalized[len(marker) :])

    # Старый формат содержал только ведущий пробел, который Telegram мог удалить.
    for category in TARGET_MONSTER_CATEGORIES:
        if normalize_button_text(category) == normalized:
            return category
    return None


def category_for_target(target: str | None) -> str | None:
    normalized_target = normalize_button_text(target)
    for category, targets in TARGET_MONSTER_CATEGORIES.items():
        for configured_target in targets:
            if normalize_button_text(configured_target) == normalized_target:
                return category
    return None


def canonical_target_name(target: str | None) -> str | None:
    normalized_target = normalize_button_text(target)
    for targets in TARGET_MONSTER_CATEGORIES.values():
        for configured_target in targets:
            if normalize_button_text(configured_target) == normalized_target:
                return configured_target
    return None


def category_from_toggle_button(text: str | None) -> str | None:
    normalized = normalize_button_text(text)
    _, separator, category_text = normalized.partition(":")
    if not separator:
        return None

    normalized_category = normalize_button_text(category_text)
    for category in TARGET_MONSTER_CATEGORIES:
        if normalize_button_text(category) == normalized_category:
            return category
    return None


def is_location_button(text: str | None) -> bool:
    return category_from_location_button(text) is not None


def is_target_button(text: str | None) -> bool:
    return canonical_target_name(text) is not None


def starts_with_normalized(text: str | None, prefix: str) -> bool:
    return normalize_button_text(text).startswith(normalize_button_text(prefix))


def get_locations_text(settings: SettingsService) -> str:
    lines = ["📍 Выбор локации", "", "Выберите локацию для настройки мобов:"]
    for category in TARGET_MONSTER_CATEGORIES:
        enabled_count, total_count = settings.get_category_enabled_count(category)
        if total_count > 0 and enabled_count == total_count:
            icon = "✅"
        elif enabled_count > 0:
            icon = "☑️"
        else:
            icon = "❌"
        lines.append(f"{icon} {category} ({enabled_count}/{total_count})")
    return "\n".join(lines)


def get_locations_keyboard() -> list[list[str]]:
    rows = [[location_button(category)] for category in TARGET_MONSTER_CATEGORIES]
    rows.append(["↩️ Настройки"])
    return rows


def get_category_toggle_button(category: str, settings: SettingsService) -> str:
    targets = TARGET_MONSTER_CATEGORIES.get(category, [])
    selected = set(settings.values.enabled_targets or [])
    enabled = bool(targets) and all(target in selected for target in targets)
    prefix = TARGETS_DISABLE_ALL_PREFIX if enabled else TARGETS_ENABLE_ALL_PREFIX
    return f"{prefix}: {category}"


def get_targets_text(category: str, settings: SettingsService) -> str:
    targets = TARGET_MONSTER_CATEGORIES[category]
    selected = set(settings.values.enabled_targets or [])
    enabled_count, total_count = settings.get_category_enabled_count(category)
    icon = "✅" if total_count and enabled_count == total_count else "☑️" if enabled_count else "❌"
    lines = [f"🎯 {category}", "", f"{icon} Выбрано мобов: {enabled_count}/{total_count}", ""]
    lines.extend(f"{'✅' if target in selected else '❌'} {target}" for target in targets)
    return "\n".join(lines)


def get_targets_keyboard(category: str, settings: SettingsService) -> list[list[str]]:
    rows = [[get_category_toggle_button(category, settings)]]
    rows.extend([[target] for target in TARGET_MONSTER_CATEGORIES[category]])
    rows.append([BACK_TO_LOCATIONS_BUTTON])
    return rows


class ControlBot:
    def __init__(
        self,
        bot: Bot,
        storage: Storage,
        supervisor: FarmerSupervisor,
        settings: SettingsService,
        auto_buff: AutoBuff,
    ) -> None:
        self.bot = bot
        self.storage = storage
        self.supervisor = supervisor
        self.settings = settings
        self.auto_buff = auto_buff
        self.router = Router(name="control")
        self.dispatcher = Dispatcher(storage=MemoryStorage())
        self.polling_task: asyncio.Task | None = None
        self.router.message.outer_middleware(AdminOnlyMiddleware(ADMIN_TELEGRAM_ID))
        self._register_handlers()
        self.dispatcher.include_router(self.router)

    async def current_keyboard(self) -> ReplyKeyboardMarkup:
        state = await self.supervisor.status()
        running = bool(state.get("task_running"))
        game_state = str(state.get("game_state") or "STOPPED")
        if not running:
            controls = [["▶️ Запустить"]]
        elif game_state in {"PAUSED", "RESTING"}:
            controls = [["▶️ Продолжить", "⏹ Стоп"]]
        else:
            controls = [["⏸ Пауза", "⏹ Стоп"]]
        return keyboard(controls + INFO_ROWS)

    async def _send_text(
        self,
        message: Message,
        text: str,
        rows: list[list[str]] | None = None,
    ) -> None:
        try:
            reply_markup = keyboard(rows) if rows is not None else await self.current_keyboard()
            await message.answer(text, reply_markup=reply_markup)
        except TelegramNetworkError:
            logger.warning("Не удалось отправить ответ панели из-за сетевой ошибки")

    async def _send_rich(
        self,
        message: Message,
        html: str,
        fallback: str,
        rows: list[list[str]] | None = None,
    ) -> None:
        reply_markup = keyboard(rows) if rows is not None else await self.current_keyboard()
        await send_rich_with_fallback(
            self.bot,
            chat_id=message.chat.id,
            html=html,
            fallback_text=fallback,
            reply_markup=reply_markup,
        )

    def _register_handlers(self) -> None:
        r = self.router

        @r.message(Command("start"))
        @r.message(Command("menu"))
        async def start_handler(message: Message, state: FSMContext) -> None:
            await state.clear()
            await self._send_text(message, "FoG Farmer\n\nПанель управления готова.")

        @r.message(StateFilter(None), text_is("▶️ Запустить"))
        async def start_farmer(message: Message) -> None:
            _, result = await self.supervisor.start()
            await self._send_text(message, f"▶️ {result}")

        @r.message(StateFilter(None), text_is("⏸ Пауза"))
        async def pause_farmer(message: Message) -> None:
            _, result = await self.supervisor.pause()
            await self._send_text(message, f"⏸ {result}")

        @r.message(StateFilter(None), text_is("▶️ Продолжить"))
        async def resume_farmer(message: Message) -> None:
            _, result = await self.supervisor.resume()
            await self._send_text(message, f"▶️ {result}")

        @r.message(StateFilter(None), text_is("⏹ Стоп"))
        async def stop_farmer(message: Message) -> None:
            _, result = await self.supervisor.stop()
            await self._send_text(message, f"⏹ {result}")

        @r.message(StateFilter(None), text_is("✨ Авто баф"))
        async def auto_buff_menu(message: Message) -> None:
            status = await self.auto_buff.status()
            running = bool(status["running"])
            button = "⏹ Выключить автобаф" if running else "▶️ Включить автобаф"
            text = (
                "✨ Авто баф\n\n"
                f"Состояние: {'✅ включён' if running else '❌ выключен'}\n"
                f"Последний игрок: {status['last_player'] or '—'}\n"
                f"Успешно: {status['success_count']}\n"
                f"Ошибок: {status['error_count']}\n\n"
                "Автобаф принимает приглашение в группу, применяет "
                "«Благословение» и выходит из группы."
            )
            await self._send_text(message, text, [[button], [AUTO_BUFF_BACK]])

        @r.message(StateFilter(None), text_is("▶️ Включить автобаф"))
        async def enable_auto_buff(message: Message) -> None:
            if self.supervisor.is_running():
                await self._send_text(
                    message,
                    "Сначала остановите фармера: одна Telethon-сессия "
                    "не может использоваться одновременно.",
                    [["✨ Авто баф"], [AUTO_BUFF_BACK]],
                )
                return
            _, result = await self.auto_buff.start()
            await self._send_text(message, f"✨ {result}", [["✨ Авто баф"], [AUTO_BUFF_BACK]])

        @r.message(StateFilter(None), text_is("⏹ Выключить автобаф"))
        async def disable_auto_buff(message: Message) -> None:
            _, result = await self.auto_buff.stop()
            await self._send_text(message, f"⏹ {result}", [["✨ Авто баф"], [AUTO_BUFF_BACK]])

        @r.message(StateFilter(None), text_is("Состояние"))
        async def status_handler(message: Message) -> None:
            current = await self.supervisor.status()
            await self._send_rich(message, status_rich(current), "Состояние фармера получено.")

        @r.message(StateFilter(None), text_is("Статистика"))
        async def stats_handler(message: Message) -> None:
            dashboard = await self.storage.get_statistics_dashboard()
            await self._send_rich(
                message,
                stats_rich(dashboard),
                self.storage.format_statistics_text(dashboard),
            )

        @r.message(StateFilter(None), text_is("Дроп"))
        async def drops_handler(message: Message) -> None:
            session = await self.storage.get_current_session()
            drops = await self.storage.get_drops(session.session_id)
            await self._send_rich(message, drops_rich(drops), "Дроп текущей сессии загружен.")

        @r.message(StateFilter(None), text_is("⚠️ События"))
        async def events_handler(message: Message) -> None:
            events_list = await self.storage.get_events(15)
            await self._send_rich(
                message, events_rich(events_list), "⚠️ Последние события загружены."
            )

        @r.message(StateFilter(None), text_is("⚙️ Настройки"))
        async def settings_handler(message: Message) -> None:
            await self._send_rich(
                message, settings_rich(self.settings), "⚙️ Настройки", SETTINGS_ROWS
            )

        @r.message(StateFilter(None), text_is("↩️ Главное меню"))
        async def main_menu_handler(message: Message, state: FSMContext) -> None:
            await state.clear()
            await self._send_text(message, "Главное меню")

        @r.message(StateFilter(None), text_is("↩️ Настройки"))
        async def settings_back_handler(message: Message, state: FSMContext) -> None:
            await state.clear()
            await self._send_rich(
                message, settings_rich(self.settings), "⚙️ Настройки", SETTINGS_ROWS
            )

        @r.message(StateFilter(None), text_is("Количество циклов"))
        async def cycles_prompt(message: Message, state: FSMContext) -> None:
            await state.set_state(SettingsInput.cycles_count)
            await self._send_text(
                message,
                "Отправьте целое количество циклов: 1, 2, 3 и т.д.",
                [[CANCEL_BUTTON]],
            )

        @r.message(SettingsInput.cycles_count)
        async def cycles_input(message: Message, state: FSMContext) -> None:
            if normalize_button_text(message.text) == normalize_button_text(CANCEL_BUTTON):
                await state.clear()
                await self._send_text(message, "Ввод отменён.", SETTINGS_ROWS)
                return
            try:
                value = int(message.text or "")
                if value < 1:
                    raise ValueError
            except ValueError:
                await self._send_text(
                    message, "Введите целое число больше нуля.", [[CANCEL_BUTTON]]
                )
                return
            await self.settings.set_value("cycles_count", value)
            await state.clear()
            await self._send_text(message, f"✅ Количество циклов: {value}", SETTINGS_ROWS)

        @r.message(StateFilter(None), text_is("Ходов в цикле"))
        async def moves_prompt(message: Message, state: FSMContext) -> None:
            await state.set_state(SettingsInput.moves_per_cycle)
            await self._send_text(
                message,
                "Отправьте количество подтверждённых перемещений.",
                [[CANCEL_BUTTON]],
            )

        @r.message(SettingsInput.moves_per_cycle)
        async def moves_input(message: Message, state: FSMContext) -> None:
            if normalize_button_text(message.text) == normalize_button_text(CANCEL_BUTTON):
                await state.clear()
                await self._send_text(message, "Ввод отменён.", SETTINGS_ROWS)
                return
            try:
                value = int(message.text or "")
                if value < 1:
                    raise ValueError
            except ValueError:
                await self._send_text(
                    message, "Введите целое число больше нуля.", [[CANCEL_BUTTON]]
                )
                return
            await self.settings.set_value("moves_per_cycle", value)
            await state.clear()
            await self._send_text(message, f"✅ Ходов в цикле: {value}", SETTINGS_ROWS)

        @r.message(StateFilter(None), text_is("❤️ Персонаж"))
        async def character_settings_handler(message: Message) -> None:
            values = self.settings.values
            await self._send_text(
                message,
                "❤️ Параметры персонажа\n\n"
                f"Порог лечения: {values.heal_threshold} HP и ниже\n"
                f"Максимум маны: {values.max_mana}\n"
                f"Лечение: +{values.heal_amount} HP\n"
                f"Благословение: {'включено' if values.blessing_enabled else 'выключено'}",
                CHARACTER_ROWS,
            )

        @r.message(StateFilter(None), text_is("✨ Благословение"))
        async def blessing_toggle_handler(message: Message) -> None:
            enabled = await self.settings.toggle_blessing()
            state_text = "включено" if enabled else "выключено"
            await self._send_text(
                message,
                f"✨ Благословение {state_text}.\n\n"
                "При выключении фармер не проверяет баф, не открывает "
                "небоевые навыки и не применяет Благословение.",
                CHARACTER_ROWS,
            )

        character_fields = {
            normalize_button_text("❤️ Порог лечения"): ("heal_threshold", "порог лечения"),
            normalize_button_text("Максимум маны"): ("max_mana", "максимальную ману"),
            normalize_button_text("Сила лечения"): ("heal_amount", "силу лечения"),
        }

        @r.message(
            StateFilter(None),
            F.text.func(lambda value: normalize_button_text(value) in character_fields),
        )
        async def character_value_prompt(message: Message, state: FSMContext) -> None:
            key, label = character_fields[normalize_button_text(message.text)]
            await state.set_state(SettingsInput.character_value)
            await state.update_data(character_key=key, character_label=label)
            await self._send_text(message, f"Отправьте {label} целым числом.", [[CANCEL_BUTTON]])

        @r.message(SettingsInput.character_value)
        async def character_value_input(message: Message, state: FSMContext) -> None:
            if normalize_button_text(message.text) == normalize_button_text(CANCEL_BUTTON):
                await state.clear()
                await self._send_text(message, "Ввод отменён.", CHARACTER_ROWS)
                return
            try:
                value = int(message.text or "")
                self.settings.validate_character_value(value)
            except ValueError:
                await self._send_text(
                    message, "Введите целое число больше нуля.", [[CANCEL_BUTTON]]
                )
                return
            data = await state.get_data()
            await self.settings.set_value(str(data["character_key"]), value)
            await state.clear()
            await self._send_text(message, "✅ Параметр сохранён.", CHARACTER_ROWS)

        # Цепочка выбора мобов. Все проверки нормализованы и поддерживают старые кнопки.
        @r.message(StateFilter(None), text_is("Выбор мобов", BACK_TO_LOCATIONS_BUTTON))
        async def locations_handler(message: Message) -> None:
            await self._send_text(
                message,
                get_locations_text(self.settings),
                get_locations_keyboard(),
            )

        @r.message(StateFilter(None), F.text.func(is_location_button))
        async def location_targets_handler(message: Message) -> None:
            category = category_from_location_button(message.text)
            if category is None:
                await self._send_text(
                    message,
                    "Не удалось определить локацию.",
                    get_locations_keyboard(),
                )
                return
            await self._send_text(
                message,
                get_targets_text(category, self.settings),
                get_targets_keyboard(category, self.settings),
            )

        @r.message(
            StateFilter(None),
            F.text.func(lambda value: starts_with_normalized(value, TARGETS_ENABLE_ALL_PREFIX)),
        )
        async def enable_target_category(message: Message) -> None:
            category = category_from_toggle_button(message.text)
            if category is None:
                await self._send_text(
                    message,
                    "Не удалось определить локацию.",
                    get_locations_keyboard(),
                )
                return
            await self.settings.set_category_enabled(category, True)
            await self._send_text(
                message,
                get_targets_text(category, self.settings),
                get_targets_keyboard(category, self.settings),
            )

        @r.message(
            StateFilter(None),
            F.text.func(lambda value: starts_with_normalized(value, TARGETS_DISABLE_ALL_PREFIX)),
        )
        async def disable_target_category(message: Message) -> None:
            category = category_from_toggle_button(message.text)
            if category is None:
                await self._send_text(
                    message,
                    "Не удалось определить локацию.",
                    get_locations_keyboard(),
                )
                return
            await self.settings.set_category_enabled(category, False)
            await self._send_text(
                message,
                get_targets_text(category, self.settings),
                get_targets_keyboard(category, self.settings),
            )

        @r.message(StateFilter(None), F.text.func(is_target_button))
        async def target_toggle(message: Message) -> None:
            target = canonical_target_name(message.text)
            category = category_for_target(target)
            if target is None or category is None:
                await self._send_text(
                    message,
                    "Не удалось определить локацию моба.",
                    get_locations_keyboard(),
                )
                return
            await self.settings.toggle_target(target)
            await self._send_text(
                message,
                get_targets_text(category, self.settings),
                get_targets_keyboard(category, self.settings),
            )

        @r.message(StateFilter(None), text_is("⏱ Задержки"))
        async def delays_handler(message: Message) -> None:
            await self._send_rich(
                message,
                settings_rich(self.settings),
                "⏱ Настройки задержек",
                DELAYS_ROWS,
            )

        delay_mapping = {
            normalize_button_text("Перемещение"): "move_delay",
            normalize_button_text("⚔️ Открытие нападения"): "attack_delay",
            normalize_button_text("Выбор цели"): "target_delay",
            normalize_button_text("✨ Использование навыка"): "skill_delay",
            normalize_button_text("☕ Длинная пауза"): "long_pause",
            normalize_button_text("Передышка между циклами"): "cycle_rest",
        }

        @r.message(
            StateFilter(None),
            F.text.func(lambda value: normalize_button_text(value) in delay_mapping),
        )
        async def delay_prompt(message: Message, state: FSMContext) -> None:
            key = delay_mapping[normalize_button_text(message.text)]
            await state.set_state(SettingsInput.delay_range)
            await state.update_data(delay_key=key)
            unit = "минуты" if key == "cycle_rest" else "секунды"
            await self._send_text(
                message,
                f"Отправьте минимум и максимум через пробел ({unit}).\nПример: 5 15",
                [[CANCEL_BUTTON]],
            )

        @r.message(SettingsInput.delay_range)
        async def delay_input(message: Message, state: FSMContext) -> None:
            if normalize_button_text(message.text) == normalize_button_text(CANCEL_BUTTON):
                await state.clear()
                await self._send_text(message, "Ввод отменён.", DELAYS_ROWS)
                return
            try:
                values = (message.text or "").replace(",", ".").split()
                if len(values) != 2:
                    raise ValueError
                minimum, maximum = map(float, values)
                self.settings.validate_range(minimum, maximum)
            except ValueError:
                await self._send_text(
                    message, "Введите два числа: минимум и максимум.", [[CANCEL_BUTTON]]
                )
                return
            data = await state.get_data()
            key = str(data["delay_key"])
            if key == "cycle_rest":
                minimum *= 60
                maximum *= 60
            await self.settings.set_value(f"{key}_min", minimum)
            await self.settings.set_value(f"{key}_max", maximum)
            await state.clear()
            await self._send_text(message, "✅ Диапазон задержки сохранён.", DELAYS_ROWS)

        @r.message(StateFilter(None), text_is("Шанс длинной паузы"))
        async def chance_prompt(message: Message, state: FSMContext) -> None:
            await state.set_state(SettingsInput.long_pause_chance)
            await self._send_text(
                message,
                "Отправьте вероятность от 0 до 100 процентов.",
                [[CANCEL_BUTTON]],
            )

        @r.message(SettingsInput.long_pause_chance)
        async def chance_input(message: Message, state: FSMContext) -> None:
            if normalize_button_text(message.text) == normalize_button_text(CANCEL_BUTTON):
                await state.clear()
                await self._send_text(message, "Ввод отменён.", DELAYS_ROWS)
                return
            try:
                value = float((message.text or "").replace(",", "."))
                if not 0 <= value <= 100:
                    raise ValueError
            except ValueError:
                await self._send_text(message, "Введите число от 0 до 100.", [[CANCEL_BUTTON]])
                return
            await self.settings.set_value("long_pause_chance", value / 100)
            await state.clear()
            await self._send_text(message, f"✅ Шанс длинной паузы: {value:g}%", DELAYS_ROWS)

        @r.message()
        async def fallback_handler(message: Message, state: FSMContext) -> None:
            if normalize_button_text(message.text) == normalize_button_text(CANCEL_BUTTON):
                await state.clear()
                await self._send_text(message, "Ввод отменён.", SETTINGS_ROWS)
                return
            logger.warning("Неизвестная кнопка панели: %r", message.text)
            await self._send_text(
                message,
                "Кнопка не распознана. Откройте меню командой /menu.",
            )

    async def start(self) -> None:
        await self.bot.delete_webhook(drop_pending_updates=True)
        self.polling_task = asyncio.create_task(
            self.dispatcher.start_polling(
                self.bot,
                allowed_updates=self.dispatcher.resolve_used_update_types(),
                handle_signals=False,
                close_bot_session=False,
            ),
            name="aiogram-control-bot",
        )

    async def stop(self) -> None:
        if self.polling_task is None:
            return
        await self.dispatcher.stop_polling()
        try:
            await self.polling_task
        except asyncio.CancelledError:
            pass
        self.polling_task = None
