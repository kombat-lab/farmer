from __future__ import annotations

import asyncio
import random
import time
from pathlib import Path
from collections import Counter, deque
from typing import Optional

from telethon import TelegramClient, events
from telethon.errors import RPCError

from config import (
    API_HASH,
    API_ID,
        CHARACTER_NAME,
    COMBAT_PROGRESS_TIMEOUT,
    DEATH_RECOVERY_MAX_WAIT,
    DEATH_RECOVERY_MIN_WAIT,
    DEATH_RECOVERY_RECHECK_INTERVAL,
    DATA_RETENTION_DAYS,
    LOG_RETENTION_DAYS,
    GAME_BOT,
    GENERAL_PROGRESS_TIMEOUT,
        LOG_BACKUP_COUNT,
    LOG_DIRECTORY,
    LOG_FILENAME,
    LOG_MAX_BYTES,
    MAP_MAX_X,
    MAP_MAX_Y,
    MAP_MIN_X,
    MAP_MIN_Y,
        MAX_RECOVERY_ATTEMPTS,
    MIN_HP_AFTER_DEATH,
        MOVE_PROGRESS_TIMEOUT,
    RECOVERY_WATCHDOG_TIMEOUT,
    SESSION_NAME,
        STATE_HISTORY_LIMIT,
        TARGET_SELECTION_TIMEOUT,
    WATCHDOG_CHECK_INTERVAL,
)
from models import (
    ActionType,
    BotState,
    MessageKind,
    RuntimeContext,
)
from navigator import SnakeNavigator
from parser import (
    classify_message,
    extract_combat_target,
    extract_player_hp,
    normalize,
    parse_map,
)
from rewards import parse_battle_reward
from statistics import FarmStatistics, format_report
from watchdog import ProgressWatchdog
from logger_setup import setup_logging
from storage import Storage, utc_now
from notifications import Notifier
from settings_service import SettingsService
from combat_events import parse_combat_round_events
from skills import choose_skill, parse_current_mana
from targeting import analyze_map_targets, select_combat_target
from telegram_buttons import find_button, get_button_texts


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

MAX_FAILED_MOVE_ATTEMPTS = 2
EVENT_QUEUE_SIZE = 200
PROCESSED_EVENT_CACHE_SIZE = 500

