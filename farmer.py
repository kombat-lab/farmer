from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import Counter, defaultdict, deque
from collections.abc import Coroutine, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from telethon import events
from telethon.errors import FloodWaitError, RPCError

from automation_policy import DelayRange
from bounded_values import require_int64
from config import (
    ACTIVITY_BREAK_DURATION_MAX,
    ACTIVITY_BREAK_DURATION_MIN,
    ACTIVITY_BREAK_MOVES_MAX,
    ACTIVITY_BREAK_MOVES_MIN,
    ACTIVITY_BREAK_WORK_MAX,
    ACTIVITY_BREAK_WORK_MIN,
    API_HASH,
    API_ID,
    COMBAT_PROGRESS_TIMEOUT,
    DATA_RETENTION_DAYS,
    GAME_BOT,
    GENERAL_PROGRESS_TIMEOUT,
    LOG_DIRECTORY,
    LOG_FILENAME,
    LOG_RETENTION_DAYS,
    MAX_RECOVERY_ATTEMPTS,
    MOVE_PROGRESS_TIMEOUT,
    RECOVERY_WATCHDOG_TIMEOUT,
    TARGET_SELECTION_TIMEOUT,
    TELEGRAM_ACTION_MIN_INTERVAL,
    TELEGRAM_CALLBACK_RPC_TIMEOUT,
    TELEGRAM_FLOOD_INCIDENT_WINDOW,
    TELEGRAM_FLOOD_WAIT_BUFFER,
    TELEGRAM_RECOVERY_LIMIT,
    TELEGRAM_RECOVERY_WINDOW,
    WATCHDOG_CHECK_INTERVAL,
)
from event_cache import BoundedKeyCache
from event_ingress import EventIngress, IngressClosedError
from game_input import ActionKey, ActionOutcome, InboundEvent
from game_mechanisms import (
    CycleDescriptor,
    MechanismBundle,
    MechanismServices,
    MechanismSnapshot,
    require_mechanism_runtime,
)
from game_message import GameMessage
from human_delays import ActivityBreakPlanner, HumanDelayModel
from inbound_message import InboundMessage
from json_types import JsonValue
from liveness import LivenessPhase, LivenessPolicy, ProgressMonitor
from message_snapshot import MessageSnapshot
from notifications import Notifier
from runtime_state import BotState, require_phase_name
from settings_service import SettingsService
from storage import Storage, utc_now
from storage_types import FarmerStatePatch, TelegramSafetyStatus
from task_scope import TaskScope
from telegram_action_executor import CallbackStatus, TelegramActionExecutor
from telegram_buttons import ButtonPosition, find_button, get_button_texts
from telegram_client_port import TelegramClientPort
from telegram_safety import (
    RollingAttemptGuard,
    StateRefreshGate,
    TelegramActionLimiter,
    TelegramActionTelemetry,
)

logger = logging.getLogger("fog_farmer")


TELEGRAM_STATE_RPC_TIMEOUT = 10.0

EVENT_QUEUE_SIZE = 200
PROCESSED_EVENT_CACHE_SIZE = 500
LATEST_MESSAGE_CACHE_SIZE = 200
SHUTDOWN_STEP_TIMEOUT = 5.0

_APPLICATION_PHASE_OVERRIDES = frozenset(
    {
        BotState.PAUSED,
        BotState.RESTING,
        BotState.ACTIVITY_BREAK,
        BotState.STOPPED,
    }
)


@dataclass(frozen=True, slots=True)
class MechanismView:
    """Validated immutable runtime state safe to expose outside the mechanism."""

    snapshot: MechanismSnapshot
    cycle: CycleDescriptor | None

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, MechanismSnapshot):
            raise TypeError("Mechanism runtime returned an invalid snapshot")
        if self.cycle is not None and not isinstance(self.cycle, CycleDescriptor):
            raise TypeError("Mechanism runtime returned an invalid cycle descriptor")


@dataclass(frozen=True, slots=True)
class FinalMechanismView:
    """Terminal mechanism facts captured before the runtime releases resources."""

    status: MechanismView
    session_report: str
    session_elapsed_seconds: int

    def __post_init__(self) -> None:
        if not isinstance(self.status, MechanismView):
            raise TypeError("Final mechanism status must be a MechanismView")
        if type(self.session_report) is not str:
            raise TypeError("Mechanism session report must be a string")
        elapsed = require_int64(
            self.session_elapsed_seconds,
            "Mechanism session elapsed seconds",
            minimum=0,
        )
        object.__setattr__(self, "session_elapsed_seconds", elapsed)


