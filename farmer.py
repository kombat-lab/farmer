from __future__ import annotations

import asyncio
import random
import time
from contextlib import suppress
from pathlib import Path
from statistics import FarmStatistics, format_report
from typing import Any

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RPCError

from blessing import BlessingManager
from combat_events import parse_combat_round_events
from config import (
    API_HASH,
    API_ID,
    CHARACTER_NAME,
    COMBAT_PROGRESS_TIMEOUT,
    DATA_RETENTION_DAYS,
    DEATH_RECOVERY_MAX_WAIT,
    DEATH_RECOVERY_MIN_WAIT,
    GAME_BOT,
    GENERAL_PROGRESS_TIMEOUT,
    LOG_BACKUP_COUNT,
    LOG_DIRECTORY,
    LOG_FILENAME,
    LOG_MAX_BYTES,
    LOG_RETENTION_DAYS,
    MAP_MAX_X,
    MAP_MAX_Y,
    MAP_MIN_X,
    MAP_MIN_Y,
    MAX_RECOVERY_ATTEMPTS,
    MIN_HP_AFTER_DEATH,
    MIN_HP_PERCENT_TO_START_BATTLE,
    MOVE_PROGRESS_TIMEOUT,
    RECOVERY_WATCHDOG_TIMEOUT,
    SESSION_NAME,
    TARGET_SELECTION_TIMEOUT,
    TELEGRAM_ACTION_LIMIT,
    TELEGRAM_ACTION_MIN_INTERVAL,
    TELEGRAM_ACTION_WINDOW,
    TELEGRAM_RECOVERY_LIMIT,
    TELEGRAM_RECOVERY_WINDOW,
    WATCHDOG_CHECK_INTERVAL,
)
from event_cache import BoundedKeyCache
from logger_setup import setup_logging
from models import (
    ActionType,
    BotState,
    MessageKind,
    RuntimeContext,
)
from navigator import SnakeNavigator
from notifications import Notifier
from parser import (
    classify_message,
    extract_combat_target,
    extract_player_hp,
    normalize,
    parse_map,
)
from rewards import parse_battle_reward
from settings_service import SettingsService
from skills import choose_skill, parse_current_mana
from storage import Storage, utc_now
from targeting import analyze_map_targets, select_combat_target
from telegram_buttons import find_button, get_button_texts
from telegram_safety import (
    RollingAttemptGuard,
    StateRefreshGate,
    TelegramActionLimiter,
    message_state_key,
)
from watchdog import ProgressWatchdog

logger = setup_logging(
    log_directory=LOG_DIRECTORY,
    log_filename=LOG_FILENAME,
    max_bytes=LOG_MAX_BYTES,
    backup_count=LOG_BACKUP_COUNT,
)


ATTACK_BUTTON = "⚔️ Напасть"
BACK_TO_MAP_BUTTON = "↩️ К карте"
LOOK_BUTTON = "👀 Осмотреться"
MAP_COMMAND = "Карта"

MAX_FAILED_MOVE_ATTEMPTS = len(SnakeNavigator.ALL_MOVE_BUTTONS)
EVENT_QUEUE_SIZE = 200
PROCESSED_EVENT_CACHE_SIZE = 500
LATEST_MESSAGE_CACHE_SIZE = 200
MAX_CALLBACK_TIMEOUTS = 2

