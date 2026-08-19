from __future__ import annotations

import asyncio
import random
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import FarmStatistics, format_report
from typing import Any

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RPCError

from blessing import BlessingManager
from combat_learning import build_shadow_plan
from combat_round import parse_combat_round
from combat_strategy import (
    CombatDecisionTrace,
    CombatMemory,
    RecentCombatKnowledge,
    SkillTarget,
    build_decision_trace,
    choose_combat_action,
    resolve_decision_trace,
)
from config import (
    ACTIVITY_BREAK_DURATION_MAX,
    ACTIVITY_BREAK_DURATION_MIN,
    ACTIVITY_BREAK_MOVES_MAX,
    ACTIVITY_BREAK_MOVES_MIN,
    ACTIVITY_BREAK_WORK_MAX,
    ACTIVITY_BREAK_WORK_MIN,
    ACTIVITY_PROFILE_FAST,
    API_HASH,
    API_ID,
    CHARACTER_NAME,
    COMBAT_PROGRESS_TIMEOUT,
    DATA_RETENTION_DAYS,
    DEATH_RECOVERY_MAX_WAIT,
    DEATH_RECOVERY_MIN_WAIT,
    FAST_ATTACK_DELAY,
    FAST_MOVE_DELAY,
    FAST_SKILL_DELAY,
    FAST_TARGET_DELAY,
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
from human_delays import ActivityBreakPlanner, HumanDelayModel, parse_remaining_seconds
from logger_setup import setup_logging
from models import (
    ActionType,
    BotState,
    MapInfo,
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
from skills import enough_health_for_battle
from storage import Storage, utc_now
from targeting import analyze_map_targets, select_combat_target
from telegram_buttons import find_button, get_button_texts
from telegram_safety import (
    RollingAttemptGuard,
    StateRefreshGate,
    TelegramActionLimiter,
    TelegramActionTelemetry,
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
        self.combat = CombatMemory()
        for target in settings.values.treatment_enemy_targets:
            self.combat.confirm_treatment_enemy(target)
        self.combat_decisions: list[CombatDecisionTrace] = []
        self.pending_combat_decision: CombatDecisionTrace | None = None
        self.combat_knowledge_profiles: dict[int, RecentCombatKnowledge] = {}
        self.active_combat_profile_max_hp: int | None = None
        self.delay_model = HumanDelayModel()
        self.activity_break_planner = ActivityBreakPlanner()
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
        self.activity_break_task: asyncio.Task | None = None
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
        self.telegram_action_telemetry = TelegramActionTelemetry()

        self.blessing = BlessingManager()

    def log(self, text: str) -> None:
        logger.info("[%s] %s", self.state.name, text)

    def record_telegram_action(self, kind: str) -> None:
        snapshot = self.telegram_action_telemetry.record(kind)
        total_value = snapshot["total"]
        total = total_value if isinstance(total_value, int) else 0
        if total % 10 == 0:
            self.log(
                "[TELEGRAM_IO] исходящих действий: "
                f"всего={total}, за 1 мин={snapshot['last_minute']}, "
                f"за 10 мин={snapshot['last_ten_minutes']}, "
                f"типы={snapshot['by_kind']}"
            )

    def _new_combat_knowledge(self) -> RecentCombatKnowledge:
        knowledge = RecentCombatKnowledge()
        for target in self.settings.values.treatment_enemy_targets:
            knowledge.confirm_treatment_enemy(target)
        return knowledge

    async def load_combat_knowledge(self) -> None:
        stored_profiles = await self.storage.load_combat_knowledge()
        for max_hp, payload in stored_profiles.items():
            knowledge = RecentCombatKnowledge.from_payload(payload)
            for target in self.settings.values.treatment_enemy_targets:
                knowledge.confirm_treatment_enemy(target)
            self.combat_knowledge_profiles[max_hp] = knowledge
        if stored_profiles:
            self.log(
                "Загружена долговременная боевая память: "
                f"профилей персонажа — {len(stored_profiles)}."
            )

    def activate_combat_profile(self, max_hp: int | None) -> None:
        if max_hp is None or max_hp <= 0:
            return
        if self.active_combat_profile_max_hp == max_hp:
            return

        knowledge = self.combat_knowledge_profiles.setdefault(
            max_hp,
            self._new_combat_knowledge(),
        )
        self.combat.knowledge = knowledge
        self.active_combat_profile_max_hp = max_hp
        if self.combat.target_name:
            knowledge.load_into(self.combat)
        self.log(f"Активирован боевой профиль для максимума HP {max_hp}.")

    async def persist_combat_knowledge(self) -> None:
        max_hp = getattr(self, "active_combat_profile_max_hp", None)
        if max_hp is None:
            return
        await self.storage.save_combat_knowledge(
            max_hp,
            self.combat.knowledge.as_payload(),
        )

    def mark_progress(self, reason: str) -> None:
        self.watchdog.mark_progress(reason)
        self.pending_progress_reason = reason
        if self.running and (
            self.progress_persist_task is None or self.progress_persist_task.done()
        ):
            self.progress_persist_task = asyncio.create_task(self._persist_progress())

    def _state_snapshot(
        self,
        reason: str,
        *,
        pause_requested: bool | None = None,
    ) -> dict[str, Any]:
        position = self.context.current_position
        return {
            "game_state": self.state.name,
            "position_x": position[0] if position else None,
            "position_y": position[1] if position else None,
            "current_hp": self.context.current_hp,
            "max_hp": self.context.max_hp,
            "active_target": self.context.active_target,
            "moves": self.context.move_count,
            "last_action": reason,
            "last_progress_at": utc_now(),
            "session_id": self.session_id,
            "current_cycle": self.current_cycle,
            "cycles_count": self.settings.values.cycles_count,
            "moves_in_cycle": self.moves_in_cycle,
            "moves_per_cycle": self.settings.values.moves_per_cycle,
            "pause_requested": int(
                self.pause_requested if pause_requested is None else pause_requested
            ),
        }

    async def _persist_progress(self) -> None:
        # Coalesce bursts into one writer. The most recent state is what the
        # control bot needs; spawning one SQLite task per update can otherwise
        # amplify a bad incoming-message loop into thousands of pending writes.
        while self.running and self.pending_progress_reason is not None:
            reason = self.pending_progress_reason
            self.pending_progress_reason = None
            await self.storage.update_state(**self._state_snapshot(reason))

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

    def action_delay(
        self,
        action_type: ActionType,
        *,
        urgent: bool = False,
        remaining_seconds: int | None = None,
    ) -> float:
        s = self.settings.values
        if s.activity_profile == ACTIVITY_PROFILE_FAST:
            ranges = {
                ActionType.MOVE: FAST_MOVE_DELAY,
                ActionType.OPEN_ATTACK: FAST_ATTACK_DELAY,
                ActionType.SELECT_TARGET: FAST_TARGET_DELAY,
                ActionType.USE_SKILL: FAST_SKILL_DELAY,
            }
        else:
            ranges = {
                ActionType.MOVE: (s.move_delay_min, s.move_delay_max),
                ActionType.OPEN_ATTACK: (s.attack_delay_min, s.attack_delay_max),
                ActionType.SELECT_TARGET: (s.target_delay_min, s.target_delay_max),
                ActionType.USE_SKILL: (s.skill_delay_min, s.skill_delay_max),
            }
        minimum, maximum = ranges[action_type]
        return self.delay_model.action_delay(
            minimum,
            maximum,
            urgent=urgent,
            remaining_seconds=remaining_seconds,
        )

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
            self.record_telegram_action("inline_callback")
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
        urgent: bool = False,
        remaining_seconds: int | None = None,
    ) -> bool:
        delay = self.action_delay(
            action_type,
            urgent=urgent,
            remaining_seconds=remaining_seconds,
        )

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
        return enough_health_for_battle(
            self.context.current_hp,
            self.context.max_hp,
            self.settings.values.battle_start_hp_percent,
        )

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
        required_percent = self.settings.values.battle_start_hp_percent
        threshold = (max_hp * required_percent + 99) // 100
        self.mark_progress("ожидание восстановления HP перед боем")
        await self.storage.add_event(
            "LOW_HP_WAIT_STARTED",
            f"HP {current_hp}/{max_hp}; новые бои разрешены от {threshold}",
            level="INFO",
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

    def confirm_pending_move(
        self,
        current_position: tuple[int, int],
        *,
        movement_blocked: bool = False,
    ) -> tuple[int, int] | None:
        previous_obstacles = set(self.navigator.runtime_blocked)
        plan = self.context.pending_move
        if plan is None:
            if movement_blocked:
                self.navigator.reject_last_plan(
                    current_position,
                    mark_destination_blocked=True,
                )
            learned = self.navigator.runtime_blocked - previous_obstacles
            return next(iter(learned), None)

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
                self.moves_in_cycle += 1
                self.context.failed_move_attempts = 0
                self.mark_progress("навигатор пересинхронизирован")
                self.log(
                    "Перемещение подтверждено по фактической позиции: "
                    f"{plan.origin} → {current_position} "
                    f"(ожидалось {plan.destination}). "
                    f"Всего: {self.context.move_count}"
                )
            else:
                self.context.failed_move_attempts += 1

        self.context.pending_move = None
        learned = self.navigator.runtime_blocked - previous_obstacles
        return next(iter(learned), None)

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
        if self.state in {BotState.RESTING, BotState.ACTIVITY_BREAK}:
            if self.rest_task:
                self.rest_task.cancel()
                self.rest_task = None
            if self.activity_break_task:
                self.activity_break_task.cancel()
                self.activity_break_task = None
            self.activity_break_planner.reset()
            await self.enter_paused()
        return True, "Пауза запрошена. Бот остановится на карте после текущего действия или боя."

    async def enter_paused(self) -> None:
        self.pause_requested = False
        self.activity_break_planner.reset()
        self.state = BotState.PAUSED
        self.mark_progress("фармер поставлен на паузу")
        await self.storage.update_state(
            process_status="PAUSED",
            game_state="PAUSED",
            pause_requested=0,
            rest_until=None,
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
            self.activity_break_planner.reset()
            action = f"передышка пропущена, начат цикл {self.current_cycle}"
        elif self.state is BotState.ACTIVITY_BREAK:
            if self.activity_break_task:
                self.activity_break_task.cancel()
                self.activity_break_task = None
            self.activity_break_planner.complete(
                self.moves_in_cycle,
                moves_min=ACTIVITY_BREAK_MOVES_MIN,
                moves_max=ACTIVITY_BREAK_MOVES_MAX,
                work_min=ACTIVITY_BREAK_WORK_MIN,
                work_max=ACTIVITY_BREAK_WORK_MAX,
            )
            action = "длительный перерыв пропущен"
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

    async def start_activity_break(self) -> None:
        seconds = self.activity_break_planner.duration(
            ACTIVITY_BREAK_DURATION_MIN,
            ACTIVITY_BREAK_DURATION_MAX,
        )
        rest_until = datetime.now(UTC) + timedelta(seconds=seconds)
        self.state = BotState.ACTIVITY_BREAK
        self.mark_progress(f"длительный перерыв: {int(seconds)} сек.")
        await self.storage.update_state(rest_until=rest_until.isoformat())
        await self.storage.add_event(
            "ACTIVITY_BREAK_STARTED",
            f"Перерыв на {int(seconds)} сек. после {self.moves_in_cycle} перемещений",
        )
        self.log(
            f"Начат длительный перерыв на {seconds / 60:.1f} мин. "
            f"после {self.moves_in_cycle} перемещений."
        )
        self.activity_break_task = asyncio.create_task(
            self.finish_activity_break(seconds),
            name="activity-break",
        )

    async def finish_activity_break(self, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        if not self.running or self.state is not BotState.ACTIVITY_BREAK:
            return
        self.activity_break_planner.complete(
            self.moves_in_cycle,
            moves_min=ACTIVITY_BREAK_MOVES_MIN,
            moves_max=ACTIVITY_BREAK_MOVES_MAX,
            work_min=ACTIVITY_BREAK_WORK_MIN,
            work_max=ACTIVITY_BREAK_WORK_MAX,
        )
        self.activity_break_task = None
        self.state = BotState.STARTING
        self.mark_progress("длительный перерыв завершён")
        await self.storage.update_state(rest_until=None)
        await self.storage.add_event(
            "ACTIVITY_BREAK_FINISHED",
            "Длительный перерыв завершён; запрошено одно свежее состояние карты",
        )
        self.log("Длительный перерыв завершён. Обновляю карту один раз.")
        await self.process_latest_state()

    async def rest_between_cycles(self, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        if not self.running or self.state is BotState.PAUSED:
            return
        self.current_cycle += 1
        self.moves_in_cycle = 0
        self.activity_break_planner.reset()
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

    async def handle_map(self, message, map_info: MapInfo) -> None:
        geometry_changed = bool(
            map_info.width
            and map_info.height
            and (
                self.navigator.max_x != map_info.width - 1
                or self.navigator.max_y != map_info.height - 1
            )
        )
        if map_info.location_name and (
            map_info.location_name != self.navigator.location_name or geometry_changed
        ):
            learned_obstacles = await self.storage.get_map_obstacles(map_info.location_name)
            self.navigator.use_location(
                map_info.location_name,
                learned_obstacles,
                current_position=map_info.position,
                width=map_info.width,
                height=map_info.height,
            )
            self.context.pending_move = None
            self.context.failed_move_attempts = 0

        self.context.current_position = map_info.position
        if map_info.current_hp is not None:
            self.context.current_hp = map_info.current_hp
            self.context.max_hp = map_info.max_hp

        learned_obstacle = self.confirm_pending_move(
            map_info.position,
            movement_blocked=map_info.movement_blocked,
        )
        route_rebuilt = self.navigator.ensure_position(map_info.position)
        discarded_obstacles = self.navigator.take_recovery_discarded_obstacles()
        if discarded_obstacles and self.navigator.location_name:
            deleted = await self.storage.forget_map_obstacles(
                self.navigator.location_name,
                discarded_obstacles,
            )
            self.log(
                "Маршрут не соответствовал фактической позиции. "
                f"Удалено сомнительных препятствий: {deleted}; "
                f"маршрут перестроен от {map_info.position}."
            )
            self.context.failed_move_attempts = 0
        elif route_rebuilt:
            self.log(
                f"Маршрут пересинхронизирован по фактической позиции {map_info.position}."
            )
            self.context.failed_move_attempts = 0
        if learned_obstacle is not None and self.navigator.location_name:
            inserted = await self.storage.remember_map_obstacle(
                self.navigator.location_name,
                learned_obstacle,
            )
            if inserted:
                self.log(
                    f"Изучено препятствие: {self.navigator.location_name} "
                    f"{learned_obstacle}. Маршрут перестроен локально."
                )

        if self.state is BotState.RECOVERY:
            await self.handle_recovery_map(message, map_info)
            return

        if self.pause_requested or self.state is BotState.PAUSED:
            await self.enter_paused()
            return

        if self.state in {BotState.RESTING, BotState.ACTIVITY_BREAK}:
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

        if self.settings.values.activity_profile == ACTIVITY_PROFILE_FAST:
            self.activity_break_planner.reset()
        elif self.activity_break_planner.is_due(
            self.moves_in_cycle,
            moves_min=ACTIVITY_BREAK_MOVES_MIN,
            moves_max=ACTIVITY_BREAK_MOVES_MAX,
            work_min=ACTIVITY_BREAK_WORK_MIN,
            work_max=ACTIVITY_BREAK_WORK_MAX,
        ):
            await self.start_activity_break()
            return

        if (
            self.settings.values.activity_profile != ACTIVITY_PROFILE_FAST
            and map_info.movement_finished
            and self.delay_model.should_take_long_pause(
                self.settings.values.long_pause_chance
            )
        ):
            pause = self.delay_model.action_delay(
                self.settings.values.long_pause_min,
                self.settings.values.long_pause_max,
            )
            self.log(f"Короткая пауза на пустой карте: {pause:.1f} сек.")
            await self.intentional_sleep(pause)

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

        if normalize(self.combat.pending_skill or "") == "лечение":
            enemy_name, enemy_position = select_combat_target(
                message,
                self.settings.values.enabled_targets,
                self.context.active_target,
                preferred_target="enemy",
                character_name=CHARACTER_NAME,
            )
            if enemy_position is not None:
                confirmed_target = (
                    self.combat.target_name
                    or self.context.active_target
                    or enemy_name
                )
                self.combat.confirm_treatment_enemy(confirmed_target)
                if confirmed_target and await self.settings.add_treatment_enemy_target(
                    confirmed_target
                ):
                    self.log(
                        f"Подтверждено атакующее Лечение для цели «{confirmed_target}»."
                    )
                    await self.storage.add_event(
                        "TREATMENT_ENEMY_CONFIRMED",
                        f"Лечение может наносить урон цели «{confirmed_target}»",
                    )

        target_name, position = select_combat_target(
            message,
            self.settings.values.enabled_targets,
            self.context.active_target,
            preferred_target=(
                "self" if self.combat.pending_target is SkillTarget.SELF else "enemy"
            ),
            character_name=CHARACTER_NAME,
        )

        if position is None:
            await self.recover_latest_state("не найдена доступная цель навыка")
            return

        delay = self.action_delay(
            ActionType.SELECT_TARGET,
            urgent=self.combat.pending_urgent,
            remaining_seconds=parse_remaining_seconds(message.raw_text or ""),
        )
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
            self.mark_progress("цель навыка выбрана")
        elif self.running:
            await self.recover_latest_state("не удалось выбрать цель навыка")

    async def handle_combat_turn(self, message) -> None:
        self.state = BotState.COMBAT
        self.mark_progress("ход игрока")

        if self.pending_combat_decision is not None:
            self.log(
                "Предыдущее боевое решение не подтверждено сообщением игры; "
                "оно не попадёт в статистику."
            )
            self.pending_combat_decision = None

        round_state = self.combat.latest_round
        current_mana = round_state.current_mana if round_state is not None else None
        self.log(
            f"Выбор навыка: мана={current_mana if current_mana is not None else 'не распознана'}"
        )

        decision = choose_combat_action(
            message,
            memory=self.combat,
            current_hp=self.context.current_hp,
            max_hp=self.context.max_hp,
            heal_threshold=self.settings.values.heal_threshold,
            round_state=round_state,
        )
        if decision is None:
            await self.recover_latest_state("не найден доступный навык")
            return

        skill_name = decision.skill_name
        self.combat.pending_skill = skill_name
        self.combat.pending_target = decision.target
        self.combat.pending_urgent = decision.urgent
        shadow_plan = build_shadow_plan(
            message,
            memory=self.combat,
            current_hp=self.context.current_hp,
            max_hp=self.context.max_hp,
            executed=decision,
            round_state=round_state,
        )
        if shadow_plan is not None:
            self.log(shadow_plan.format_log())
        decision_trace = build_decision_trace(
            created_at=utc_now(),
            telegram_message_id=int(message.id),
            memory=self.combat,
            round_state=round_state,
            current_hp=self.context.current_hp,
            max_hp=self.context.max_hp,
            decision=decision,
            shadow_plan=(shadow_plan.as_payload() if shadow_plan is not None else None),
        )
        self.log(decision_trace.format_log())

        clicked = await self.click_button(
            message,
            contains=(skill_name,),
            exclude=("CD:",),
            action_type=ActionType.USE_SKILL,
            description=skill_name,
            urgent=decision.urgent,
            remaining_seconds=(
                round_state.remaining_seconds
                if round_state is not None
                else parse_remaining_seconds(message.raw_text or "")
            ),
        )
        if clicked:
            self.pending_combat_decision = decision_trace
            self.mark_progress(f"использован навык {skill_name}")
        else:
            self.combat.pending_skill = None
            self.combat.pending_target = None
            self.combat.pending_urgent = False
            self.pending_combat_decision = None

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
            combat_decisions=tuple(
                trace.as_payload() for trace in self.combat_decisions
            ),
        )
        await self.persist_combat_knowledge()

        self.context.clear_combat()
        self.combat.reset()
        self.combat_decisions.clear()
        self.pending_combat_decision = None
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

            await self.handle_map(message, map_info)
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
            self.record_telegram_action("map_message")
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
    ) -> bool:
        if not self.event_queue.empty():
            self.log(
                f"Восстановление «{reason}» не требуется: новое состояние уже в локальной очереди."
            )
            return False

        if not self.recovery_attempt_guard.allow():
            await self.stop(
                f"более {TELEGRAM_RECOVERY_LIMIT} аварийных восстановлений "
                f"за {int(TELEGRAM_RECOVERY_WINDOW // 60)} минут; остановлено для защиты от флуда"
            )
            return False

        attempt = self.watchdog.begin_recovery_attempt()

        self.log(f"Восстановление состояния ({attempt}/{MAX_RECOVERY_ATTEMPTS}): {reason}")

        if attempt > MAX_RECOVERY_ATTEMPTS:
            await self.stop(f"исчерпаны попытки восстановления: {reason}")
            return False

        # Telethon already delivered the newest state to the local cache.
        # Reading history here adds an RPC and risks replaying an old inline UI.
        return await self.request_map_refresh()

    def watchdog_diagnostic_payload(
        self,
        *,
        elapsed: float,
        timeout: float,
    ) -> dict[str, Any]:
        pending_move = self.context.pending_move
        latest_message = self.latest_received_message
        return {
            "state": self.state.name,
            "elapsed_seconds": round(elapsed, 2),
            "timeout_seconds": round(timeout, 2),
            "last_progress_reason": self.watchdog.reason,
            "recovery_attempts_before": self.watchdog.recovery_attempts,
            "position": self.context.current_position,
            "hp": {
                "current": self.context.current_hp,
                "maximum": self.context.max_hp,
            },
            "active_target": self.context.active_target,
            "battle_target": self.context.battle_target,
            "pending_move": (
                {
                    "origin": pending_move.origin,
                    "destination": pending_move.destination,
                    "button": pending_move.button,
                }
                if pending_move is not None
                else None
            ),
            "event_queue_size": self.event_queue.qsize(),
            "latest_message_id": (
                int(latest_message.id) if latest_message is not None else None
            ),
            "inbound_generation": self.inbound_generation,
            "telegram_actions": self.telegram_action_telemetry.snapshot(),
        }

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
                BotState.ACTIVITY_BREAK,
                BotState.WAITING_FOR_HEALTH,
                BotState.RECOVERY,
            }:
                continue

            # A deliberate safety delay is not a stalled game state. Starting
            # recovery here would add a second request behind the limiter.
            if self.intentional_waits or self.telegram_action_limiter.pending:
                continue

            timeout = self.watchdog.timeout_for_state(
                self.state,
                move_timeout=MOVE_PROGRESS_TIMEOUT,
                target_timeout=TARGET_SELECTION_TIMEOUT,
                combat_timeout=COMBAT_PROGRESS_TIMEOUT,
                general_timeout=GENERAL_PROGRESS_TIMEOUT,
                recovery_timeout=RECOVERY_WATCHDOG_TIMEOUT,
            )
            elapsed = self.watchdog.elapsed()
            should_recover = elapsed >= timeout

            if not should_recover:
                continue

            diagnostic = self.watchdog_diagnostic_payload(
                elapsed=elapsed,
                timeout=timeout,
            )
            self.log(
                "Watchdog обнаружил отсутствие прогресса. "
                "Пробую восстановить состояние без уведомления."
            )

            refresh_requested = await self.recover_latest_state(
                f"watchdog: нет прогресса в состоянии {self.state.name}"
            )
            diagnostic["refresh_requested"] = refresh_requested
            diagnostic["recovery_attempts_after"] = self.watchdog.recovery_attempts
            await self.storage.add_event(
                "WATCHDOG_TRIGGERED",
                f"Нет прогресса в состоянии {diagnostic['state']}",
                level="INFO",
                payload=diagnostic,
            )

    async def handle_message(self, message) -> None:
        if not self.running:
            return

        text = message.raw_text or ""
        hp_changed = self.update_hp(text)
        self.confirm_blessing_from_text(text)

        map_info = parse_map(
            text,
            self.settings.values.enabled_targets,
            CHARACTER_NAME,
        )
        kind = classify_message(
            text,
            self.settings.values.enabled_targets,
            CHARACTER_NAME,
            is_map=map_info is not None,
        )
        round_state = parse_combat_round(text, get_button_texts(message))

        if kind is MessageKind.COMBAT_STARTED:
            self.activate_combat_profile(self.context.max_hp)
        elif self.combat.target_name and self.active_combat_profile_max_hp is None:
            self.activate_combat_profile(self.context.max_hp)

        if kind is MessageKind.COMBAT_STARTED:
            observed_target = extract_combat_target(text)
            normalized_text = normalize(text)
            if "на помощь врагу присоединился" not in normalized_text:
                self.combat_decisions.clear()
                self.pending_combat_decision = None
                self.combat.begin(observed_target, text)
            elif self.combat.target_name is None:
                self.combat.target_name = observed_target
        if self.pending_combat_decision is not None and round_state is not None:
            player_skills = [
                skill
                for skill in round_state.skill_uses
                if normalize(CHARACTER_NAME) in normalize(skill.actor)
            ]
            failed_player_skills = [
                skill
                for skill in round_state.failed_skill_uses
                if normalize(CHARACTER_NAME) in normalize(skill.actor)
            ]
            if player_skills:
                confirmed_skill = player_skills[-1].skill
                expected_skill = self.pending_combat_decision.decision.skill_name
                if normalize(expected_skill) == normalize(confirmed_skill):
                    resolved_trace = resolve_decision_trace(
                        self.pending_combat_decision,
                        round_state,
                        CHARACTER_NAME,
                    )
                    self.combat_decisions.append(resolved_trace)
                    if normalize(expected_skill) == "лечение":
                        planned_target = resolved_trace.decision.target
                        actual_target = resolved_trace.actual_target
                        actual_description = (
                            actual_target.value if actual_target is not None else "unknown"
                        )
                        self.log(
                            "Результат Лечения: "
                            f"план={planned_target.value}, факт={actual_description}, "
                            f"эффект={resolved_trace.actual_effect or 'не распознан'}, "
                            f"значение={resolved_trace.actual_amount or 0}."
                        )
                        target_name = resolved_trace.target_name
                        if actual_target is SkillTarget.ENEMY:
                            self.combat.confirm_treatment_enemy(target_name)
                            await self.settings.add_treatment_enemy_target(target_name)
                        elif (
                            planned_target is SkillTarget.ENEMY
                            and actual_target is SkillTarget.SELF
                        ):
                            self.combat.revoke_treatment_enemy(target_name)
                            removed = await self.settings.remove_treatment_enemy_target(
                                target_name
                            )
                            if removed:
                                await self.storage.add_event(
                                    "TREATMENT_ENEMY_REVOKED",
                                    f"Лечение недоступно как атака для «{target_name}»",
                                )
                else:
                    self.log(
                        "Игра подтвердила другой навык: "
                        f"ожидался «{expected_skill}», применён «{confirmed_skill}». "
                        "Решение исключено из статистики."
                    )
                self.pending_combat_decision = None
            elif failed_player_skills:
                failed = failed_player_skills[-1]
                self.log(
                    f"Навык «{failed.skill}» не применён: {failed.reason}. "
                    "Решение исключено из статистики."
                )
                self.pending_combat_decision = None
        self.combat.observe(text, CHARACTER_NAME, round_state)

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

        defeated_enemies = round_state.defeated if round_state is not None else ()
        for defeated_enemy in defeated_enemies:
            self.context.remove_combat_enemy(defeated_enemy)
            self.log(f"Противник повержен: {defeated_enemy}")
            if normalize(self.combat.target_name or "") == normalize(defeated_enemy):
                if self.context.active_target:
                    self.combat.begin(self.context.active_target)
                else:
                    self.combat.reset()

        if kind is MessageKind.MAP:
            assert map_info is not None
            await self.handle_map(message, map_info)
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
                _, cards = await self.storage.record_battle(
                    telegram_message_id=message.id,
                    session_id=self.session_id,
                    target_name=target_name,
                    result="VICTORY",
                    xp=reward.xp,
                    dust=reward.dust,
                    items=reward.items,
                    position=self.context.current_position,
                    combat_decisions=tuple(
                        trace.as_payload() for trace in self.combat_decisions
                    ),
                )
                await self.persist_combat_knowledge()
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
                self.combat.reset()
                self.combat_decisions.clear()
                self.pending_combat_decision = None
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

        # Отложенная запись прогресса могла быть отменена строками выше.
        # Сохраняем фактические счётчики синхронно до завершения сессии.
        await self.storage.update_state(
            **self._state_snapshot(reason, pause_requested=False)
        )

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
            payload={
                "telegram_actions": (
                    self.telegram_action_telemetry.snapshot()
                    if hasattr(self, "telegram_action_telemetry")
                    else {}
                )
            },
        )
        await self.persist_combat_knowledge()
        await self.storage.checkpoint()
        for task in (
            self.worker_task,
            self.watchdog_task,
            self.recovery_task,
            self.rest_task,
            self.activity_break_task,
            self.progress_persist_task,
        ):
            if task and task is not asyncio.current_task():
                task.cancel()

        if self.client.is_connected():
            await self.client.disconnect()

    async def run(self) -> None:
        self.validate_config()
        await self.load_combat_knowledge()
        if not self.settings.values.treatment_targets_initialized:
            historical_treatment_targets = (
                await self.storage.get_confirmed_treatment_targets()
            )
            for target in sorted(historical_treatment_targets):
                await self.settings.add_treatment_enemy_target(target)
            await self.settings.mark_treatment_targets_initialized()
        for target in self.settings.values.treatment_enemy_targets:
            self.combat.confirm_treatment_enemy(target)
            for knowledge in self.combat_knowledge_profiles.values():
                knowledge.confirm_treatment_enemy(target)
        if self.settings.values.treatment_enemy_targets:
            self.log(
                "Подтверждённые цели атакующего Лечения: "
                f"{sorted(self.settings.values.treatment_enemy_targets)}"
            )
        deleted_logs = self.cleanup_old_log_files()
        cleanup = await self.storage.cleanup_old_data(DATA_RETENTION_DAYS)
        learning_rows = await self.storage.backfill_combat_battle_analysis()
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
        if learning_rows:
            logger.info(
                "Подготовлены профильные итоги прошлых боёв: %s.",
                learning_rows,
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