class Farmer:
    def __init__(
        self,
        storage: Storage,
        notifier: Notifier,
        settings: SettingsService,
        *,
        mechanism_bundle: MechanismBundle,
        client: TelegramClientPort,
        action_executor: TelegramActionExecutor | None = None,
        progress_monitor: ProgressMonitor | None = None,
        liveness_policy: LivenessPolicy | None = None,
    ) -> None:
        self.storage = storage
        self.notifier = notifier
        self.settings = settings
        self.session_id: int | None = None
        self.stop_reason: str | None = None
        self._shutdown_task: asyncio.Task[None] | None = None
        self._shutdown_complete = False
        self._stop_state_saved = False
        self._stop_session_saved = False
        self._stop_event_saved = False
        self._run_task: asyncio.Task[None] | None = None
        self._run_session_active = False
        self._session_quiesced = asyncio.Event()
        self._session_quiesced.set()
        self._background_error: Exception | None = None
        self.task_scope = TaskScope(self._background_failed)

        self.client = client
        self._stop_requested = asyncio.Event()
        self._consumer_lock = asyncio.Lock()
        self._consumer_task: asyncio.Task[None] | None = None
        self._inflight_event: InboundEvent | None = None
        self._drain_task: asyncio.Task[None] | None = None
        self._mechanisms_close_task: asyncio.Task[None] | None = None
        self._stop_persist_task: asyncio.Task[None] | None = None
        self._mechanisms_initialized = False
        self._mechanism_view = MechanismView(
            MechanismSnapshot(
                phase_name=BotState.STARTING.name,
                position=None,
                location_name=None,
                current_hp=None,
                max_hp=None,
                active_target=None,
                total_progress_units=0,
                cycle_progress_units=0,
                liveness_phase=LivenessPhase.GENERAL,
                liveness_suspended=True,
            ),
            None,
        )
        self._final_mechanism_view: FinalMechanismView | None = None

        self.game_bot: object | None = None
        self.state = BotState.STARTING
        self.running = True

        self.delay_model = HumanDelayModel()
        self.activity_break_planner = ActivityBreakPlanner()

        self.watchdog = ProgressMonitor() if progress_monitor is None else progress_monitor
        self.liveness_policy = (
            LivenessPolicy(
                general_timeout=GENERAL_PROGRESS_TIMEOUT,
                discovery_timeout=MOVE_PROGRESS_TIMEOUT,
                target_timeout=TARGET_SELECTION_TIMEOUT,
                combat_timeout=COMBAT_PROGRESS_TIMEOUT,
                recovery_timeout=RECOVERY_WATCHDOG_TIMEOUT,
            )
            if liveness_policy is None
            else liveness_policy
        )
        self.watchdog_task: asyncio.Task[None] | None = None
        self.pause_requested = False
        self.current_cycle = 1
        self.rest_task: asyncio.Task[None] | None = None
        self.activity_break_task: asyncio.Task[None] | None = None
        self.progress_persist_task: asyncio.Task[None] | None = None
        self.pending_progress_reason: str | None = None
        self.intentional_waits = 0
        self.telegram_cooldown_until = 0.0
        self.telegram_cooldown_until_utc: datetime | None = None
        self.telegram_cooldown_reason: str | None = None
        self.telegram_cooldown_action: str | None = None
        self.telegram_cooldown_resume_mode = "reprocess"
        self.telegram_cooldown_task: asyncio.Task[None] | None = None
        self.telegram_cooldown_notified = False
        self.telegram_cooldown_changed = asyncio.Event()
        self.telegram_flood_incidents: deque[float] = deque()
        self.telegram_metrics_pending: dict[str, Counter[str]] = defaultdict(Counter)
        self.telegram_metrics_flush_task: asyncio.Task[None] | None = None
        self.telegram_metrics_flush_lock = asyncio.Lock()
        self.silent_stall_generation: int | None = None

        # RPC handles are kept outside immutable input data. Retain only queued,
        # currently processed, or latest-prompt messages, with exact object identity.
        self._event_messages: dict[int, InboundMessage] = {}
        self._message_events: dict[int, InboundEvent] = {}
        self._event_references: Counter[int] = Counter()
        self.worker_task: asyncio.Task[None] | None = None

        self.callback_timeout_count = 0
        self.action_executor = (
            TelegramActionExecutor(TELEGRAM_CALLBACK_RPC_TIMEOUT)
            if action_executor is None
            else action_executor
        )
        self.attempted_actions: BoundedKeyCache[ActionKey] = BoundedKeyCache(
            PROCESSED_EVENT_CACHE_SIZE
        )
        self.state_refresh_gate = StateRefreshGate()
        self.recovery_attempt_guard = RollingAttemptGuard(
            max_attempts=TELEGRAM_RECOVERY_LIMIT,
            window_seconds=TELEGRAM_RECOVERY_WINDOW,
        )
        self.telegram_action_limiter = TelegramActionLimiter(
            min_interval=TELEGRAM_ACTION_MIN_INTERVAL,
        )
        self.telegram_action_telemetry = TelegramActionTelemetry()
        services = MechanismServices(
            running=lambda: self.running,
            session_id=lambda: self.session_id,
            state_name=lambda: self._mechanism_phase_name,
            set_state_name=self._set_mechanism_state,
            pause_requested=lambda: self.pause_requested,
            is_current=self._is_current_event,
            telegram_cooldown_remaining=self.telegram_cooldown_remaining,
            log=self.log,
            mark_progress=self.mark_progress,
            activity_break_is_due=self.activity_break_is_due,
            stop=self.stop,
            enter_paused=self.enter_paused,
            complete_cycle=self.complete_cycle,
            start_activity_break=self.start_activity_break,
            pause_after_progress=self.pause_after_movement,
            request_current_state=self.request_current_state,
            send_game_message=self.send_game_message,
            click_button=self.click_event_button_outcome,
            start_task=lambda coroutine, name: self._start_background(
                coroutine, name=name
            ),
        )
        self._mechanism_bundle = mechanism_bundle
        self.mechanisms = require_mechanism_runtime(mechanism_bundle.build(services))
        self.input_policy = self.mechanisms.input_policy
        self.ingress = EventIngress(
            self.input_policy,
            capacity=EVENT_QUEUE_SIZE,
            registry_capacity=LATEST_MESSAGE_CACHE_SIZE,
        )

    def _start_background(
        self,
        coroutine: Coroutine[object, object, None],
        *,
        name: str,
    ) -> asyncio.Task[None]:
        return self.task_scope.create(coroutine, name=name)

    def activity_break_is_due(self, progress_units: int) -> bool:
        return self.activity_break_planner.is_due(
            progress_units,
            moves_min=ACTIVITY_BREAK_MOVES_MIN,
            moves_max=ACTIVITY_BREAK_MOVES_MAX,
            work_min=ACTIVITY_BREAK_WORK_MIN,
            work_max=ACTIVITY_BREAK_WORK_MAX,
        )

    async def pause_after_movement(self) -> None:
        if not self.running:
            return
        timing = self.settings.runtime_timing_policy()
        if not self.delay_model.should_take_long_pause(timing.long_pause_chance):
            return
        pause = self.delay_model.action_delay(
            timing.long_pause.minimum,
            timing.long_pause.maximum,
        )
        self.log(f"Короткая пауза после перемещения: {pause:.1f} сек.")
        await self.intentional_sleep(pause)

    def _background_failed(self, error: Exception) -> None:
        logger.error("Фоновая задача завершилась с ошибкой: %s", error)
        if not self.running:
            return
        self._background_error = error
        runner = self._run_task
        if runner is not None and not runner.done():
            runner.cancel()

    @property
    def state(self) -> BotState:
        return self._state

    @state.setter
    def state(self, value: BotState) -> None:
        if not isinstance(value, BotState):
            raise TypeError("Farmer application state must be a BotState")
        self._state = value
        self._mechanism_phase_name = value.name

    def _set_mechanism_state(self, name: str) -> None:
        name = require_phase_name(name)
        if self.state in _APPLICATION_PHASE_OVERRIDES:
            # Application lifecycle owns suspension and terminal states. A late
            # mechanism callback must not resume work behind the control plane.
            return
        self._mechanism_phase_name = name
        self._mechanism_view = MechanismView(
            replace(self._mechanism_view.snapshot, phase_name=name),
            self._mechanism_view.cycle,
        )
        legacy_state = BotState.__members__.get(name)
        if legacy_state is not None:
            self._state = legacy_state

    def _projected_phase_name(self, snapshot: MechanismSnapshot) -> str:
        if self.state in _APPLICATION_PHASE_OVERRIDES:
            return self.state.name
        return snapshot.phase_name

    def mechanism_view(self) -> MechanismView:
        """Return live state only inside the initialized runtime lifetime."""

        final = self._final_mechanism_view
        if final is not None:
            return final.status
        if not self._mechanisms_initialized:
            return self._mechanism_view
        view = MechanismView(
            self.mechanisms.snapshot(),
            self.mechanisms.cycle_descriptor(),
        )
        self._mechanism_view = view
        return view

    def _capture_final_mechanism_view(self) -> FinalMechanismView:
        final = self._final_mechanism_view
        if final is not None:
            return final
        status = self.mechanism_view()
        if self._mechanisms_initialized:
            report = self.mechanisms.format_session_report("ИТОГ ТЕКУЩЕЙ СЕССИИ")
            elapsed = self.mechanisms.session_elapsed_seconds()
        else:
            report = "ИТОГ ТЕКУЩЕЙ СЕССИИ"
            elapsed = 0
        final = FinalMechanismView(status, report, elapsed)
        self._final_mechanism_view = final
        return final

    def _is_current_event(self, event: InboundEvent) -> bool:
        return (
            self.resolve_input_event(event) is not None
            and self.ingress.is_current(event.prompt_token)
        )

    def start_cycle(self) -> CycleDescriptor:
        cycle = self.mechanisms.start_cycle(self.current_cycle)
        if not isinstance(cycle, CycleDescriptor):
            raise TypeError("Mechanism runtime returned an invalid cycle descriptor")
        self._mechanism_view = MechanismView(self._mechanism_view.snapshot, cycle)
        return cycle

    def _require_cycle(self) -> CycleDescriptor:
        cycle = self.mechanism_view().cycle
        if cycle is None:
            raise RuntimeError("No mechanism cycle has started")
        return cycle

    def log(self, text: str) -> None:
        logger.info("[%s] %s", self._mechanism_phase_name, text)

    def record_telegram_action(self, kind: str) -> None:
        snapshot = self.telegram_action_telemetry.record(kind)
        self.record_telegram_metric("outgoing_total")
        self.record_telegram_metric(
            "inline_callbacks" if kind == "inline_callback" else "map_requests"
        )
        last_minute = snapshot["last_minute"]
        last_ten_minutes = snapshot["last_ten_minutes"]
        self.record_telegram_metric_peak(
            "peak_actions_1m",
            last_minute if isinstance(last_minute, int) else 0,
        )
        self.record_telegram_metric_peak(
            "peak_actions_10m",
            last_ten_minutes if isinstance(last_ten_minutes, int) else 0,
        )
        total_value = snapshot["total"]
        total = total_value if isinstance(total_value, int) else 0
        if total % 10 == 0:
            self.log(
                "[TELEGRAM_IO] исходящих действий: "
                f"всего={total}, за 1 мин={snapshot['last_minute']}, "
                f"за 10 мин={snapshot['last_ten_minutes']}, "
                f"типы={snapshot['by_kind']}"
            )

    @staticmethod
    def telegram_metric_bucket() -> str:
        return datetime.now(UTC).replace(minute=0, second=0, microsecond=0).isoformat()

    def record_telegram_metric(self, metric: str, amount: int = 1) -> None:
        """Buffers diagnostics locally; it never slows or blocks Telegram actions."""
        if not hasattr(self, "telegram_metrics_pending"):
            return
        self.telegram_metrics_pending[self.telegram_metric_bucket()][metric] += amount
        task = self.telegram_metrics_flush_task
        if (task is None or task.done()) and self.running and not self.task_scope.closed:
            self.telegram_metrics_flush_task = self._start_background(
                self._flush_telegram_metrics_after_delay(),
                name="telegram-telemetry-flush",
            )

    def record_telegram_metric_peak(self, metric: str, value: int) -> None:
        """Keeps a high-water mark inside the current hourly buffer."""
        if not hasattr(self, "telegram_metrics_pending"):
            return
        bucket = self.telegram_metric_bucket()
        self.telegram_metrics_pending[bucket][metric] = max(
            self.telegram_metrics_pending[bucket][metric],
            value,
        )

    async def _flush_telegram_metrics_after_delay(self) -> None:
        try:
            await asyncio.sleep(15.0)
            await self.flush_telegram_metrics()
        except asyncio.CancelledError:
            return

    async def flush_telegram_metrics(self) -> None:
        async with self.telegram_metrics_flush_lock:
            pending = self.telegram_metrics_pending
            self.telegram_metrics_pending = defaultdict(Counter)
            pending_items = tuple(
                (bucket, dict(metrics)) for bucket, metrics in pending.items()
            )
            committed = 0
            try:
                for bucket, metrics in pending_items:
                    await self.storage.increment_telegram_activity(bucket, metrics)
                    committed += 1
            except BaseException:
                # Buckets confirmed by Storage must not be incremented again.
                # Preserve only the current/remaining suffix for a later retry.
                for bucket, metrics in pending_items[committed:]:
                    self.telegram_metrics_pending[bucket].update(metrics)
                raise

    def mark_progress(self, reason: str) -> None:
        self.watchdog.mark_progress(reason)
        self.pending_progress_reason = reason
        if (
            self.running
            and not self.task_scope.closed
            and (self.progress_persist_task is None or self.progress_persist_task.done())
        ):
            self.progress_persist_task = self._start_background(
                self._persist_progress(), name="persist-progress"
            )

    def _state_snapshot(
        self,
        reason: str,
        *,
        pause_requested: bool | None = None,
        mechanism_view: MechanismView | None = None,
    ) -> FarmerStatePatch:
        view = self.mechanism_view() if mechanism_view is None else mechanism_view
        snapshot = view.snapshot
        cycle = view.cycle
        position = snapshot.position
        game_state = self._projected_phase_name(snapshot)
        patch: FarmerStatePatch = {
            "game_state": game_state,
            "position_x": position[0] if position else None,
            "position_y": position[1] if position else None,
            "current_hp": snapshot.current_hp,
            "max_hp": snapshot.max_hp,
            "active_target": snapshot.active_target,
            "moves": snapshot.total_progress_units,
            "last_action": reason,
            "last_progress_at": utc_now(),
            "session_id": self.session_id,
            "current_cycle": self.current_cycle,
            "cycles_count": self.settings.run_policy().cycles_count,
            "moves_in_cycle": snapshot.cycle_progress_units,
            "pause_requested": int(
                self.pause_requested if pause_requested is None else pause_requested
            ),
        }
        if cycle is not None:
            patch["moves_per_cycle"] = cycle.target
        return patch

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

        self._mechanism_bundle.validate()
        self.mechanisms.validate()

    @property
    def latest_received_message(self) -> InboundMessage | None:
        latest = self.ingress.latest_prompt
        return self._event_messages.get(latest.sequence) if latest is not None else None

    def _input_message(self, message: GameMessage) -> InboundMessage | None:
        event = self._message_events.get(id(message))
        if event is None:
            return None
        inbound = self._event_messages.get(event.sequence)
        if inbound is None or (inbound is not message and inbound.rpc is not message):
            return None
        return inbound

    def resolve_input_event(self, event: InboundEvent) -> InboundMessage | None:
        inbound = self._event_messages.get(event.sequence)
        return inbound if inbound is not None and inbound.event is event else None

    def input_event(self, message: GameMessage) -> InboundEvent | None:
        inbound = self._input_message(message)
        return inbound.event if inbound is not None else None

    def is_latest_message(self, message: GameMessage) -> bool:
        event = self.input_event(message)
        return event is not None and self.ingress.is_current(event.prompt_token)

    def _prune_message_events(self) -> None:
        latest = self.ingress.latest_prompt
        latest_sequence = latest.sequence if latest is not None else None
        for sequence in tuple(self._event_messages):
            if sequence == latest_sequence or self._event_references.get(sequence, 0) > 0:
                continue
            inbound = self._event_messages.pop(sequence)
            for handle in (inbound, inbound.rpc):
                associated = self._message_events.get(id(handle))
                if associated is not None and associated.sequence == sequence:
                    self._message_events.pop(id(handle), None)

    async def enqueue_message(self, message: GameMessage) -> None:
        if not self.running:
            return
        result = await self.ingress.accept(MessageSnapshot.from_message(message))
        event = result.event
        if not result.accepted or event is None:
            return
        # No await between queue admission and registration: the consumer cannot
        # observe an accepted event without its corresponding RPC handle.
        inbound = InboundMessage(event, message)
        self._event_messages[event.sequence] = inbound
        self._message_events[id(message)] = event
        self._message_events[id(inbound)] = event
        self._event_references[event.sequence] += 1
        self._prune_message_events()
        self.record_telegram_metric("incoming_semantic_states")

    async def event_worker(self) -> None:
        await self._consume_accepted_events()
        self._check_ingress_shutdown()

    async def _consume_accepted_events(self) -> None:
        # One consumer owns a fact until its handler succeeds. A failed handler
        # keeps both the exact immutable event and RPC sidecar for cleanup retry.
        async with self._consumer_lock:
            self._consumer_task = asyncio.current_task()
            try:
                while True:
                    event = self._inflight_event
                    if event is None:
                        try:
                            event = await self.ingress.get()
                        except IngressClosedError:
                            return
                        self._inflight_event = event
                    message = self._event_messages.get(event.sequence)
                    if message is None:
                        raise RuntimeError("Accepted input has no corresponding RPC message")
                    await self.handle_message(message)
                    # No await between successful handling and acknowledgement.
                    self._inflight_event = None
                    self._event_references[event.sequence] -= 1
                    if self._event_references[event.sequence] <= 0:
                        self._event_references.pop(event.sequence, None)
                    self._prune_message_events()
                    self.ingress.task_done()
            finally:
                self._consumer_task = None

    def _check_ingress_shutdown(self) -> None:
        # Admission may have closed while the worker was awaiting input.
        if self.running:
            raise RuntimeError("Input stream closed while the farmer was running")

    def action_delay(
        self,
        *,
        delay_range: DelayRange,
        urgent: bool = False,
        remaining_seconds: int | None = None,
    ) -> float:
        if not isinstance(delay_range, DelayRange):
            raise ValueError("delay_range must be DelayRange")
        return self.delay_model.action_delay(
            delay_range.minimum,
            delay_range.maximum,
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

    def telegram_cooldown_remaining(self) -> float:
        return max(0.0, self.telegram_cooldown_until - time.monotonic())

    def telegram_safety_status(self) -> TelegramSafetyStatus:
        snapshot = self.telegram_action_telemetry.snapshot()
        return {
            "telegram_cooldown_remaining": int(self.telegram_cooldown_remaining()),
            "telegram_cooldown_until": (
                self.telegram_cooldown_until_utc.isoformat()
                if self.telegram_cooldown_until_utc is not None
                else None
            ),
            "telegram_cooldown_reason": self.telegram_cooldown_reason,
            "telegram_actions_1m": snapshot["last_minute"],
            "telegram_actions_10m": snapshot["last_ten_minutes"],
        }

    def _telegram_action_event_payload(self) -> dict[str, JsonValue]:
        snapshot = self.telegram_action_telemetry.snapshot()
        return {
            "total": snapshot["total"],
            "last_minute": snapshot["last_minute"],
            "last_ten_minutes": snapshot["last_ten_minutes"],
            "by_kind": dict(snapshot["by_kind"]),
        }

    async def restore_telegram_cooldown(self) -> None:
        raw_until = await self.storage.get_setting("telegram_cooldown_until")
        if not isinstance(raw_until, str):
            return
        try:
            until = datetime.fromisoformat(raw_until)
            if until.tzinfo is None:
                until = until.replace(tzinfo=UTC)
        except ValueError:
            await self.storage.delete_settings({"telegram_cooldown_until"})
            return
        remaining = (until - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            await self.storage.delete_settings(
                {"telegram_cooldown_until", "telegram_cooldown_reason"}
            )
            return
        reason = await self.storage.get_setting(
            "telegram_cooldown_reason", "восстановленная пауза Telegram"
        )
        if not str(reason).startswith("Telegram FLOOD_WAIT"):
            await self.storage.delete_settings(
                {"telegram_cooldown_until", "telegram_cooldown_reason"}
            )
            return
        self.telegram_cooldown_until = time.monotonic() + remaining
        self.telegram_cooldown_until_utc = until
        self.telegram_cooldown_reason = str(reason)
        self.telegram_cooldown_resume_mode = "refresh"
        self.telegram_cooldown_notified = False
        self.log(f"Восстановлена Telegram-пауза ещё на {remaining:.0f} сек.")
        self.telegram_cooldown_task = self._start_background(
            self.telegram_cooldown_loop(), name="telegram-cooldown"
        )

    async def start_telegram_cooldown(
        self,
        seconds: float,
        *,
        reason: str,
        action: str,
        resume_mode: str,
        event_type: str,
        payload: Mapping[str, JsonValue] | None = None,
    ) -> None:
        pause = max(1.0, seconds)
        deadline = time.monotonic() + pause
        until_utc = datetime.now(UTC) + timedelta(seconds=pause)
        if deadline >= self.telegram_cooldown_until:
            self.telegram_cooldown_until = deadline
            self.telegram_cooldown_until_utc = until_utc
            self.telegram_cooldown_reason = reason
            self.telegram_cooldown_action = action
        if resume_mode == "refresh":
            self.telegram_cooldown_resume_mode = "refresh"

        self.mark_progress(f"Telegram-пауза: {reason}")
        persisted_until = self.telegram_cooldown_until_utc or until_utc
        await self.storage.set_settings(
            {
                "telegram_cooldown_until": persisted_until.isoformat(),
                "telegram_cooldown_reason": self.telegram_cooldown_reason,
            }
        )
        event_payload: dict[str, JsonValue] = {
            "pause_seconds": pause,
            "action": action,
            "telegram_actions": self._telegram_action_event_payload(),
            "queue_size": self.ingress.qsize(),
        }
        if payload:
            event_payload.update(payload)
        await self.storage.add_event(
            event_type,
            f"{reason}; пауза {pause:.0f} сек.; действие: {action}",
            level="WARNING",
            payload=event_payload,
        )
        self.log(f"{reason}. Исходящие действия приостановлены на {pause:.0f} сек.")
        if not self.telegram_cooldown_notified:
            await self.notifier.send(
                "⏳ <b>Telegram-пауза</b>\n"
                f"Причина: {reason}\n"
                f"Пауза: {pause:.0f} сек.\n"
                "Входящие сообщения продолжают обрабатываться локально."
            )
            self.telegram_cooldown_notified = True
        if self.running and (
            self.telegram_cooldown_task is None or self.telegram_cooldown_task.done()
        ):
            self.telegram_cooldown_task = self._start_background(
                self.telegram_cooldown_loop(), name="telegram-cooldown"
            )
        self.telegram_cooldown_changed.set()

    async def telegram_cooldown_loop(self) -> None:
        try:
            while self.running:
                remaining = self.telegram_cooldown_remaining()
                if remaining <= 0:
                    break
                self.telegram_cooldown_changed.clear()
                try:
                    await asyncio.wait_for(
                        self.telegram_cooldown_changed.wait(),
                        timeout=remaining,
                    )
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            return
        if not self.running:
            return

        resume_mode = self.telegram_cooldown_resume_mode
        self.telegram_cooldown_until = 0.0
        self.telegram_cooldown_until_utc = None
        self.telegram_cooldown_reason = None
        self.telegram_cooldown_action = None
        self.telegram_cooldown_resume_mode = "reprocess"
        self.telegram_cooldown_task = None
        was_notified = self.telegram_cooldown_notified
        self.telegram_cooldown_notified = False
        await self.storage.delete_settings({"telegram_cooldown_until", "telegram_cooldown_reason"})
        await self.storage.add_event(
            "TELEGRAM_COOLDOWN_FINISHED",
            "Telegram-пауза завершена; работа продолжена по последнему состоянию",
        )
        self.log("Telegram-пауза завершена. Перепроверяю последнее состояние.")
        if was_notified:
            await self.notifier.send("▶️ <b>Telegram-пауза завершена</b>\nФармер продолжает работу.")

        if resume_mode == "refresh" or self.latest_received_message is None:
            await self.request_current_state(force=True)
            return
        self.enqueue_latest_for_reprocessing()

    def enqueue_latest_for_reprocessing(self) -> None:
        latest = self.ingress.latest_prompt
        if latest is None or latest.sequence not in self._event_messages:
            return
        if self.ingress.requeue_latest():
            self._event_references[latest.sequence] += 1

    def flood_wait_pause(self, server_seconds: int) -> tuple[float, int]:
        now = time.monotonic()
        cutoff = now - TELEGRAM_FLOOD_INCIDENT_WINDOW
        while self.telegram_flood_incidents and self.telegram_flood_incidents[0] <= cutoff:
            self.telegram_flood_incidents.popleft()
        self.telegram_flood_incidents.append(now)
        count = len(self.telegram_flood_incidents)
        return server_seconds + TELEGRAM_FLOOD_WAIT_BUFFER, count

    async def pause_for_flood_wait(
        self,
        server_seconds: int,
        action: str,
        *,
        resume_mode: str,
    ) -> None:
        self.record_telegram_metric("flood_waits")
        self.record_telegram_metric("flood_wait_seconds", server_seconds)
        pause, incident_count = self.flood_wait_pause(server_seconds)
        await self.start_telegram_cooldown(
            pause,
            reason=f"Telegram FLOOD_WAIT на {server_seconds} сек.",
            action=action,
            resume_mode=resume_mode,
            event_type="TELEGRAM_FLOOD_WAIT",
            payload={
                "server_seconds": server_seconds,
                "incident_count_10m": incident_count,
            },
        )

    async def record_silent_stall(self, reason: str) -> None:
        """Records a suspected silent restriction without changing the pace."""
        if self.silent_stall_generation == self.ingress.generation:
            return
        self.silent_stall_generation = self.ingress.generation
        self.record_telegram_metric("silent_stalls")
        await self.storage.add_event(
            "TELEGRAM_SILENT_STALL",
            "игра не отвечает на проверки; возможное тихое ограничение Telegram",
            level="WARNING",
            payload={
                "recovery_reason": reason,
                "inbound_generation": self.ingress.generation,
                "telegram_actions": self._telegram_action_event_payload(),
            },
        )
        self.log(
            "[TELEGRAM_DIAGNOSTIC] Зафиксирован признак тихого ограничения; "
            "искусственная пауза не вводится."
        )

    async def record_callback_timeout(self, description: str, detail: str) -> None:
        # The callback may already have reached the game. Never repeat the same
        # semantic action blindly. Record the uncertainty without changing pace.
        self.callback_timeout_count += 1
        self.record_telegram_metric("callback_timeouts")
        await self.storage.add_event(
            "TELEGRAM_CALLBACK_TIMEOUT",
            f"{detail}: результат inline-действия «{description}» неизвестен",
            level="WARNING",
            payload={
                "consecutive_timeouts": self.callback_timeout_count,
                "telegram_actions": self._telegram_action_event_payload(),
            },
        )
        self.log(
            f"[TELEGRAM_DIAGNOSTIC] {detail}; результат «{description}» неизвестен. "
            "Повтор этого действия заблокирован, искусственная пауза не вводится."
        )

    async def reserve_telegram_action_slot(self, action: str) -> bool:
        """Smooths an immediate burst without imposing a rolling-window pause."""
        limiter_delay = await self.telegram_action_limiter.acquire()
        if limiter_delay >= 0.05:
            self.log(
                f"Telegram-запрос «{action}» выровнен на {limiter_delay:.1f} сек. "
                "между соседними действиями."
            )
        return self.running

    async def press_button(
        self, message: GameMessage, row: int, column: int, description: str
    ) -> bool:
        outcome = await self.press_button_outcome(message, row, column, description)
        return outcome is ActionOutcome.SENT

    async def press_button_outcome(
        self,
        message: GameMessage,
        row: int,
        column: int,
        description: str,
    ) -> ActionOutcome:
        if self.telegram_cooldown_remaining() > 0:
            self.log(f"Действие отложено до завершения Telegram-паузы: {description}")
            return ActionOutcome.DEFERRED

        inbound = self._input_message(message)
        if inbound is None:
            return ActionOutcome.STALE
        message = inbound
        if not self.running:
            return ActionOutcome.DEFERRED
        if not self.is_latest_message(message):
            return ActionOutcome.STALE

        event = self.input_event(message)
        if event is None or event.action_key is None:
            return ActionOutcome.STALE
        # The game input policy supplies semantic identity; transport does not
        # need to recognize maps, combat turns, or keyboard-only revisions.
        action_key = event.action_key
        if action_key in self.attempted_actions:
            reason = (
                "игра не обновила состояние после inline-действия; "
                f"повтор «{description}» заблокирован"
            )
            await self.storage.add_event(
                "REPEATED_TELEGRAM_ACTION_BLOCKED",
                reason,
                level="WARNING",
            )
            self.log(reason)
            return ActionOutcome.DUPLICATE

        self.attempted_actions.remember(action_key)
        if not await self.reserve_telegram_action_slot(description):
            self.attempted_actions.discard(action_key)
            return ActionOutcome.DEFERRED

        if not self.running or self.telegram_cooldown_remaining() > 0:
            self.attempted_actions.discard(action_key)
            return ActionOutcome.DEFERRED
        if not self.is_latest_message(message):
            self.attempted_actions.discard(action_key)
            self.log(f"Отменено устаревшее действие: {description}")
            return ActionOutcome.STALE

        self.record_telegram_action("inline_callback")
        result = await self.action_executor.execute(inbound, row, column)
        if result.status is CallbackStatus.SENT:
            self.callback_timeout_count = 0
            self.record_telegram_metric("callback_successes")
            return ActionOutcome.SENT
        if result.status is CallbackStatus.FLOOD_WAIT:
            self.attempted_actions.discard(action_key)
            assert result.flood_wait_seconds is not None
            await self.pause_for_flood_wait(
                result.flood_wait_seconds, description, resume_mode="reprocess"
            )
            return ActionOutcome.DEFERRED
        if result.status is CallbackStatus.DELIVERY_UNKNOWN:
            await self.record_callback_timeout(
                description, f"{result.error_type}: {result.detail or 'доставка не подтверждена'}"
            )
            return ActionOutcome.DELIVERY_UNKNOWN
        self.record_telegram_metric("rpc_errors")
        self.log(
            f"Telegram не выполнил нажатие «{description}»: {result.error_type}: {result.detail}"
        )
        return ActionOutcome.REJECTED

    async def click_event_button_outcome(
        self,
        event: InboundEvent,
        *,
        description: str,
        delay_range: DelayRange,
        exact: str | None = None,
        contains: tuple[str, ...] = (),
        exclude: tuple[str, ...] = (),
        position: ButtonPosition | None = None,
        urgent: bool = False,
        remaining_seconds: int | None = None,
    ) -> ActionOutcome:
        inbound = self.resolve_input_event(event)
        if inbound is None:
            return ActionOutcome.STALE
        return await self.click_button_outcome(
            inbound,
            description=description,
            delay_range=delay_range,
            exact=exact,
            contains=contains,
            exclude=exclude,
            position=position,
            urgent=urgent,
            remaining_seconds=remaining_seconds,
        )

    async def click_button(
        self,
        message: GameMessage,
        *,
        description: str,
        delay_range: DelayRange,
        exact: str | None = None,
        contains: tuple[str, ...] = (),
        exclude: tuple[str, ...] = (),
        position: ButtonPosition | None = None,
        urgent: bool = False,
        remaining_seconds: int | None = None,
    ) -> bool:
        outcome = await self.click_button_outcome(
            message,
            description=description,
            exact=exact,
            contains=contains,
            exclude=exclude,
            position=position,
            urgent=urgent,
            remaining_seconds=remaining_seconds,
            delay_range=delay_range,
        )
        return outcome is ActionOutcome.SENT

    async def click_button_outcome(
        self,
        message: GameMessage,
        *,
        description: str,
        delay_range: DelayRange,
        exact: str | None = None,
        contains: tuple[str, ...] = (),
        exclude: tuple[str, ...] = (),
        position: ButtonPosition | None = None,
        urgent: bool = False,
        remaining_seconds: int | None = None,
    ) -> ActionOutcome:
        if not bool(self.running):
            return ActionOutcome.DEFERRED
        inbound = self._input_message(message)
        if inbound is None:
            return ActionOutcome.STALE
        message = inbound
        if self.telegram_cooldown_remaining() > 0:
            self.log(f"Исходящее действие подавлено Telegram-паузой: {description}")
            return ActionOutcome.DEFERRED
        delay = self.action_delay(
            delay_range=delay_range,
            urgent=urgent,
            remaining_seconds=remaining_seconds,
        )

        self.log(f"Ожидание {delay:.1f} сек. перед действием: {description}")
        await self.intentional_sleep(delay)

        if not self.running:
            return ActionOutcome.DEFERRED
        if not self.is_latest_message(message):
            self.log(f"Отменено устаревшее действие: {description}")
            return ActionOutcome.STALE

        if position is None:
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
            return ActionOutcome.REJECTED

        row, column = position
        self.log(f"Нажимаю: {description}")
        return await self.press_button_outcome(message, row, column, description)

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
        if not self.running:
            return
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
            cycle = self.start_cycle()
            self.activity_break_planner.reset()
            action = (
                f"передышка пропущена, начат цикл {self.current_cycle}; "
                f"цель — {cycle.target} {cycle.unit_label}"
            )
        elif self.state is BotState.ACTIVITY_BREAK:
            if self.activity_break_task:
                self.activity_break_task.cancel()
                self.activity_break_task = None
            progress = self.mechanisms.snapshot().cycle_progress_units
            self.activity_break_planner.complete(
                progress,
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
        snapshot = self.mechanisms.snapshot()
        cycle = self._require_cycle()
        await self.storage.update_state(
            process_status="RUNNING",
            game_state="STARTING",
            current_cycle=self.current_cycle,
            moves_in_cycle=snapshot.cycle_progress_units,
            moves_per_cycle=cycle.target,
            pause_requested=0,
            rest_until=None,
            last_action=action,
        )
        await self.storage.add_event("FARMER_RESUMED", action)
        await self.notifier.send("▶️ <b>Фарм продолжен</b>")
        await self.process_latest_state()
        return True, "Фарм продолжен с фактической текущей позиции."

    async def complete_cycle(self) -> None:
        if not self.running:
            return
        run_policy = self.settings.run_policy()
        runtime_timing = self.settings.runtime_timing_policy()
        total = run_policy.cycles_count
        if self.current_cycle >= total:
            await self.stop(f"завершены все циклы: {total}")
            return

        rest_seconds = random.uniform(
            runtime_timing.cycle_rest.minimum,
            runtime_timing.cycle_rest.maximum,
        )
        self.state = BotState.RESTING
        self.mark_progress(f"передышка после цикла {self.current_cycle}: {int(rest_seconds)} сек.")
        cycle = self._require_cycle()
        progress = self.mechanisms.snapshot().cycle_progress_units
        await self.storage.add_event(
            "CYCLE_COMPLETED",
            f"Завершён цикл {self.current_cycle} из {total}: "
            f"{progress} {cycle.unit_label} при цели {cycle.target}; "
            f"передышка {int(rest_seconds)} сек.",
        )
        await self.notifier.send(
            f"😴 Завершён цикл {self.current_cycle} из {total}\n"
            f"Прогресс: {progress} {cycle.unit_label}\n"
            f"Передышка: {int(rest_seconds // 60)} мин. {int(rest_seconds % 60)} сек."
        )
        self.rest_task = self._start_background(
            self.rest_between_cycles(rest_seconds), name="cycle-rest"
        )

    async def start_activity_break(self) -> None:
        if not self.running:
            return
        seconds = self.activity_break_planner.duration(
            ACTIVITY_BREAK_DURATION_MIN,
            ACTIVITY_BREAK_DURATION_MAX,
        )
        rest_until = datetime.now(UTC) + timedelta(seconds=seconds)
        self.state = BotState.ACTIVITY_BREAK
        self.mark_progress(f"длительный перерыв: {int(seconds)} сек.")
        await self.storage.update_state(rest_until=rest_until.isoformat())
        progress = self.mechanisms.snapshot().cycle_progress_units
        cycle = self._require_cycle()
        await self.storage.add_event(
            "ACTIVITY_BREAK_STARTED",
            f"Перерыв на {int(seconds)} сек. после {progress} {cycle.unit_label}",
        )
        self.log(
            f"Начат длительный перерыв на {seconds / 60:.1f} мин. "
            f"после {progress} {cycle.unit_label}."
        )
        self.activity_break_task = self._start_background(
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
        progress = self.mechanisms.snapshot().cycle_progress_units
        self.activity_break_planner.complete(
            progress,
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
            "Длительный перерыв завершён; запрошено одно свежее состояние",
        )
        self.log("Длительный перерыв завершён. Обновляю состояние один раз.")
        await self.process_latest_state()

    async def rest_between_cycles(self, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        if not self.running or self.state is BotState.PAUSED:
            return
        self.current_cycle += 1
        cycle = self.start_cycle()
        self.activity_break_planner.reset()
        self.state = BotState.STARTING
        self.mark_progress(
            f"начат цикл {self.current_cycle}; цель — {cycle.target} {cycle.unit_label}"
        )
        cycles_count = self.settings.run_policy().cycles_count
        await self.storage.add_event(
            "CYCLE_STARTED",
            f"Начат цикл {self.current_cycle} из {cycles_count}; "
            f"цель — {cycle.target} {cycle.unit_label}",
        )
        await self.notifier.send(
            f"▶️ <b>Начат цикл {self.current_cycle} "
            f"из {cycles_count}</b>\n"
            f"Цель цикла: {cycle.target} {cycle.unit_label}"
        )
        await self.process_latest_state()

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

    async def request_current_state(
        self, *, force: bool = False, recovery_reason: str | None = None
    ) -> bool:
        """Apply transport and recovery guarantees around every discovery implementation."""
        if not self.running or self.telegram_cooldown_remaining() > 0:
            return False
        if not self.ingress.empty():
            self.log("Запрос состояния отложен: входящее состояние ещё обрабатывается.")
            return False
        generation = self.ingress.generation
        if not self.state_refresh_gate.reserve(generation, force=force):
            return False

        sent = False
        progress_generation = self.watchdog.generation
        try:
            if (
                not self.running
                or self.telegram_cooldown_remaining() > 0
                or self.ingress.generation != generation
                or not self.ingress.empty()
            ):
                return False
            if recovery_reason is not None:
                if self.watchdog.recovery_attempts >= MAX_RECOVERY_ATTEMPTS:
                    await self.record_silent_stall(recovery_reason)
                    await self.stop(f"состояние игры не восстановлено: {recovery_reason}")
                    return False
                if not self.recovery_attempt_guard.can_attempt():
                    await self.record_silent_stall(recovery_reason)
                    return False
            # Once the raw effect starts, timeout/disconnect cannot prove non-delivery.
            # Keep the retry deadline even if no positive response is received.
            sent = True
            outcome = await asyncio.wait_for(
                self.mechanisms.request_state(), timeout=TELEGRAM_STATE_RPC_TIMEOUT
            )
            sent = outcome in {ActionOutcome.SENT, ActionOutcome.DELIVERY_UNKNOWN}
            return outcome is ActionOutcome.SENT
        except FloodWaitError as error:
            sent = False
            await self.pause_for_flood_wait(
                error.seconds, "запрос состояния", resume_mode="refresh"
            )
            return False
        except (OSError, RPCError) as error:
            if isinstance(error, RPCError):
                sent = False
            self.record_telegram_metric("rpc_errors")
            self.log(f"Запрос состояния не подтверждён: {type(error).__name__}: {error}")
            await self.storage.add_event(
                "STATE_REFRESH_FAILED", f"{type(error).__name__}: {error}", level="WARNING"
            )
            return False
        finally:
            if recovery_reason is not None and sent:
                self.recovery_attempt_guard.allow()
                self.record_telegram_metric("recovery_attempts")
                if (
                    self.running
                    and self.ingress.generation == generation
                    and self.watchdog.generation == progress_generation
                ):
                    attempt = self.watchdog.begin_recovery_attempt()
                    self.log(
                        f"Восстановление состояния ({attempt}/{MAX_RECOVERY_ATTEMPTS}): "
                        f"{recovery_reason}"
                    )
            self.state_refresh_gate.finish(sent=sent)

    async def send_game_message(self, text: str, action_label: str) -> ActionOutcome:
        """Safe Telegram effect; the application wrapper owns recovery and retry policy."""
        generation = self.ingress.generation
        if not self.running or self.telegram_cooldown_remaining() > 0 or not self.ingress.empty():
            return ActionOutcome.DEFERRED
        if not await self.reserve_telegram_action_slot(action_label):
            return ActionOutcome.DEFERRED
        if (
            not self.running
            or self.telegram_cooldown_remaining() > 0
            or self.ingress.generation != generation
            or not self.ingress.empty()
        ):
            return ActionOutcome.STALE
        self.record_telegram_action(action_label)
        await self.client.send_message(self.game_bot, text)
        return ActionOutcome.SENT

    async def recover_latest_state(self, reason: str) -> bool:
        return await self.request_current_state(recovery_reason=reason)

    def watchdog_diagnostic_payload(
        self,
        *,
        elapsed: float,
        timeout: float,
    ) -> dict[str, JsonValue]:
        snapshot = self.mechanisms.snapshot()
        latest_message = self.latest_received_message
        return {
            "state": self._projected_phase_name(snapshot),
            "elapsed_seconds": round(elapsed, 2),
            "timeout_seconds": round(timeout, 2),
            "last_progress_reason": self.watchdog.reason,
            "recovery_attempts_before": self.watchdog.recovery_attempts,
            "position": (
                list(snapshot.position) if snapshot.position is not None else None
            ),
            "hp": {
                "current": snapshot.current_hp,
                "maximum": snapshot.max_hp,
            },
            "active_target": snapshot.active_target,
            "mechanism": dict(self.mechanisms.diagnostics()),
            "event_queue_size": self.ingress.qsize(),
            "latest_message_id": (
                int(latest_message.id) if latest_message is not None else None
            ),
            "inbound_generation": self.ingress.generation,
            "telegram_actions": self._telegram_action_event_payload(),
        }

    async def watchdog_loop(self) -> None:
        while self.running:
            await asyncio.sleep(WATCHDOG_CHECK_INTERVAL)

            if await self.storage.get_setting("farmer_stop_requested", False):
                await self.storage.set_setting("farmer_stop_requested", False)
                await self.stop("остановлен командой из другого процесса")
                return

            mechanism_snapshot = self.mechanisms.snapshot()
            if (
                self.state in _APPLICATION_PHASE_OVERRIDES
                or mechanism_snapshot.liveness_suspended
            ):
                continue

            if self.telegram_cooldown_remaining() > 0:
                continue

            # A deliberate safety delay is not a stalled game state. Starting
            # recovery here would add a second request behind the limiter.
            if (
                self.intentional_waits
                or self.telegram_action_limiter.pending
                or time.monotonic() < self.telegram_cooldown_until
            ):
                continue

            timeout = self.liveness_policy.timeout_for(
                mechanism_snapshot.liveness_phase
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
                "watchdog: нет прогресса в состоянии "
                f"{self._projected_phase_name(mechanism_snapshot)}"
            )
            diagnostic["refresh_requested"] = refresh_requested
            diagnostic["recovery_attempts_after"] = self.watchdog.recovery_attempts
            await self.storage.add_event(
                "WATCHDOG_TRIGGERED",
                f"Нет прогресса в состоянии {diagnostic['state']}",
                level="INFO",
                payload=diagnostic,
            )

    async def handle_message(self, message: GameMessage) -> None:
        inbound = self._input_message(message)
        if inbound is None:
            return
        await self.mechanisms.handle(inbound.event)

    async def process_latest_state(self) -> None:
        # Startup and resume always request one fresh authoritative game state.
        self.mark_progress("при запуске запрошено свежее состояние")
        await self.request_current_state()

    def _background_tasks(self) -> list[asyncio.Task[None]]:
        return [task for task in self.task_scope.snapshot() if not task.done()]

    @property
    def shutdown_complete(self) -> bool:
        return self._shutdown_complete and not self.task_scope.snapshot()

    def owns_run_task(self, task: asyncio.Task[None]) -> bool:
        """Whether *task* is currently executing run(), including its finalizer."""
        return self._run_task is task

    async def _cancel_background_tasks(self, *, exclude: asyncio.Task[None] | None = None) -> None:
        await self.task_scope.cancel_and_wait(SHUTDOWN_STEP_TIMEOUT, exclude=exclude)

    async def _persist_stop(self, reason: str) -> None:
        final = self._final_mechanism_view
        if final is None:
            raise RuntimeError("Final mechanism view was not captured before persistence")
        if not self._stop_state_saved:
            await self.storage.update_state(
                **self._state_snapshot(
                    reason,
                    pause_requested=False,
                    mechanism_view=final.status,
                )
            )
            self._stop_state_saved = True
            logger.info("\n%s", final.session_report)
            logger.info("Причина остановки: %s", reason)
        if not self._stop_session_saved:
            await self.storage.finish_session(
                self.session_id,
                reason,
                final.session_elapsed_seconds,
            )
            self._stop_session_saved = True
        if not self._stop_event_saved:
            await self.storage.add_event(
                "FARMER_STOPPED",
                reason,
                payload={"telegram_actions": self._telegram_action_event_payload()},
            )
            self._stop_event_saved = True
        await self.flush_telegram_metrics()
        await self.storage.checkpoint()

    @staticmethod
    def _consume_shutdown_result(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    @staticmethod
    def _needs_retry(task: asyncio.Task[None] | None) -> bool:
        return task is None or (
            task.done() and (task.cancelled() or task.exception() is not None)
        )

    def _create_shutdown_task(
        self, coroutine: Coroutine[object, object, None], *, name: str
    ) -> asyncio.Task[None]:
        try:
            task = asyncio.create_task(coroutine, name=name)
        except BaseException:
            coroutine.close()
            raise
        task.add_done_callback(self._consume_shutdown_result)
        return task

    async def _await_shutdown_step(self, task: asyncio.Task[None]) -> None:
        # A timed-out operation remains owned. A later stop joins this same task;
        # cancelling it could lose a committed fact or interrupt resource cleanup.
        done, _ = await asyncio.wait({task}, timeout=SHUTDOWN_STEP_TIMEOUT)
        if not done:
            raise TimeoutError(f"Shutdown step is still running: {task.get_name()}")
        task.result()

    async def _drain_accepted_events(self) -> None:
        await self._consume_accepted_events()
        await self.ingress.join()

    async def _shutdown(self, reason: str) -> None:
        # Startup can still own mechanism initialization or a connect RPC.
        # Its finalizer signals quiescence before joining this coordinator.
        async with asyncio.timeout(SHUTDOWN_STEP_TIMEOUT):
            await self._session_quiesced.wait()
        if self._needs_retry(self._drain_task):
            self._drain_task = self._create_shutdown_task(
                self._drain_accepted_events(), name="farmer-drain"
            )
        assert self._drain_task is not None
        await self._await_shutdown_step(self._drain_task)
        # Mechanism timers and the durable outbox use the same scope. They must
        # stop before mechanism close/persistence can declare a stable snapshot.
        await self._cancel_background_tasks()
        self._capture_final_mechanism_view()
        if self._needs_retry(self._mechanisms_close_task):
            self._mechanisms_close_task = self._create_shutdown_task(
                self.mechanisms.aclose(), name="farmer-mechanisms-close"
            )
        assert self._mechanisms_close_task is not None
        await self._await_shutdown_step(self._mechanisms_close_task)
        self.state = BotState.STOPPED
        self.pending_progress_reason = None
        if self._needs_retry(self._stop_persist_task):
            self._stop_persist_task = self._create_shutdown_task(
                self._persist_stop(reason), name="farmer-final-persistence"
            )
        assert self._stop_persist_task is not None
        await self._await_shutdown_step(self._stop_persist_task)
        self._shutdown_complete = True

    async def stop(self, reason: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = reason
        self.running = False
        self.ingress.close()
        self.task_scope.close()
        self._stop_requested.set()
        current = asyncio.current_task()
        runner = self._run_task
        if (
            runner is not None
            and runner is not current
            and self._run_session_active
            and not runner.done()
            and runner.cancelling() == 0
        ):
            runner.cancel()
        if self._needs_retry(self._shutdown_task):
            self._shutdown_task = self._create_shutdown_task(
                self._shutdown(self.stop_reason), name="farmer-shutdown"
            )
        shutdown_task = self._shutdown_task
        assert shutdown_task is not None
        if (
            current is self._consumer_task
            or current in self.task_scope.snapshot()
            or (current is self._run_task and self._run_session_active)
        ):
            # A worker requests shutdown; it must return to let the coordinator
            # drain/ack its current fact and join this worker without a cycle.
            return
        cancelled = False
        while True:
            try:
                await asyncio.shield(shutdown_task)
                break
            except asyncio.CancelledError:
                if shutdown_task.cancelled():
                    raise
                cancelled = True
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise
        if cancelled:
            raise asyncio.CancelledError

    async def _watch_transport(self) -> None:
        # Telethon.run_until_disconnected() disconnects in its own finally.
        # Observe the transport future without borrowing that ownership behavior.
        await asyncio.shield(self.client.disconnected)
        await self.stop("Telegram-соединение завершено")

    async def run(self) -> None:
        self._run_task = asyncio.current_task()
        self._run_session_active = True
        self._session_quiesced.clear()
        reason = "Telegram-соединение завершено"
        try:
            await self._run_session()
        except asyncio.CancelledError:
            if self._background_error is not None:
                error = self._background_error
                reason = f"ошибка фоновой задачи: {type(error).__name__}: {error}"
                raise error from None
            if self._stop_requested.is_set():
                return
            reason = "задача фармера отменена"
            raise
        except Exception as error:
            reason = f"аварийное завершение: {type(error).__name__}: {error}"
            raise
        finally:
            self._run_session_active = False
            self._session_quiesced.set()
            try:
                await self.stop(self.stop_reason or reason)
            finally:
                self._run_task = None

    async def _run_session(self) -> None:
        self.validate_config()
        await self.client.connect()
        # Observe disconnects before any startup RPC can block. The watcher owns
        # no transport resource; it only asks the Farmer lifecycle to unwind.
        self._start_background(
            self._watch_transport(), name="telegram-transport-watch"
        )
        if not await self.client.is_user_authorized():
            raise RuntimeError(
                "Telethon-сессия не авторизована. Выполните python authorize.py "
                "в интерактивном терминале с тем же FOG_DATA_DIR."
            )
        await self.mechanisms.initialize()
        self._mechanisms_initialized = True
        self.mechanism_view()
        if not self.running:
            return
        deleted_logs = self.cleanup_old_log_files()
        cleanup = await self.storage.cleanup_old_data(
            DATA_RETENTION_DAYS,
            event_types_to_delete=("LOW_HP_WAIT_STARTED", "LOW_HP_WAIT_FINISHED"),
        )
        compacted = await self.storage.compact_if_needed()
        logger.info(
            "Очистка хранения: срок %s дн.; events=%s, "
            "battles=%s, drops=%s, sessions=%s, logs=%s, compacted=%s",
            DATA_RETENTION_DAYS,
            cleanup["events"],
            cleanup["battles"],
            cleanup["drops"],
            cleanup["sessions"],
            deleted_logs,
            compacted,
        )
        run_policy = self.settings.run_policy()
        cycle = self.start_cycle()
        self.session_id = await self.storage.start_session(
            cycles_count=run_policy.cycles_count,
            moves_per_cycle=cycle.target,
        )
        await self.storage.add_event(
            "FARMER_STARTED",
            f"Фармер запущен: {run_policy.cycles_count} цикл(а), "
            f"диапазон {cycle.minimum}–{cycle.maximum} {cycle.unit_label}; "
            f"первый цикл — {cycle.target} {cycle.unit_label}",
        )
        await self.notifier.send(
            "▶️ Фармер запущен\n"
            f"Циклов: {run_policy.cycles_count}\n"
            f"Диапазон: {cycle.minimum}–{cycle.maximum} {cycle.unit_label}\n"
            f"Первый цикл: {cycle.target} {cycle.unit_label}"
        )

        # Uses the Telethon entity cache and avoids fetching the full entity on
        # every restart once the peer is known to the session.
        self.game_bot = await self.client.get_input_entity(GAME_BOT)

        logger.info("=" * 72)
        logger.info("Farmer запущен")
        logger.info("Telegram-сессия подключена")
        logger.info(
            "Telegram: наблюдение без локального бюджета и автозамедления; "
            "дополнительный интервал %.1f сек.",
            TELEGRAM_ACTION_MIN_INTERVAL,
        )
        logger.info(
            "Прогресс цикла: диапазон %s–%s %s; текущая цель — %s.",
            cycle.minimum,
            cycle.maximum,
            cycle.unit_label,
            cycle.target,
        )
        logger.info(
            "Watchdog: поиск %s сек., бой %s сек.",
            MOVE_PROGRESS_TIMEOUT,
            COMBAT_PROGRESS_TIMEOUT,
        )
        logger.info(
            "Полный журнал: %s/%s",
            LOG_DIRECTORY,
            LOG_FILENAME,
        )
        logger.info("=" * 72)

        async def accept_game_message(event: events.NewMessage.Event, metric: str) -> None:
            if event.message.out:
                return
            self.record_telegram_metric(metric)
            await self.enqueue_message(cast(GameMessage, event.message))

        async def on_game_message(event: events.NewMessage.Event) -> None:
            await accept_game_message(event, "incoming_new_messages")

        async def on_game_message_edit(event: events.MessageEdited.Event) -> None:
            await accept_game_message(event, "incoming_message_edits")

        self.client.add_event_handler(on_game_message, events.NewMessage(chats=self.game_bot))
        self.client.add_event_handler(
            on_game_message_edit, events.MessageEdited(chats=self.game_bot)
        )
        self.worker_task = self._start_background(self.event_worker(), name="game-events")
        self.watchdog_task = self._start_background(self.watchdog_loop(), name="progress-watchdog")

        await self.restore_telegram_cooldown()
        await self.process_latest_state()
        await self._stop_requested.wait()