BLESSING_REFRESH_INTERVAL = 29 * 60
BLESSING_RETRY_INTERVAL = 5 * 60
NON_COMBAT_SKILLS_BUTTON = "Небоевые навыки"
BLESSING_BUTTON = "Благословение"
BLESSING_STATUS_MARKER = "благословение: +5 ко всем характеристикам на 30 мин"


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
        )

        self.game_bot = None
        self.state = BotState.STARTING
        self.running = True

        self.context = RuntimeContext()
        self.statistics = FarmStatistics()
        self.runtime_finalized = False

        self.navigator = SnakeNavigator(
            min_x=MAP_MIN_X,
            max_x=MAP_MAX_X,
            min_y=MAP_MIN_Y,
            max_y=MAP_MAX_Y,
        )

        self.watchdog = ProgressWatchdog()
        self.watchdog_task: Optional[asyncio.Task] = None
        self.recovery_task: Optional[asyncio.Task] = None
        self.recovery_started_at: Optional[float] = None
        self.pause_requested = False
        self.current_cycle = 1
        self.moves_in_cycle = 0
        self.rest_task: Optional[asyncio.Task] = None

        self.event_queue: asyncio.Queue = asyncio.Queue(
            maxsize=EVENT_QUEUE_SIZE
        )
        self.worker_task: Optional[asyncio.Task] = None

        self.processed_event_keys: set[tuple] = set()
        self.processed_event_order: deque[tuple] = deque(
            maxlen=PROCESSED_EVENT_CACHE_SIZE
        )

        self.blessing_refreshed_at: float | None = None
        self.blessing_next_attempt_at = 0.0
        self.blessing_refresh_in_progress = False

    def log(self, text: str) -> None:
        logger.info("[%s] %s", self.state.name, text)

    def mark_progress(self, reason: str) -> None:
        self.watchdog.mark_progress(reason)
        if self.running:
            asyncio.create_task(
                self._persist_progress(reason)
            )

    async def _persist_progress(self, reason: str) -> None:
        # Задача могла попасть в очередь до остановки. В таком случае
        # не даём ей перезаписать финальную причину завершения сессии.
        if not self.running:
            return

        await self.storage.update_state(
            game_state=self.state.name,
            position_x=(
                self.context.current_position[0]
                if self.context.current_position else None
            ),
            position_y=(
                self.context.current_position[1]
                if self.context.current_position else None
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

        if self.settings.values.heal_amount <= 0:
            raise ValueError("heal_amount должен быть больше нуля.")

    def remember_event(self, message) -> bool:
        edit_timestamp = (
            message.edit_date.timestamp()
            if message.edit_date
            else None
        )
        key = (
            message.id,
            edit_timestamp,
            message.raw_text or "",
        )

        if key in self.processed_event_keys:
            return False

        if len(self.processed_event_order) == self.processed_event_order.maxlen:
            oldest = self.processed_event_order.popleft()
            self.processed_event_keys.discard(oldest)

        self.processed_event_order.append(key)
        self.processed_event_keys.add(key)
        return True

    async def enqueue_message(
        self,
        message,
        *,
        force: bool = False,
    ) -> None:
        if not self.running:
            return

        # Live-события Telethon защищаем от дублей. Но сообщения,
        # которые мы специально нашли в истории при запуске или
        # восстановлении, должны быть обработаны повторно: это
        # фактическое текущее состояние игры, даже если такая редакция
        # сообщения уже встречалась в предыдущем цикле.
        if not force and not self.remember_event(message):
            return

        try:
            self.event_queue.put_nowait(message)
        except asyncio.QueueFull:
            await self.stop(
                "очередь Telegram-событий переполнена"
            )

    async def event_worker(self) -> None:
        while self.running:
            message = await self.event_queue.get()

            try:
                await self.handle_message(message)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self.stop(
                    f"ошибка обработки сообщения: "
                    f"{type(error).__name__}: {error}"
                )
            finally:
                self.event_queue.task_done()

    @staticmethod
    def get_button_texts(message) -> list[str]:
        return get_button_texts(message)

    @staticmethod
    def find_button(
        message,
        *,
        exact: Optional[str] = None,
        contains: tuple[str, ...] = (),
        exclude: tuple[str, ...] = (),
    ):
        return find_button(
            message,
            exact=exact,
            contains=contains,
            exclude=exclude,
        )

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

    async def get_fresh_message(self, message_id: int):
        fresh = await self.client.get_messages(
            self.game_bot,
            ids=message_id,
        )
        return fresh if fresh and fresh.id else None

    async def click_button(
        self,
        message,
        *,
        action_type: ActionType,
        description: str,
        exact: Optional[str] = None,
        contains: tuple[str, ...] = (),
        exclude: tuple[str, ...] = (),
    ) -> bool:
        delay = self.action_delay(action_type)

        self.log(
            f"Ожидание {delay:.1f} сек. "
            f"перед действием: {description}"
        )
        await asyncio.sleep(delay)

        if not self.running:
            return False

        fresh_message = await self.get_fresh_message(message.id)
        if fresh_message is None:
            return False

        position = self.find_button(
            fresh_message,
            exact=exact,
            contains=contains,
            exclude=exclude,
        )
        if position is None:
            self.log(
                f"Кнопка «{description}» больше недоступна. "
                f"Текущие кнопки: {self.get_button_texts(fresh_message)}"
            )
            return False

        row, column = position

        try:
            self.log(f"Нажимаю: {description}")
            await fresh_message.click(row, column)
            return True
        except Exception as error:
            self.log(
                f"Ошибка нажатия «{description}»: "
                f"{type(error).__name__}: {error}"
            )
            return False

    def update_hp(self, text: str) -> None:
        hp = extract_player_hp(text, CHARACTER_NAME)
        if hp is None:
            return

        current_hp, max_hp = hp
        if (
            current_hp != self.context.current_hp
            or max_hp != self.context.max_hp
        ):
            self.context.current_hp = current_hp
            self.context.max_hp = max_hp
            self.log(
                f"Здоровье обновлено: {current_hp}/{max_hp}"
            )

    def confirm_pending_move(
        self,
        current_position: tuple[int, int],
    ) -> None:
        plan = self.context.pending_move
        if plan is None:
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
            self.context.failed_move_attempts += 1
            self.log(
                f"Перемещение через {plan.button} не выполнено. "
                f"Неудач подряд: "
                f"{self.context.failed_move_attempts}/"
                f"{MAX_FAILED_MOVE_ATTEMPTS}"
            )
        else:
            recovered = self.navigator.recover_from_actual_transition(
                plan.origin,
                current_position,
            )
            if recovered:
                self.context.move_count += 1
                self.context.failed_move_attempts = 0
                self.mark_progress(
                    "навигатор пересинхронизирован"
                )
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
            await self.stop(
                f"завершены все циклы: {total}"
            )
            return

        rest_seconds = random.uniform(
            self.settings.values.cycle_rest_min,
            self.settings.values.cycle_rest_max,
        )
        self.state = BotState.RESTING
        self.mark_progress(
            f"передышка после цикла {self.current_cycle}: {int(rest_seconds)} сек."
        )
        await self.storage.add_event(
            "CYCLE_COMPLETED",
            f"Завершён цикл {self.current_cycle} из {total}; передышка {int(rest_seconds)} сек.",
        )
        await self.notifier.send(
            f"😴 Завершён цикл {self.current_cycle} из {total}\n"
            f"Передышка: {int(rest_seconds // 60)} мин. {int(rest_seconds % 60)} сек."
        )
        self.rest_task = asyncio.create_task(
            self.rest_between_cycles(rest_seconds)
        )

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

    def blessing_refresh_due(self) -> bool:
        now = time.monotonic()
        if self.blessing_refresh_in_progress:
            return False
        if now < self.blessing_next_attempt_at:
            return False
        if self.blessing_refreshed_at is None:
            return True
        return now - self.blessing_refreshed_at >= BLESSING_REFRESH_INTERVAL

    async def try_refresh_blessing_from_map(self, message) -> bool:
        if not self.blessing_refresh_due():
            return False

        clicked = await self.click_button(
            message,
            contains=(NON_COMBAT_SKILLS_BUTTON,),
            action_type=ActionType.OPEN_ATTACK,
            description=NON_COMBAT_SKILLS_BUTTON,
        )
        if not clicked:
            self.blessing_next_attempt_at = (
                time.monotonic() + BLESSING_RETRY_INTERVAL
            )
            self.log(
                "Не удалось открыть небоевые навыки. "
                "Повторю попытку через 5 минут."
            )
            return False

        self.blessing_refresh_in_progress = True
        self.blessing_next_attempt_at = (
            time.monotonic() + BLESSING_RETRY_INTERVAL
        )
        self.mark_progress("открыто меню небоевых навыков")
        return True

    async def handle_blessing_menu(self, message) -> bool:
        if not self.blessing_refresh_in_progress:
            return False

        position = self.find_button(
            message,
            contains=(BLESSING_BUTTON,),
        )
        if position is None:
            return False

        clicked = await self.click_button(
            message,
            contains=(BLESSING_BUTTON,),
            action_type=ActionType.USE_SKILL,
            description=BLESSING_BUTTON,
        )
        if clicked:
            self.mark_progress("использовано Благословение")
        else:
            self.blessing_refresh_in_progress = False
        return True

    def confirm_blessing_from_text(self, text: str) -> None:
        if not self.blessing_refresh_in_progress:
            return
        if BLESSING_STATUS_MARKER not in normalize(text):
            return

        self.blessing_refreshed_at = time.monotonic()
        self.blessing_next_attempt_at = (
            self.blessing_refreshed_at + BLESSING_REFRESH_INTERVAL
        )
        self.blessing_refresh_in_progress = False
        self.log("Благословение подтверждено. Следующее обновление через 29 минут.")
        self.mark_progress("Благословение обновлено")

    async def handle_map(self, message, text: str) -> None:
        map_info = parse_map(
            text,
            self.settings.values.enabled_targets,
            CHARACTER_NAME,
        )
        if map_info is None:
            return

        self.context.current_position = map_info.position
        if map_info.current_hp is not None:
            self.context.current_hp = map_info.current_hp
            self.context.max_hp = map_info.max_hp

        self.confirm_pending_move(map_info.position)

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

        if (
            self.context.failed_move_attempts
            >= MAX_FAILED_MOVE_ATTEMPTS
        ):
            await self.recover_latest_state(
                "неудачные перемещения"
            )
            return

        if (
            self.context.checked_empty_position is not None
            and self.context.checked_empty_position
            != map_info.position
        ):
            self.context.checked_empty_position = None

        self.log(
            f"Карта: позиция {map_info.position}, "
            f"HP: {self.context.current_hp}/"
            f"{self.context.max_hp}, "
            f"монстров заявлено: {map_info.monster_count}, "
            f"показано: {list(map_info.monsters) or 'нет'}"
        )

        if (
            map_info.movement_finished
            and random.random() < self.settings.values.long_pause_chance
        ):
            pause = random.uniform(
                self.settings.values.long_pause_min,
                self.settings.values.long_pause_max,
            )
            await asyncio.sleep(pause)

        if await self.try_refresh_blessing_from_map(message):
            return

        if map_info.found_target is not None:
            self.context.active_target = map_info.found_target
            self.context.checking_hidden_monsters = False
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
            self.context.checked_empty_position
            == map_info.position
        ):
            self.context.checking_hidden_monsters = False
        elif map_info.has_hidden_monsters:
            self.context.active_target = None
            self.context.checking_hidden_monsters = True

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
        else:
            self.context.checking_hidden_monsters = False

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

    def analyze_target_buttons(
        self,
        message,
    ) -> tuple[Optional[str], dict[str, tuple[int, int]]]:
        analysis = analyze_map_targets(
            message,
            self.settings.values.enabled_targets,
        )
        return analysis.selected_target, analysis.target_counts

    async def handle_target_selection(
        self,
        message,
    ) -> None:
        self.state = BotState.TARGET_SELECTION
        self.mark_progress("список целей получен")

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

        found_target, target_counts = (
            self.analyze_target_buttons(message)
        )

        event_key = (
            f"{message.id}:"
            f"{message.edit_date.timestamp() if message.edit_date else 0}:"
            f"{'|'.join(self.get_button_texts(message))}"
        )
        self.statistics.record_target_list(
            event_key,
            target_counts,
        )

        if found_target is not None:
            self.context.active_target = found_target
            self.context.battle_target = found_target
            self.context.checking_hidden_monsters = False
            self.context.checked_empty_position = None

            clicked = await self.click_button(
                message,
                contains=(found_target,),
                exclude=("pvp:", "занят"),
                action_type=ActionType.SELECT_TARGET,
                description=f"выбор цели {found_target}",
            )

            if clicked:
                self.statistics.record_attacked(
                    found_target
                )
                self.state = BotState.COMBAT
                self.mark_progress("цель выбрана")
            return

        self.context.active_target = None

        # Если в списке были наши мобы, но все они заняты, клетка уже
        # полностью проверена. То же самое относится к проверке скрытых
        # монстров. После возврата на карту нужно перейти дальше, а не
        # снова открывать тот же список целей.
        all_matching_targets_are_occupied = bool(target_counts) and all(
            found > 0 and occupied >= found
            for found, occupied in target_counts.values()
        )

        if (
            self.context.current_position is not None
            and (
                self.context.checking_hidden_monsters
                or all_matching_targets_are_occupied
            )
        ):
            self.context.checked_empty_position = (
                self.context.current_position
            )

            if all_matching_targets_are_occupied:
                occupied_summary = ", ".join(
                    f"{target}: {occupied}/{found}"
                    for target, (found, occupied)
                    in target_counts.items()
                )
                self.log(
                    "Все подходящие цели на клетке заняты. "
                    f"Клетка {self.context.current_position} "
                    "помечена как проверенная. "
                    f"Занято: {occupied_summary}"
                )

        self.context.checking_hidden_monsters = False

        clicked = await self.click_button(
            message,
            exact=BACK_TO_MAP_BUTTON,
            action_type=ActionType.SELECT_TARGET,
            description=BACK_TO_MAP_BUTTON,
        )
        if clicked:
            self.mark_progress("возврат к карте")
        else:
            await self.recover_latest_state(
                "не удалось вернуться к карте"
            )

    def analyze_combat_target_buttons(self, message):
        return select_combat_target(
            message,
            self.settings.values.enabled_targets,
            self.context.active_target,
        )

    async def handle_combat_target_selection(
        self,
        message,
    ) -> None:
        self.state = BotState.COMBAT
        self.mark_progress("получен список целей навыка")

        target_name, position = self.analyze_combat_target_buttons(
            message
        )

        if position is None:
            await self.recover_latest_state(
                "не найдена доступная цель навыка"
            )
            return

        delay = self.action_delay(ActionType.SELECT_TARGET)
        self.log(
            f"Ожидание {delay:.1f} сек. перед выбором "
            f"боевой цели: {target_name}"
        )
        await asyncio.sleep(delay)

        if not self.running:
            return

        fresh_message = await self.get_fresh_message(message.id)
        if fresh_message is None:
            await self.recover_latest_state(
                "сообщение выбора боевой цели исчезло"
            )
            return

        fresh_target_name, fresh_position = (
            self.analyze_combat_target_buttons(fresh_message)
        )

        if fresh_position is None:
            self.mark_progress(
                "выбор цели навыка больше не требуется"
            )
            return

        row, column = fresh_position

        try:
            self.log(
                "Выбираю боевую цель: "
                f"{fresh_target_name or target_name}"
            )
            await fresh_message.click(row, column)
            self.mark_progress(
                "цель атакующего навыка выбрана"
            )
        except Exception as error:
            self.log(
                "Ошибка выбора боевой цели: "
                f"{type(error).__name__}: {error}"
            )
            await self.recover_latest_state(
                "не удалось выбрать цель навыка"
            )

    def skill_available(self, message, skill_name: str) -> bool:
        return choose_skill(
            message,
            current_hp=self.context.current_hp,
            max_hp=(self.context.max_hp or self.settings.values.max_hp),
            heal_amount=self.settings.values.heal_amount,
        ) == normalize(skill_name)

    async def handle_combat_turn(self, message) -> None:
        self.state = BotState.COMBAT
        self.mark_progress("ход игрока")

        current_mana = parse_current_mana(message.raw_text or "")
        self.log(
            "Выбор навыка: "
            f"мана={current_mana if current_mana is not None else 'не распознана'}"
        )

        skill_name = choose_skill(
            message,
            current_hp=self.context.current_hp,
            max_hp=(self.context.max_hp or self.settings.values.max_hp),
            heal_amount=self.settings.values.heal_amount,
        )
        if skill_name is None:
            await self.recover_latest_state("не найден доступный навык")
            return

        self.context.pending_skill = skill_name
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
        self.statistics.add_defeat(
            message_id,
            target_name,
        )
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
        self.context.checking_hidden_monsters = False

        self.state = BotState.RECOVERY
        self.recovery_started_at = time.monotonic()
        await self.storage.add_event(
            "PLAYER_DEFEATED",
            f"Поражение от {target_name}; ожидание восстановления HP",
            level="WARNING",
        )
        await self.notifier.send(
            "☠️ Персонаж погиб\n"
            f"Цель: {target_name}\n"
            "Начато восстановление здоровья."
        )
        self.mark_progress("начато восстановление после смерти")

        if self.recovery_task:
            self.recovery_task.cancel()

        self.recovery_task = asyncio.create_task(
            self.death_recovery_loop()
        )

    async def death_recovery_loop(self) -> None:
        await asyncio.sleep(DEATH_RECOVERY_MIN_WAIT)

        while self.running and self.state is BotState.RECOVERY:
            elapsed = (
                time.monotonic()
                - (self.recovery_started_at or time.monotonic())
            )

            if elapsed > DEATH_RECOVERY_MAX_WAIT:
                await self.stop(
                    "HP не восстановилось за предельное время"
                )
                return

            await self.request_map_refresh()
            await asyncio.sleep(
                DEATH_RECOVERY_RECHECK_INTERVAL
            )

    async def handle_recovery_map(
        self,
        message,
        map_info,
    ) -> None:
        elapsed = (
            time.monotonic()
            - (self.recovery_started_at or time.monotonic())
        )

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
            self.mark_progress("здоровье восстановлено")
            await self.storage.add_event(
                "RECOVERY_FINISHED",
                f"HP восстановлено до {current_hp}/{map_info.max_hp}",
            )
            await self.notifier.send(
                "✅ Здоровье восстановлено\n"
                f"HP: {current_hp}/{map_info.max_hp}\n"
                "Фарм продолжен."
            )

            if self.recovery_task:
                self.recovery_task.cancel()
                self.recovery_task = None

            await self.handle_map(message, message.raw_text or "")

    async def handle_target_gone(self) -> None:
        """Штатно восстанавливает карту, если выбранный моб уже исчез."""
        disappeared_target = self.context.active_target or "неизвестная цель"

        self.context.active_target = None
        self.context.checking_hidden_monsters = False
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
            f"Монстр «{disappeared_target}» исчез с клетки. "
            "Обновляю карту и продолжаю маршрут."
        )

        await self.request_map_refresh()

    async def request_map_refresh(self) -> None:
        messages = await self.client.get_messages(
            self.game_bot,
            limit=20,
        )

        for message in messages:
            if self.find_button(
                message,
                exact=LOOK_BUTTON,
            ) is not None:
                await self.click_button(
                    message,
                    exact=LOOK_BUTTON,
                    action_type=ActionType.OPEN_ATTACK,
                    description=LOOK_BUTTON,
                )
                return

        await self.client.send_message(
            self.game_bot,
            MAP_COMMAND,
        )

    async def recover_latest_state(
        self,
        reason: str,
    ) -> None:
        attempt = self.watchdog.begin_recovery_attempt()
        self.statistics.record_recovery_attempt()

        self.log(
            f"Восстановление состояния "
            f"({attempt}/{MAX_RECOVERY_ATTEMPTS}): {reason}"
        )

        if attempt > MAX_RECOVERY_ATTEMPTS:
            await self.stop(
                f"исчерпаны попытки восстановления: {reason}"
            )
            return

        messages = await self.client.get_messages(
            self.game_bot,
            limit=STATE_HISTORY_LIMIT,
        )

        for message in messages:
            kind = classify_message(
                message.raw_text or "",
                self.settings.values.enabled_targets,
                CHARACTER_NAME,
            )

            if kind is MessageKind.TARGET_GONE:
                self.statistics.record_successful_recovery()
                await self.enqueue_message(message, force=True)
                return

            if kind is MessageKind.PLAYER_TURN:
                self.statistics.record_successful_recovery()
                await self.enqueue_message(message, force=True)
                return

            if kind is MessageKind.COMBAT_TARGET_SELECTION:
                self.statistics.record_successful_recovery()
                await self.enqueue_message(message, force=True)
                return

            if kind is MessageKind.COMBAT_STARTED:
                self.statistics.record_successful_recovery()
                await self.enqueue_message(message, force=True)
                return

            if kind is MessageKind.TARGET_SELECTION:
                self.statistics.record_successful_recovery()
                await self.enqueue_message(message, force=True)
                return

            if kind is MessageKind.MAP:
                self.statistics.record_successful_recovery()
                await self.enqueue_message(message, force=True)
                return

            if kind is MessageKind.BATTLE_FINISHED:
                self.statistics.record_successful_recovery()
                await self.enqueue_message(message, force=True)
                return

            if kind is MessageKind.MOVE_STARTED:
                self.statistics.record_successful_recovery()
                await self.request_map_refresh()
                return

        await self.request_map_refresh()

    async def watchdog_loop(self) -> None:
        while self.running:
            await asyncio.sleep(WATCHDOG_CHECK_INTERVAL)

            if self.state in {BotState.PAUSED, BotState.RESTING}:
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

            self.statistics.record_watchdog_trigger()

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

            if self.state is BotState.RECOVERY:
                await self.request_map_refresh()
                self.mark_progress(
                    "watchdog обновил карту в восстановлении"
                )
                continue

            await self.recover_latest_state(
                f"watchdog: нет прогресса в состоянии "
                f"{self.state.name}"
            )

    async def handle_message(self, message) -> None:
        if not self.running:
            return

        text = message.raw_text or ""
        self.update_hp(text)
        self.confirm_blessing_from_text(text)

        if await self.handle_blessing_menu(message):
            return

        round_events = parse_combat_round_events(text)
        for defeated_enemy in round_events.defeated_enemies:
            self.context.remove_combat_enemy(defeated_enemy)
            self.log(f"Противник повержен: {defeated_enemy}")

        kind = classify_message(
            text,
            self.settings.values.enabled_targets,
            CHARACTER_NAME,
        )

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
                self.context.checking_hidden_monsters = False
                self.context.checked_empty_position = None

                if combat_target:
                    self.context.active_target = combat_target
                    self.context.add_combat_enemy(combat_target)
                self.context.pending_skill = None

                self.log(
                    "Обнаружено внезапное нападение"
                    + (
                        f": {combat_target}"
                        if combat_target
                        else ""
                    )
                )
                self.mark_progress("внезапное нападение")
            elif "на помощь врагу присоединился" in normalized_text:
                if combat_target:
                    self.context.add_combat_enemy(combat_target)
                self.log(
                    "К бою присоединился дополнительный моб"
                    + (
                        f": {combat_target}"
                        if combat_target
                        else ""
                    )
                )
                self.mark_progress(
                    "к врагу присоединилось подкрепление"
                )
            else:
                if combat_target:
                    self.context.active_target = combat_target
                    self.context.add_combat_enemy(combat_target)
                self.context.pending_skill = None

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
                    target_name,
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
                    self.context.kill_count += 1
                    logger.info(
                        "\n%s",
                        format_report(
                            "СТАТИСТИКА ТЕКУЩЕЙ СЕССИИ",
                            self.statistics.session_report(),
                        ),
                    )

                self.context.clear_combat()
                self.context.checking_hidden_monsters = False
                self.mark_progress("бой завершён победой")
                return

            if "Поражение" in text:
                await self.enter_death_recovery(
                    message.id
                )
                return

    async def process_latest_state(self) -> None:
        messages = await self.client.get_messages(
            self.game_bot,
            limit=STATE_HISTORY_LIMIT,
        )

        map_history: list[tuple[int, int]] = []

        for message in reversed(messages):
            map_info = parse_map(
                message.raw_text or "",
                self.settings.values.enabled_targets,
                CHARACTER_NAME,
            )
            if map_info is None:
                continue
            if (
                not map_history
                or map_history[-1] != map_info.position
            ):
                map_history.append(map_info.position)

        self.navigator.initialize_from_history(
            map_history
        )

        for message in messages:
            kind = classify_message(
                message.raw_text or "",
                self.settings.values.enabled_targets,
                CHARACTER_NAME,
            )

            if kind in {
                MessageKind.PLAYER_TURN,
                MessageKind.COMBAT_TARGET_SELECTION,
                MessageKind.COMBAT_STARTED,
                MessageKind.TARGET_SELECTION,
                MessageKind.MAP,
                MessageKind.BATTLE_FINISHED,
                MessageKind.TARGET_GONE,
            }:
                self.statistics.record_startup_state_recovery()
                await self.enqueue_message(message, force=True)
                return

            if kind is MessageKind.MOVE_STARTED:
                self.statistics.record_startup_state_recovery()
                self.state = BotState.MOVING
                self.mark_progress(
                    "запуск во время перемещения"
                )
                return

        self.mark_progress(
            "при синхронизации запрошено обновление карты"
        )
        await self.request_map_refresh()

    async def stop(self, reason: str) -> None:
        if not self.running:
            return

        self.stop_reason = reason
        self.running = False
        self.state = BotState.STOPPED

        if not self.runtime_finalized:
            self.statistics.finalize_runtime()
            self.runtime_finalized = True

        logger.info(
            "\n%s",
            format_report(
                "ИТОГ ТЕКУЩЕЙ СЕССИИ",
                self.statistics.session_report(),
            ),
        )
        logger.info(
            "\n%s",
            format_report(
                "ОБЩАЯ СТАТИСТИКА",
                self.statistics.total_report(),
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
        for task in (
            self.worker_task,
            self.watchdog_task,
            self.recovery_task,
            self.rest_task,
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
            "Очистка хранения: срок %s дн.; events=%s, battles=%s, drops=%s, sessions=%s, unknown=%s, logs=%s",
            DATA_RETENTION_DAYS, cleanup["events"], cleanup["battles"], cleanup["drops"],
            cleanup["sessions"], cleanup["unknown_battles"], deleted_logs,
        )
        self.session_id = await self.storage.start_session(
            cycles_count=self.settings.values.cycles_count,
            moves_per_cycle=self.settings.values.moves_per_cycle,
        )
        await self.storage.add_event(
            "FARMER_STARTED",
            f"Фармер запущен: {self.settings.values.cycles_count} цикл(а), по {self.settings.values.moves_per_cycle} ходов",
        )
        await self.notifier.send(
            "▶️ Фармер запущен\n"
            f"Циклов: {self.settings.values.cycles_count}\nХодов в цикле: {self.settings.values.moves_per_cycle}"
        )

        await self.client.start()
        self.game_bot = await self.client.get_entity(GAME_BOT)
        me = await self.client.get_me()

        logger.info("=" * 72)
        logger.info("FoG Farmer запущен")
        logger.info(
            "Telegram-аккаунт: %s",
            me.first_name or "без имени",
        )
        logger.info("Персонаж: %s", CHARACTER_NAME)
        logger.info("Цели: %s", self.settings.values.enabled_targets)
        logger.info(
            "Watchdog: движение %s сек., бой %s сек.",
            MOVE_PROGRESS_TIMEOUT,
            COMBAT_PROGRESS_TIMEOUT,
        )
        logger.info(
            "После смерти: ожидание минимум %s сек., "
            "возврат при HP >= %s",
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
            await self.enqueue_message(event.message)

        @self.client.on(events.MessageEdited(chats=self.game_bot))
        async def on_edited_message(event) -> None:
            await self.enqueue_message(event.message)

        self.worker_task = asyncio.create_task(
            self.event_worker()
        )
        self.watchdog_task = asyncio.create_task(
            self.watchdog_loop()
        )

        await self.process_latest_state()
        await self.client.run_until_disconnected()