class Farmer:
    def __init__(self, storage: Storage, notifier: Notifier, settings: SettingsService) -> None:
        self.storage = storage
        self.notifier = notifier
        self.settings = settings
        self.session_id: int | None = None
        self.stop_reason: str | None = None

        self.client = TelegramClient(
            SESSION_NAME,
            API_ID,
            API_HASH,
            # Even a short FLOOD_WAIT must be visible to the farmer. Silently
            # sleeping and retrying would hide the first server-side warning.
            flood_sleep_threshold=0,
        )

        self.game_bot: Any | None = None
        self.state = BotState.STARTING
        self.running = True

        self.context = RuntimeContext()
        self.statistics = FarmStatistics()

        self.navigator = SnakeNavigator(
            min_x=MAP_MIN_X,
            max_x=MAP_MAX_X,
            min_y=MAP_MIN_Y,
            max_y=MAP_MAX_Y,
        )

        self.watchdog = ProgressWatchdog()
        self.watchdog_task: asyncio.Task | None = None
        self.recovery_task: asyncio.Task | None = None
        self.recovery_started_at: float | None = None
        self.recovery_refresh_requested = False
        self.pause_requested = False
        self.current_cycle = 1
        self.moves_in_cycle = 0
        self.rest_task: asyncio.Task | None = None
        self.progress_persist_task: asyncio.Task | None = None
        self.pending_progress_reason: str | None = None
        self.intentional_waits = 0

        self.event_queue: asyncio.Queue = asyncio.Queue(maxsize=EVENT_QUEUE_SIZE)
        self.worker_task: asyncio.Task | None = None

        self.processed_events = BoundedKeyCache(PROCESSED_EVENT_CACHE_SIZE)
        self.latest_messages: dict[int, Any] = {}
        self.latest_received_message: Any | None = None
        self.callback_timeout_count = 0
        self.attempted_actions = BoundedKeyCache(PROCESSED_EVENT_CACHE_SIZE)
        self.inbound_generation = 0
        self.state_refresh_gate = StateRefreshGate()
        self.recovery_attempt_guard = RollingAttemptGuard(
            max_attempts=TELEGRAM_RECOVERY_LIMIT,
            window_seconds=TELEGRAM_RECOVERY_WINDOW,
        )
        self.telegram_action_limiter = TelegramActionLimiter(
            min_interval=TELEGRAM_ACTION_MIN_INTERVAL,
            max_actions=TELEGRAM_ACTION_LIMIT,
            window_seconds=TELEGRAM_ACTION_WINDOW,
        )

        self.blessing = BlessingManager()

    def log(self, text: str) -> None:
        logger.info("[%s] %s", self.state.name, text)

    def mark_progress(self, reason: str) -> None:
        self.watchdog.mark_progress(reason)
        self.pending_progress_reason = reason
        if self.running and (
            self.progress_persist_task is None or self.progress_persist_task.done()
        ):
            self.progress_persist_task = asyncio.create_task(self._persist_progress())

    async def _persist_progress(self) -> None:
        # Coalesce bursts into one writer. The most recent state is what the
        # control bot needs; spawning one SQLite task per update can otherwise
        # amplify a bad incoming-message loop into thousands of pending writes.
        while self.running and self.pending_progress_reason is not None:
            reason = self.pending_progress_reason
            self.pending_progress_reason = None
            await self.storage.update_state(
                game_state=self.state.name,
                position_x=(
                    self.context.current_position[0] if self.context.current_position else None
                ),
                position_y=(
                    self.context.current_position[1] if self.context.current_position else None
                ),
                current_hp=self.context.current_hp,
                max_hp=self.context.max_hp,
                active_target=self.context.active_target,
                moves=self.context.move_count,
                max_moves=self.settings.values.moves_per_cycle,
                last_action=reason,
                last_progress_at=utc_now(),
                session_id=self.session_id,
                current_cycle=self.current_cycle,
                cycles_count=self.settings.values.cycles_count,
                moves_in_cycle=self.moves_in_cycle,
                moves_per_cycle=self.settings.values.moves_per_cycle,
                pause_requested=int(self.pause_requested),
            )

    def validate_config(self) -> None:
        if not isinstance(API_ID, int) or API_ID <= 0:
            raise ValueError("API_ID должен быть положительным числом.")

        if not isinstance(API_HASH, str) or not API_HASH.strip():
            raise ValueError("API_HASH не заполнен.")

        if not isinstance(GAME_BOT, str) or not GAME_BOT.startswith("@"):
            raise ValueError("GAME_BOT должен начинаться с @.")

        if not CHARACTER_NAME.strip():
            raise ValueError("CHARACTER_NAME не заполнен.")

        if not self.settings.values.enabled_targets:
            raise ValueError("Не выбран ни один моб для нападения.")

    @staticmethod
    def event_key(message) -> tuple:
        return message_state_key(message)

    def cache_latest_message(self, message) -> bool:
        """Stores an inbound update and rejects older revisions of the same message."""
        previous = self.latest_messages.get(message.id)
        if previous is not None:
            previous_edit = previous.edit_date.timestamp() if previous.edit_date else 0.0
            current_edit = message.edit_date.timestamp() if message.edit_date else 0.0
            if current_edit < previous_edit:
                return False

        if previous is None and len(self.latest_messages) >= LATEST_MESSAGE_CACHE_SIZE:
            oldest_id = next(iter(self.latest_messages))
            self.latest_messages.pop(oldest_id, None)

        self.latest_messages[message.id] = message
        return True

    def is_latest_message(self, message) -> bool:
        latest_for_id = self.latest_messages.get(message.id)
        latest_global = self.latest_received_message
        return (
            latest_for_id is not None
            and latest_global is not None
            and self.event_key(latest_for_id) == self.event_key(message)
            and self.event_key(latest_global) == self.event_key(message)
        )

    def remember_event(self, message) -> bool:
        return self.processed_events.remember(self.event_key(message))

    async def enqueue_message(
        self,
        message,
    ) -> None:
        if not self.running:
            return

        is_latest = self.cache_latest_message(message)
        if not is_latest:
            return

        is_new_state = self.remember_event(message)
        if not is_new_state:
            if (
                self.latest_received_message is not None
                and self.latest_received_message.id == message.id
                and self.event_key(self.latest_received_message) == self.event_key(message)
            ):
                self.latest_received_message = message
            return
        self.latest_received_message = message
        self.inbound_generation += 1

        try:
            self.event_queue.put_nowait(message)
        except asyncio.QueueFull:
            await self.stop("очередь Telegram-событий переполнена")

    async def event_worker(self) -> None:
        while self.running:
            message = await self.event_queue.get()

            try:
                # While an older revision was waiting in the queue, Telethon
                # may already have delivered a newer edit. Only the newest
                # revision is allowed to make a decision or press a button.
                if not self.is_latest_message(message):
                    continue
                await self.handle_message(message)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self.stop(f"ошибка обработки сообщения: {type(error).__name__}: {error}")
            finally:
                self.event_queue.task_done()

    def action_delay(self, action_type: ActionType) -> float:
        s = self.settings.values
        ranges = {
            ActionType.MOVE: (s.move_delay_min, s.move_delay_max),
            ActionType.OPEN_ATTACK: (s.attack_delay_min, s.attack_delay_max),
            ActionType.SELECT_TARGET: (s.target_delay_min, s.target_delay_max),
            ActionType.USE_SKILL: (s.skill_delay_min, s.skill_delay_max),
        }
        minimum, maximum = ranges[action_type]
        return random.uniform(minimum, maximum)

    async def intentional_sleep(self, seconds: float) -> None:
        """Marks configured human-like waits so watchdog does not recover over them."""
        self.intentional_waits += 1
        try:
            await asyncio.sleep(seconds)
        finally:
            self.intentional_waits -= 1

    async def stop_for_flood_wait(self, error: FloodWaitError, action: str) -> None:
        seconds = max(1, int(error.seconds))
        reason = f"Telegram FLOOD_WAIT на {seconds} сек. при действии: {action}"
        await self.storage.add_event(
            "TELEGRAM_FLOOD_WAIT",
            reason,
            level="CRITICAL",
            payload={"seconds": seconds, "action": action},
        )
        await self.notifier.send(
            "⛔️ <b>Telegram ограничил действия</b>\n"
            f"Ожидание: {seconds} сек.\n"
            f"Действие: {action}\n"
            "Фармер остановлен, автоповторов не будет."
        )
        await self.stop(reason)

    async def press_button(
        self,
        message,
        row: int,
        column: int,
        description: str,
    ) -> bool:
        action_key = (*self.event_key(message), description)
        if action_key in self.attempted_actions:
            reason = (
                "игра не обновила состояние после inline-действия; "
                f"повтор «{description}» заблокирован"
            )
            await self.storage.add_event(
                "REPEATED_TELEGRAM_ACTION_BLOCKED",
                reason,
                level="CRITICAL",
            )
            await self.notifier.send(
                "⛔️ <b>Повтор inline-действия заблокирован</b>\n"
                "После предыдущего нажатия игра не прислала "
                "новое состояние. Фармер остановлен вместо повторного запроса."
            )
            await self.stop(reason)
            return False

        limiter_delay = await self.telegram_action_limiter.acquire()
        if limiter_delay >= 0.05:
            self.log(f"Защитный лимит Telegram добавил {limiter_delay:.1f} сек. перед действием.")

        if not self.running or not self.is_latest_message(message):
            self.log(f"Отменено устаревшее действие: {description}")
            return False

        self.attempted_actions.remember(action_key)

        try:
            await message.click(row, column)
            self.callback_timeout_count = 0
            return True
        except FloodWaitError as error:
            await self.stop_for_flood_wait(error, description)
            return False
        except RPCError as error:
            if getattr(error, "message", "") != "BOT_RESPONSE_TIMEOUT":
                self.log(
                    f"Telegram не выполнил нажатие «{description}»: {type(error).__name__}: {error}"
                )
                return False

            self.callback_timeout_count += 1
            self.log(
                f"Telegram не получил ответ на inline-кнопку "
                f"«{description}» ({self.callback_timeout_count}/{MAX_CALLBACK_TIMEOUTS}): {error}"
            )
            if self.callback_timeout_count >= MAX_CALLBACK_TIMEOUTS:
                reason = "повторный BOT_RESPONSE_TIMEOUT; возможно ограничение inline-кнопок"
                await self.storage.add_event(
                    "TELEGRAM_CALLBACK_TIMEOUT",
                    reason,
                    level="CRITICAL",
                )
                await self.notifier.send(
                    "⛔️ <b>Не работают inline-кнопки</b>\n"
                    "Telegram дважды подряд не получил ответ "
                    "от игрового бота. Фармер остановлен без повторов."
                )
                await self.stop(reason)
            return False

    async def click_button(
        self,
        message,
        *,
        action_type: ActionType,
        description: str,
        exact: str | None = None,
        contains: tuple[str, ...] = (),
        exclude: tuple[str, ...] = (),
    ) -> bool:
        delay = self.action_delay(action_type)

        self.log(f"Ожидание {delay:.1f} сек. перед действием: {description}")
        await self.intentional_sleep(delay)

        if not self.running:
            return False

        if not self.is_latest_message(message):
            self.log(f"Отменено устаревшее действие: {description}")
            return False

        position = find_button(
            message,
            exact=exact,
            contains=contains,
            exclude=exclude,
        )
        if position is None:
            self.log(
                f"Кнопка «{description}» больше недоступна. "
                f"Текущие кнопки: {get_button_texts(message)}"
            )
            return False

        row, column = position

        self.log(f"Нажимаю: {description}")
        return await self.press_button(message, row, column, description)

    def update_hp(self, text: str) -> bool:
        hp = extract_player_hp(text, CHARACTER_NAME)
        if hp is None:
            return False

        current_hp, max_hp = hp
        changed = current_hp != self.context.current_hp or max_hp != self.context.max_hp
        if changed:
            self.context.current_hp = current_hp
            self.context.max_hp = max_hp
            self.log(f"Здоровье обновлено: {current_hp}/{max_hp}")
        return changed

    def has_battle_health(self) -> bool:
        current_hp = self.context.current_hp
        max_hp = self.context.max_hp
        if current_hp is None or max_hp is None or max_hp <= 0:
            return False
        return current_hp * 100 >= max_hp * MIN_HP_PERCENT_TO_START_BATTLE

    def battle_health_is_low(self) -> bool:
        return (
            self.context.current_hp is not None
            and self.context.max_hp is not None
            and self.context.max_hp > 0
            and not self.has_battle_health()
        )

    async def wait_for_battle_health(self) -> None:
        if self.state is BotState.WAITING_FOR_HEALTH:
            return
        self.state = BotState.WAITING_FOR_HEALTH
        current_hp = self.context.current_hp or 0
        max_hp = self.context.max_hp or 0
        threshold = (max_hp * MIN_HP_PERCENT_TO_START_BATTLE + 99) // 100
        self.mark_progress("ожидание восстановления HP перед боем")
        await self.storage.add_event(
            "LOW_HP_WAIT_STARTED",
            f"HP {current_hp}/{max_hp}; новые бои разрешены от {threshold}",
            level="INFO",
        )
        await self.notifier.send(
            "❤️ <b>Низкий запас HP</b>\n"
            f"Сейчас: {current_hp}/{max_hp}\n"
            f"Новые бои начнутся при HP не ниже {threshold}."
        )

    async def finish_battle_health_wait(self) -> None:
        self.state = BotState.MAP
        current_hp = self.context.current_hp or 0
        max_hp = self.context.max_hp or 0
        self.mark_progress("HP восстановлено для новых боёв")
        await self.storage.add_event(
            "LOW_HP_WAIT_FINISHED",
            f"HP восстановлено до {current_hp}/{max_hp}",
        )
        await self.notifier.send(
            f"❤️ <b>Здоровье восстановлено</b>\nHP: {current_hp}/{max_hp}\nФармер продолжает работу."
        )

    def confirm_pending_move(
        self,
        current_position: tuple[int, int],
        *,
        movement_blocked: bool = False,
    ) -> None:
        plan = self.context.pending_move
        if plan is None:
            if movement_blocked:
                self.navigator.reject_last_plan(
                    current_position,
                    mark_destination_blocked=True,
                )
            return

        if current_position == plan.destination:
            self.navigator.confirm_success(
                plan,
                current_position,
            )
            self.context.move_count += 1
            self.moves_in_cycle += 1
            self.context.failed_move_attempts = 0
            self.mark_progress("координата изменилась")

            if self.context.checked_empty_position == plan.origin:
                self.context.checked_empty_position = None

            self.log(
                f"Перемещение выполнено: "
                f"{plan.origin} → {current_position} "
                f"через {plan.button}. "
                f"Всего: {self.context.move_count}"
            )
        elif current_position == plan.origin:
            buttons_exhausted = self.navigator.reject_last_plan(
                current_position,
                mark_destination_blocked=movement_blocked,
            )
            if buttons_exhausted:
                self.context.failed_move_attempts = MAX_FAILED_MOVE_ATTEMPTS
            else:
                self.context.failed_move_attempts += 1
            self.log(
                f"Перемещение через {plan.button} не выполнено. "
                f"Неудач подряд: {self.context.failed_move_attempts}. "
                "Пробую другую кнопку без запроса истории."
            )
        else:
            recovered = self.navigator.recover_from_actual_transition(
                plan.origin,
                current_position,
            )
            if recovered:
                self.context.move_count += 1
                self.context.failed_move_attempts = 0
                self.mark_progress("навигатор пересинхронизирован")
            else:
                self.context.failed_move_attempts += 1

        self.context.pending_move = None

    async def request_pause(self) -> tuple[bool, str]:
        if not self.running:
            return False, "Фармер не запущен."
        if self.state is BotState.PAUSED:
            return False, "Фармер уже на паузе."
        self.pause_requested = True
        await self.storage.update_state(
            pause_requested=1,
            last_action="запрошена безопасная пауза",
        )
        if self.state is BotState.RESTING:
            if self.rest_task:
                self.rest_task.cancel()
                self.rest_task = None
            await self.enter_paused()
        return True, "Пауза запрошена. Бот остановится на карте после текущего действия или боя."

    async def enter_paused(self) -> None:
        self.pause_requested = False
        self.state = BotState.PAUSED
        self.mark_progress("фармер поставлен на паузу")
        await self.storage.update_state(
            process_status="PAUSED",
            game_state="PAUSED",
            pause_requested=0,
        )
        await self.storage.add_event("FARMER_PAUSED", "Фармер поставлен на паузу")
        await self.notifier.send("⏸ <b>Фармер поставлен на паузу</b>")

    async def resume(self) -> tuple[bool, str]:
        if not self.running:
            return False, "Фармер не запущен."

        if self.state is BotState.RESTING:
            if self.rest_task:
                self.rest_task.cancel()
                self.rest_task = None
            self.current_cycle += 1
            self.moves_in_cycle = 0
            action = f"передышка пропущена, начат цикл {self.current_cycle}"
        elif self.state is BotState.PAUSED:
            action = "продолжение после паузы"
        else:
            return False, "Продолжение доступно только на паузе или во время передышки."

        self.pause_requested = False
        self.state = BotState.STARTING
        await self.storage.update_state(
            process_status="RUNNING",
            game_state="STARTING",
            current_cycle=self.current_cycle,
            moves_in_cycle=self.moves_in_cycle,
            pause_requested=0,
            rest_until=None,
            last_action=action,
        )
        await self.storage.add_event("FARMER_RESUMED", action)
        await self.notifier.send("▶️ <b>Фарм продолжен</b>")
        await self.process_latest_state()
        return True, "Фарм продолжен с фактической текущей позиции."

    async def complete_cycle(self) -> None:
        total = self.settings.values.cycles_count
        if self.current_cycle >= total:
            await self.stop(f"завершены все циклы: {total}")
            return

        rest_seconds = random.uniform(
            self.settings.values.cycle_rest_min,
            self.settings.values.cycle_rest_max,
        )
        self.state = BotState.RESTING
        self.mark_progress(f"передышка после цикла {self.current_cycle}: {int(rest_seconds)} сек.")
        await self.storage.add_event(
            "CYCLE_COMPLETED",
            f"Завершён цикл {self.current_cycle} из {total}; передышка {int(rest_seconds)} сек.",
        )
        await self.notifier.send(
            f"😴 Завершён цикл {self.current_cycle} из {total}\n"
            f"Передышка: {int(rest_seconds // 60)} мин. {int(rest_seconds % 60)} сек."
        )
        self.rest_task = asyncio.create_task(self.rest_between_cycles(rest_seconds))

    async def rest_between_cycles(self, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        if not self.running or self.state is BotState.PAUSED:
            return
        self.current_cycle += 1
        self.moves_in_cycle = 0
        self.state = BotState.STARTING
        self.mark_progress(f"начат цикл {self.current_cycle}")
        await self.storage.add_event(
            "CYCLE_STARTED",
            f"Начат цикл {self.current_cycle} из {self.settings.values.cycles_count}",
        )
        await self.notifier.send(
            f"▶️ <b>Начат цикл {self.current_cycle} из {self.settings.values.cycles_count}</b>"
        )
        await self.process_latest_state()

    async def try_refresh_blessing_from_map(self, message) -> bool:
        if not self.settings.values.blessing_enabled:
            self.blessing.cancel()
            return False
        return await self.blessing.try_open_from_map(
            message,
            click_button=self.click_button,
            log=self.log,
            mark_progress=self.mark_progress,
        )

    async def handle_blessing_menu(self, message) -> bool:
        if not self.settings.values.blessing_enabled:
            if self.blessing.cancel():
                returned = await self.click_button(
                    message,
                    exact=BACK_TO_MAP_BUTTON,
                    action_type=ActionType.OPEN_ATTACK,
                    description=BACK_TO_MAP_BUTTON,
                )
                if not returned:
                    await self.request_map_refresh()
                return True
            return False
        return await self.blessing.handle_menu(
            message,
            find_button=find_button,
            click_button=self.click_button,
            mark_progress=self.mark_progress,
        )

    def confirm_blessing_from_text(self, text: str) -> None:
        if not self.settings.values.blessing_enabled:
            self.blessing.cancel()
            return
        self.blessing.confirm_from_text(
            text,
            log=self.log,
            mark_progress=self.mark_progress,
        )

    async def handle_map(self, message, text: str) -> None:
        map_info = parse_map(
            text,
            self.settings.values.enabled_targets,
            CHARACTER_NAME,
        )
        if map_info is None:
            return

        if map_info.location_name and map_info.location_name != self.navigator.location_name:
            self.navigator.use_location(map_info.location_name)
            self.context.pending_move = None
            self.context.failed_move_attempts = 0

        self.context.current_position = map_info.position
        if map_info.current_hp is not None:
            self.context.current_hp = map_info.current_hp
            self.context.max_hp = map_info.max_hp

        self.confirm_pending_move(
            map_info.position,
            movement_blocked=map_info.movement_blocked,
        )

        if self.state is BotState.RECOVERY:
            await self.handle_recovery_map(message, map_info)
            return

        if self.pause_requested or self.state is BotState.PAUSED:
            await self.enter_paused()
            return

        if self.state is BotState.RESTING:
            return

        self.state = BotState.MAP
        self.mark_progress("карта получена")

        if self.context.failed_move_attempts >= MAX_FAILED_MOVE_ATTEMPTS:
            await self.stop("игра не выполнила перемещение после проверки всех доступных кнопок")
            return

        if (
            self.context.checked_empty_position is not None
            and self.context.checked_empty_position != map_info.position
        ):
            self.context.checked_empty_position = None

        self.log(
            f"Карта: позиция {map_info.position}, "
            f"HP: {self.context.current_hp}/"
            f"{self.context.max_hp}, "
            f"монстров заявлено: {map_info.monster_count}, "
            f"показано: {list(map_info.monsters) or 'нет'}"
        )

        if self.battle_health_is_low():
            await self.wait_for_battle_health()
            return

        if map_info.movement_finished and random.random() < self.settings.values.long_pause_chance:
            pause = random.uniform(
                self.settings.values.long_pause_min,
                self.settings.values.long_pause_max,
            )
            await self.intentional_sleep(pause)

        if await self.try_refresh_blessing_from_map(message):
            return

        if (
            map_info.found_target is not None
            and self.context.checked_empty_position == map_info.position
        ):
            self.log(
                f"Цель «{map_info.found_target}» на клетке {map_info.position} "
                "уже исчезала или была занята; повторное нападение пропущено."
            )

        if (
            map_info.found_target is not None
            and self.context.checked_empty_position != map_info.position
        ):
            self.context.active_target = map_info.found_target
            self.context.checked_empty_position = None

            clicked = await self.click_button(
                message,
                exact=ATTACK_BUTTON,
                action_type=ActionType.OPEN_ATTACK,
                description=ATTACK_BUTTON,
            )
            if clicked:
                self.state = BotState.TARGET_SELECTION
                self.mark_progress("открыт список целей")
            return

        if (
            self.context.checked_empty_position != map_info.position
            and map_info.has_hidden_monsters
        ):
            self.context.active_target = None

            clicked = await self.click_button(
                message,
                exact=ATTACK_BUTTON,
                action_type=ActionType.OPEN_ATTACK,
                description=ATTACK_BUTTON,
            )
            if clicked:
                self.state = BotState.TARGET_SELECTION
                self.mark_progress("открыт полный список целей")
            return

        if self.moves_in_cycle >= self.settings.values.moves_per_cycle:
            await self.complete_cycle()
            return

        plan = self.navigator.plan(map_info.position)

        clicked = await self.click_button(
            message,
            exact=plan.button,
            action_type=ActionType.MOVE,
            description=plan.button,
        )
        if clicked:
            self.context.pending_move = plan
            self.state = BotState.MOVING
            self.mark_progress("команда перемещения отправлена")

    async def handle_target_selection(
        self,
        message,
    ) -> None:
        self.state = BotState.TARGET_SELECTION
        self.mark_progress("список целей получен")

        if self.battle_health_is_low():
            clicked = await self.click_button(
                message,
                exact=BACK_TO_MAP_BUTTON,
                action_type=ActionType.SELECT_TARGET,
                description=BACK_TO_MAP_BUTTON,
            )
            if clicked:
                await self.wait_for_battle_health()
            elif self.running:
                await self.recover_latest_state("низкий HP: не удалось вернуться на карту")
            return

        if self.pause_requested:
            clicked = await self.click_button(
                message,
                exact=BACK_TO_MAP_BUTTON,
                action_type=ActionType.SELECT_TARGET,
                description=BACK_TO_MAP_BUTTON,
            )
            if not clicked:
                await self.recover_latest_state("пауза: не удалось вернуться на карту")
            return

        analysis = analyze_map_targets(
            message,
            self.settings.values.enabled_targets,
        )
        found_target = analysis.selected_target
        target_counts = analysis.target_counts

        if found_target is not None:
            self.context.active_target = found_target
            self.context.battle_target = found_target
            self.context.checked_empty_position = None

            clicked = await self.click_button(
                message,
                contains=(found_target,),
                exclude=("pvp:", "занят"),
                action_type=ActionType.SELECT_TARGET,
                description=f"выбор цели {found_target}",
            )

            if clicked:
                self.state = BotState.COMBAT
                self.mark_progress("цель выбрана")
            return

        self.context.active_target = None

        # Если в списке были наши мобы, но все они заняты, клетка уже
        # полностью проверена. То же самое относится к проверке скрытых
        # монстров. После возврата на карту нужно перейти дальше, а не
        # снова открывать тот же список целей.
        all_matching_targets_are_occupied = bool(target_counts) and all(
            found > 0 and occupied >= found for found, occupied in target_counts.values()
        )

        # The full target list is already available in this inbound message.
        # If no free configured target was selected, reopening the same list
        # cannot reveal more data and can only create a request loop.
        if self.context.current_position is not None:
            self.context.checked_empty_position = self.context.current_position

            if all_matching_targets_are_occupied:
                occupied_summary = ", ".join(
                    f"{target}: {occupied}/{found}"
                    for target, (found, occupied) in target_counts.items()
                )
                self.log(
                    "Все подходящие цели на клетке заняты. "
                    f"Клетка {self.context.current_position} "
                    "помечена как проверенная. "
                    f"Занято: {occupied_summary}"
                )

        clicked = await self.click_button(
            message,
            exact=BACK_TO_MAP_BUTTON,
            action_type=ActionType.SELECT_TARGET,
            description=BACK_TO_MAP_BUTTON,
        )
        if clicked:
            self.mark_progress("возврат к карте")
        else:
            await self.recover_latest_state("не удалось вернуться к карте")

    async def handle_combat_target_selection(
        self,
        message,
    ) -> None:
        self.state = BotState.COMBAT
        self.mark_progress("получен список целей навыка")

        target_name, position = select_combat_target(
            message,
            self.settings.values.enabled_targets,
            self.context.active_target,
        )

        if position is None:
            await self.recover_latest_state("не найдена доступная цель навыка")
            return

        delay = self.action_delay(ActionType.SELECT_TARGET)
        self.log(f"Ожидание {delay:.1f} сек. перед выбором боевой цели: {target_name}")
        await self.intentional_sleep(delay)

        if not self.running:
            return

        if not self.is_latest_message(message):
            self.log("Отменён устаревший выбор боевой цели")
            return

        row, column = position
        self.log(f"Выбираю боевую цель: {target_name}")
        clicked = await self.press_button(
            message,
            row,
            column,
            f"боевая цель {target_name}",
        )
        if clicked:
            self.mark_progress("цель атакующего навыка выбрана")
        elif self.running:
            await self.recover_latest_state("не удалось выбрать цель навыка")

    async def handle_combat_turn(self, message) -> None:
        self.state = BotState.COMBAT
        self.mark_progress("ход игрока")

        current_mana = parse_current_mana(message.raw_text or "")
        self.log(
            f"Выбор навыка: мана={current_mana if current_mana is not None else 'не распознана'}"
        )

        skill_name = choose_skill(
            message,
            current_hp=self.context.current_hp,
            heal_threshold=self.settings.values.heal_threshold,
        )
        if skill_name is None:
            await self.recover_latest_state("не найден доступный навык")
            return

        clicked = await self.click_button(
            message,
            contains=(skill_name,),
            exclude=("CD:",),
            action_type=ActionType.USE_SKILL,
            description=skill_name,
        )
        if clicked:
            self.mark_progress(f"использован навык {skill_name}")

    def resolved_battle_target(self) -> str:
        candidates = [
            self.context.battle_target,
            self.context.active_target,
            *self.context.combat_enemies,
        ]
        for candidate in candidates:
            if candidate and normalize(candidate) not in {"неизвестная цель", "unknown target"}:
                return candidate
        return "неопределённый моб"

    def cleanup_old_log_files(self) -> int:
        cutoff = time.time() - max(1, LOG_RETENTION_DAYS) * 86400
        deleted = 0
        log_dir = Path(LOG_DIRECTORY)
        for path in log_dir.glob(f"{LOG_FILENAME}*"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    deleted += 1
            except OSError:
                logger.exception("Не удалось удалить старый лог %s", path)
        return deleted

    async def enter_death_recovery(
        self,
        message_id: int,
    ) -> None:
        target_name = self.resolved_battle_target()
        self.statistics.add_defeat(message_id)
        await self.storage.record_battle(
            telegram_message_id=message_id,
            session_id=self.session_id,
            target_name=target_name,
            result="DEFEAT",
            position=self.context.current_position,
        )

        self.context.clear_combat()
        self.context.pending_move = None
        self.context.checked_empty_position = None

        self.state = BotState.RECOVERY
        self.recovery_started_at = time.monotonic()
        self.recovery_refresh_requested = False
        await self.storage.add_event(
            "PLAYER_DEFEATED",
            f"Поражение от {target_name}; ожидание восстановления HP",
            level="WARNING",
        )
        await self.notifier.send(
            f"☠️ Персонаж погиб\nЦель: {target_name}\nНачато восстановление здоровья."
        )
        self.mark_progress("начато восстановление после смерти")

        if self.recovery_task:
            self.recovery_task.cancel()

        self.recovery_task = asyncio.create_task(self.death_recovery_loop())

    async def death_recovery_loop(self) -> None:
        await asyncio.sleep(DEATH_RECOVERY_MIN_WAIT)

        if not self.running or self.state is not BotState.RECOVERY:
            return

        # HP notifications are parsed locally. If the threshold was reached
        # during the mandatory pause, exactly one fresh map is requested now.
        await self.maybe_request_recovery_map()

        remaining = max(0, DEATH_RECOVERY_MAX_WAIT - DEATH_RECOVERY_MIN_WAIT)
        await asyncio.sleep(remaining)
        if self.running and self.state is BotState.RECOVERY:
            await self.stop("HP не восстановилось за предельное время")

    async def maybe_request_recovery_map(self) -> bool:
        if self.state is not BotState.RECOVERY or self.recovery_refresh_requested:
            return False

        elapsed = time.monotonic() - (self.recovery_started_at or time.monotonic())
        current_hp = self.context.current_hp or 0
        if elapsed < DEATH_RECOVERY_MIN_WAIT or current_hp < MIN_HP_AFTER_DEATH:
            return False

        self.recovery_refresh_requested = True
        self.log(f"HP восстановлено до {current_hp}; запрашиваю карту один раз.")
        requested = await self.request_map_refresh()
        if not requested and self.state is BotState.RECOVERY:
            self.recovery_refresh_requested = False
        return requested

    async def handle_recovery_map(
        self,
        message,
        map_info,
    ) -> None:
        elapsed = time.monotonic() - (self.recovery_started_at or time.monotonic())

        if elapsed < DEATH_RECOVERY_MIN_WAIT:
            return

        current_hp = map_info.current_hp or 0

        self.log(
            f"Проверка восстановления: "
            f"HP {current_hp}/{map_info.max_hp}, "
            f"прошло {int(elapsed)} сек."
        )

        if current_hp >= MIN_HP_AFTER_DEATH:
            self.state = BotState.MAP
            self.recovery_started_at = None
            self.recovery_refresh_requested = False
            self.mark_progress("здоровье восстановлено")
            await self.storage.add_event(
                "RECOVERY_FINISHED",
                f"HP восстановлено до {current_hp}/{map_info.max_hp}",
            )
            await self.notifier.send(
                f"✅ Здоровье восстановлено\nHP: {current_hp}/{map_info.max_hp}\nФарм продолжен."
            )

            if self.recovery_task:
                self.recovery_task.cancel()
                self.recovery_task = None

            await self.handle_map(message, message.raw_text or "")
        else:
            # The map can be newer than the preceding HP notification. Wait
            # for another inbound health update instead of polling Telegram.
            self.recovery_refresh_requested = False

    async def handle_target_gone(self) -> None:
        """Штатно восстанавливает карту, если выбранный моб уже исчез."""
        disappeared_target = self.context.active_target or "неизвестная цель"

        self.context.active_target = None
        self.context.checked_empty_position = self.context.current_position
        self.context.pending_move = None
        self.context.failed_move_attempts = 0

        self.state = BotState.MAP
        self.mark_progress("цель исчезла до начала боя")

        await self.storage.add_event(
            "TARGET_GONE",
            f"Монстр «{disappeared_target}» исчез с текущей клетки",
        )
        self.log(
            f"Монстр «{disappeared_target}» исчез с клетки. Обновляю карту и продолжаю маршрут."
        )

        await self.request_map_refresh()

    async def request_map_refresh(self) -> bool:
        if not self.event_queue.empty():
            self.log("Запрос карты не нужен: входящее состояние уже ждёт локального разбора.")
            return False

        # One semantic inbound state may cause several local recovery paths.
        # Only the first of them is allowed to reach Telegram.
        generation = self.inbound_generation
        if not self.state_refresh_gate.reserve(generation):
            self.log("Повторный запрос карты для того же состояния подавлен.")
            return False

        message = self.latest_received_message
        if (
            message is not None
            and self.is_latest_message(message)
            and find_button(message, exact=LOOK_BUTTON) is not None
        ):
            return await self.click_button(
                message,
                exact=LOOK_BUTTON,
                action_type=ActionType.OPEN_ATTACK,
                description=LOOK_BUTTON,
            )

        try:
            limiter_delay = await self.telegram_action_limiter.acquire()
            if limiter_delay >= 0.05:
                self.log(
                    f"Защитный лимит Telegram добавил "
                    f"{limiter_delay:.1f} сек. перед запросом карты."
                )
            if not self.running or self.inbound_generation != generation:
                self.log("Запрос карты отменён: уже получено новое состояние.")
                return False
            await self.client.send_message(
                self.game_bot,
                MAP_COMMAND,
            )
            return True
        except FloodWaitError as error:
            await self.stop_for_flood_wait(error, MAP_COMMAND)
            return False

    async def recover_latest_state(
        self,
        reason: str,
    ) -> None:
        if not self.event_queue.empty():
            self.log(
                f"Восстановление «{reason}» не требуется: новое состояние уже в локальной очереди."
            )
            return

        if not self.recovery_attempt_guard.allow():
            await self.stop(
                f"более {TELEGRAM_RECOVERY_LIMIT} аварийных восстановлений "
                f"за {int(TELEGRAM_RECOVERY_WINDOW // 60)} минут; остановлено для защиты от флуда"
            )
            return

        attempt = self.watchdog.begin_recovery_attempt()

        self.log(f"Восстановление состояния ({attempt}/{MAX_RECOVERY_ATTEMPTS}): {reason}")

        if attempt > MAX_RECOVERY_ATTEMPTS:
            await self.stop(f"исчерпаны попытки восстановления: {reason}")
            return

        # Telethon already delivered the newest state to the local cache.
        # Reading history here adds an RPC and risks replaying an old inline UI.
        await self.request_map_refresh()

    async def watchdog_loop(self) -> None:
        while self.running:
            await asyncio.sleep(WATCHDOG_CHECK_INTERVAL)

            if await self.storage.get_setting("farmer_stop_requested", False):
                await self.storage.set_setting("farmer_stop_requested", False)
                await self.stop("остановлен командой из другого процесса")
                return

            if self.state in {
                BotState.PAUSED,
                BotState.RESTING,
                BotState.WAITING_FOR_HEALTH,
                BotState.RECOVERY,
            }:
                continue

            # A deliberate safety delay is not a stalled game state. Starting
            # recovery here would add a second request behind the limiter.
            if self.intentional_waits or self.telegram_action_limiter.pending:
                continue

            should_recover = self.watchdog.should_recover(
                self.state,
                move_timeout=MOVE_PROGRESS_TIMEOUT,
                target_timeout=TARGET_SELECTION_TIMEOUT,
                combat_timeout=COMBAT_PROGRESS_TIMEOUT,
                general_timeout=GENERAL_PROGRESS_TIMEOUT,
                recovery_timeout=RECOVERY_WATCHDOG_TIMEOUT,
            )

            if not should_recover:
                continue

            # Обычное срабатывание watchdog — внутренний механизм
            # самовосстановления. Сохраняем его для диагностики в SQLite
            # и журнале, но не отправляем тревожное сообщение в Telegram.
            await self.storage.add_event(
                "WATCHDOG_TRIGGERED",
                f"Нет прогресса в состоянии {self.state.name}",
                level="INFO",
            )
            self.log(
                "Watchdog обнаружил отсутствие прогресса. "
                "Пробую восстановить состояние без уведомления."
            )

            await self.recover_latest_state(
                f"watchdog: нет прогресса в состоянии {self.state.name}"
            )

    async def handle_message(self, message) -> None:
        if not self.running:
            return

        text = message.raw_text or ""
        hp_changed = self.update_hp(text)
        self.confirm_blessing_from_text(text)

        kind = classify_message(
            text,
            self.settings.values.enabled_targets,
            CHARACTER_NAME,
        )

        if self.state is BotState.RECOVERY and hp_changed and kind is not MessageKind.MAP:
            await self.maybe_request_recovery_map()
        if self.state is BotState.RECOVERY and kind is MessageKind.OTHER:
            return

        if self.state is BotState.WAITING_FOR_HEALTH:
            active_battle_kinds = {
                MessageKind.TARGET_SELECTION,
                MessageKind.COMBAT_TARGET_SELECTION,
                MessageKind.COMBAT_STARTED,
                MessageKind.PLAYER_TURN,
                MessageKind.BATTLE_FINISHED,
            }
            if kind not in active_battle_kinds:
                if not self.has_battle_health():
                    return
                await self.finish_battle_health_wait()
                if kind is not MessageKind.MAP:
                    await self.request_map_refresh()
                    return

        if await self.handle_blessing_menu(message):
            return

        round_events = parse_combat_round_events(text)
        for defeated_enemy in round_events.defeated_enemies:
            self.context.remove_combat_enemy(defeated_enemy)
            self.log(f"Противник повержен: {defeated_enemy}")

        if kind is MessageKind.MAP:
            await self.handle_map(message, text)
            return

        if kind is MessageKind.MOVE_STARTED:
            self.state = BotState.MOVING
            self.mark_progress("сервер подтвердил движение")
            return

        if kind is MessageKind.TARGET_SELECTION:
            await self.handle_target_selection(message)
            return

        if kind is MessageKind.COMBAT_TARGET_SELECTION:
            await self.handle_combat_target_selection(message)
            return

        if kind is MessageKind.COMBAT_STARTED:
            combat_target = extract_combat_target(text)
            normalized_text = normalize(text)

            if "на вас напали:" in normalized_text:
                self.context.pending_move = None
                self.context.failed_move_attempts = 0
                self.context.checked_empty_position = None

                if combat_target:
                    self.context.active_target = combat_target
                    self.context.add_combat_enemy(combat_target)
                self.log(
                    "Обнаружено внезапное нападение"
                    + (f": {combat_target}" if combat_target else "")
                )
                self.mark_progress("внезапное нападение")
            elif "на помощь врагу присоединился" in normalized_text:
                if combat_target:
                    self.context.add_combat_enemy(combat_target)
                self.log(
                    "К бою присоединился дополнительный моб"
                    + (f": {combat_target}" if combat_target else "")
                )
                self.mark_progress("к врагу присоединилось подкрепление")
            else:
                if combat_target:
                    self.context.active_target = combat_target
                    self.context.add_combat_enemy(combat_target)
                self.mark_progress("бой начался")

            self.state = BotState.COMBAT
            return

        if kind is MessageKind.PLAYER_TURN:
            await self.handle_combat_turn(message)
            return

        if kind is MessageKind.BATTLE_INVITE:
            self.log("Приглашение в бой проигнорировано.")
            return

        if kind is MessageKind.TARGET_GONE:
            await self.handle_target_gone()
            return

        if kind is MessageKind.BATTLE_FINISHED:
            if "Победа" in text:
                target_name = self.resolved_battle_target()
                reward = parse_battle_reward(text)

                added = self.statistics.add_victory(
                    message.id,
                    reward,
                )
                db_added, cards = await self.storage.record_battle(
                    telegram_message_id=message.id,
                    session_id=self.session_id,
                    target_name=target_name,
                    result="VICTORY",
                    xp=reward.xp,
                    dust=reward.dust,
                    items=reward.items,
                    position=self.context.current_position,
                )
                for card in cards:
                    await self.storage.add_event(
                        "MOB_CARD_DROPPED",
                        card,
                        payload={"position": self.context.current_position},
                    )
                    await self.notifier.card_drop(
                        card,
                        self.context.current_position,
                )
                if added:
                    logger.info(
                        "\n%s",
                        format_report(
                            "СТАТИСТИКА ТЕКУЩЕЙ СЕССИИ",
                            self.statistics.session_report(),
                        ),
                    )

                self.context.clear_combat()
                self.mark_progress("бой завершён победой")
                return

            if "Поражение" in text:
                await self.enter_death_recovery(message.id)
                return

    async def process_latest_state(self) -> None:
        # Historical inline messages may belong to an old location or an
        # already completed turn. One fresh map command is both cheaper than a
        # history read plus an action and safer than replaying stale buttons.
        self.mark_progress("при запуске запрошено свежее состояние")
        await self.request_map_refresh()

    async def stop(self, reason: str) -> None:
        if not self.running:
            return

        self.stop_reason = reason
        self.running = False
        self.state = BotState.STOPPED
        self.pending_progress_reason = None

        if self.progress_persist_task and self.progress_persist_task is not asyncio.current_task():
            self.progress_persist_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.progress_persist_task

        logger.info(
            "\n%s",
            format_report(
                "ИТОГ ТЕКУЩЕЙ СЕССИИ",
                self.statistics.session_report(),
            ),
        )
        logger.info("Причина остановки: %s", reason)
        await self.storage.finish_session(
            self.session_id,
            reason,
            self.statistics.elapsed_seconds(),
        )
        await self.storage.add_event(
            "FARMER_STOPPED",
            reason,
        )
        await self.storage.checkpoint()
        for task in (
            self.worker_task,
            self.watchdog_task,
            self.recovery_task,
            self.rest_task,
            self.progress_persist_task,
        ):
            if task and task is not asyncio.current_task():
                task.cancel()

        if self.client.is_connected():
            await self.client.disconnect()

    async def run(self) -> None:
        self.validate_config()
        deleted_logs = self.cleanup_old_log_files()
        cleanup = await self.storage.cleanup_old_data(DATA_RETENTION_DAYS)
        logger.info(
            "Очистка хранения: срок %s дн.; events=%s, battles=%s, "
            "drops=%s, sessions=%s, logs=%s",
            DATA_RETENTION_DAYS,
            cleanup["events"],
            cleanup["battles"],
            cleanup["drops"],
            cleanup["sessions"],
            deleted_logs,
        )
        self.session_id = await self.storage.start_session(
            cycles_count=self.settings.values.cycles_count,
            moves_per_cycle=self.settings.values.moves_per_cycle,
        )
        await self.storage.add_event(
            "FARMER_STARTED",
            f"Фармер запущен: {self.settings.values.cycles_count} цикл(а), "
            f"по {self.settings.values.moves_per_cycle} ходов",
        )
        await self.notifier.send(
            "▶️ Фармер запущен\n"
            f"Циклов: {self.settings.values.cycles_count}\n"
            f"Ходов в цикле: {self.settings.values.moves_per_cycle}"
        )

        await self.client.start()
        # Uses the Telethon entity cache and avoids fetching the full entity on
        # every restart once the peer is known to the session.
        self.game_bot = await self.client.get_input_entity(GAME_BOT)

        logger.info("=" * 72)
        logger.info("FoG Farmer запущен")
        logger.info("Telegram-сессия подключена")
        logger.info(
            "Telegram-предохранитель: не более %s действий за %s сек., интервал не менее %.1f сек.",
            TELEGRAM_ACTION_LIMIT,
            int(TELEGRAM_ACTION_WINDOW),
            TELEGRAM_ACTION_MIN_INTERVAL,
        )
        logger.info("Персонаж: %s", CHARACTER_NAME)
        logger.info("Цели: %s", self.settings.values.enabled_targets)
        logger.info(
            "Watchdog: движение %s сек., бой %s сек.",
            MOVE_PROGRESS_TIMEOUT,
            COMBAT_PROGRESS_TIMEOUT,
        )
        logger.info(
            "После смерти: ожидание минимум %s сек., возврат при HP >= %s",
            DEATH_RECOVERY_MIN_WAIT,
            MIN_HP_AFTER_DEATH,
        )
        logger.info(
            "Полный журнал: %s/%s",
            LOG_DIRECTORY,
            LOG_FILENAME,
        )
        logger.info("=" * 72)

        @self.client.on(events.NewMessage(chats=self.game_bot))
        async def on_new_message(event) -> None:
            if event.message.out:
                return
            await self.enqueue_message(event.message)

        @self.client.on(events.MessageEdited(chats=self.game_bot))
        async def on_edited_message(event) -> None:
            if event.message.out:
                return
            await self.enqueue_message(event.message)

        self.worker_task = asyncio.create_task(self.event_worker())
        self.watchdog_task = asyncio.create_task(self.watchdog_loop())

        await self.process_latest_state()
        await self.client.run_until_disconnected()
