from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Mapping

from battle_notification_outbox import BattleNotificationOutbox
from combat import CombatEventKind, CombatStatus
from config import (
    CHARACTER_NAME,
    DEATH_RECOVERY_MAX_WAIT,
    DEATH_RECOVERY_MIN_WAIT,
    GAME_BOT,
    MAP_MAX_X,
    MAP_MAX_Y,
    MAP_MIN_X,
    MAP_MIN_Y,
    MIN_HP_AFTER_DEATH,
)
from discovery import DiscoveryEventKind
from event_cache import BoundedKeyCache
from farm_statistics import FarmStatistics, format_report
from farmer_combat_runtime import FarmerCombatRuntime
from farmer_map_runtime import FarmerMapRuntime
from fog_input import FoGInputPolicy
from game_input import ActionOutcome, InboundEvent, InputPolicy
from game_mechanisms import (
    CycleDescriptor,
    MechanismBundle,
    MechanismRuntime,
    MechanismServices,
    MechanismSnapshot,
)
from json_types import JsonValue
from legacy_combat_controller import LegacyCombatController
from legacy_combat_diagnostics import LegacyCombatDiagnostics
from legacy_liveness import liveness_is_suspended, liveness_phase
from legacy_map_controller import LegacyMapController
from message_snapshot import canonical_source_scope
from models import BotState, RuntimeContext
from notifications import Notifier
from parser import extract_player_hp, is_passive_health_notification
from settings_service import SettingsService
from skills import enough_health_for_battle
from storage import Storage
from telegram_safety import MessageFactKey, semantic_message_text

logger = logging.getLogger("fog_farmer")
PROCESSED_EVENT_CACHE_SIZE = 500


def _legacy_fog_source_scope(game_bot: str) -> str:
    if type(game_bot) is not str:
        raise ValueError("GAME_BOT must be a string")
    username = game_bot.strip()
    if not username.startswith("@") or len(username) == 1:
        raise ValueError("GAME_BOT должен начинаться с @ и содержать имя.")
    return canonical_source_scope(f"telegram:bot:{username[1:].casefold()}")


class ManagedLegacyCombatController:
    """Own current combat diagnostics together with the disposable controller."""

    def __init__(
        self,
        controller: LegacyCombatController,
        diagnostics: LegacyCombatDiagnostics,
    ) -> None:
        self._controller = controller
        self._diagnostics = diagnostics

    @property
    def legacy_controller(self) -> LegacyCombatController:
        return self._controller

    @property
    def diagnostics(self) -> LegacyCombatDiagnostics:
        return self._diagnostics

    async def initialize(self) -> None:
        await self._diagnostics.initialize()
        await self._controller.initialize()
        learning_rows = await self._diagnostics.backfill()
        deleted_decisions = await self._diagnostics.cleanup()
        if learning_rows:
            logger.info(
                "Подготовлены профильные итоги прошлых боёв: %s.",
                learning_rows,
            )
        if deleted_decisions:
            logger.info(
                "Удалено устаревших legacy-решений боя: %s.",
                deleted_decisions,
            )

    async def persist(self) -> None:
        await self._controller.persist()

    def reset(self) -> None:
        self._controller.reset()

    def status(self) -> CombatStatus:
        return self._controller.status()


class LegacyFoGMechanismRuntime:
    """One replaceable unit containing all current FoG map/combat orchestration."""

    def __init__(
        self,
        services: MechanismServices,
        settings: SettingsService,
        storage: Storage,
        notifier: Notifier,
    ) -> None:
        self._services = services
        self._settings = settings
        self._storage = storage
        self._notifier = notifier
        self.battle_notifications = BattleNotificationOutbox(
            storage, notifier, delivery_allowed=services.running
        )
        self.battle_notification_task: asyncio.Task[None] | None = None
        self.context = RuntimeContext()
        self.statistics = FarmStatistics()
        self.moves_in_cycle = 0
        self._cycle: CycleDescriptor | None = None
        self.recovery_started_at: float | None = None
        self.recovery_refresh_requested = False
        self.recovery_task: asyncio.Task[None] | None = None
        self._observed_events: BoundedKeyCache[MessageFactKey] = BoundedKeyCache(
            PROCESSED_EVENT_CACHE_SIZE
        )
        self._last_observed_prompt: MessageFactKey | None = None
        self._initialized = False
        self._combat_initialized = False
        self._closed = False
        self._lifecycle_lock = asyncio.Lock()

        self._input_policy = FoGInputPolicy(
            character_name=CHARACTER_NAME,
            enabled_targets=lambda: self._settings.target_policy().enabled,
        )
        diagnostics = LegacyCombatDiagnostics(storage)
        map_runtime = FarmerMapRuntime(
            services,
            self,
            storage,
            legacy_map_policy=self._settings.legacy_map_policy,
            target_policy=self._settings.target_policy,
            recovery_minimum_wait=DEATH_RECOVERY_MIN_WAIT,
            recovery_minimum_hp=MIN_HP_AFTER_DEATH,
        )
        self.discovery = LegacyMapController(
            map_runtime,
            character_name=CHARACTER_NAME,
            min_x=MAP_MIN_X,
            max_x=MAP_MAX_X,
            min_y=MAP_MIN_Y,
            max_y=MAP_MAX_Y,
        )
        combat_runtime = FarmerCombatRuntime(
            services,
            self.context,
            diagnostics,
            storage,
            self.statistics,
            self,
            source_scope=_legacy_fog_source_scope(GAME_BOT),
            legacy_combat_policy=self._settings.legacy_combat_policy,
            target_policy=self._settings.target_policy,
            add_treatment_target=self._settings.add_treatment_enemy_target,
            remove_treatment_target=self._settings.remove_treatment_enemy_target,
            wake_battle_notifications=self.battle_notifications.wake,
        )
        controller = LegacyCombatController(combat_runtime, character_name=CHARACTER_NAME)
        self.combat = ManagedLegacyCombatController(controller, diagnostics)

    @property
    def input_policy(self) -> InputPolicy:
        return self._input_policy

    @property
    def cycle_target(self) -> int:
        cycle = self._cycle
        if cycle is None:
            raise RuntimeError("No FoG cycle has started")
        return cycle.target

    @property
    def state(self) -> BotState:
        name = self._services.state_name()
        try:
            return BotState[name]
        except KeyError as error:
            raise RuntimeError(f"Unknown legacy FoG state: {name}") from error

    def _set_state(self, state: BotState) -> None:
        self._services.set_state_name(state.name)

    def validate(self) -> None:
        if not isinstance(CHARACTER_NAME, str) or not CHARACTER_NAME.strip():
            raise ValueError("CHARACTER_NAME не заполнен.")
        if not self._settings.target_policy().enabled:
            raise ValueError("Нужно выбрать хотя бы одного моба.")
        self._settings.legacy_map_policy()
        self._settings.legacy_combat_policy()

    async def initialize(self) -> None:
        async with self._lifecycle_lock:
            if self._initialized:
                return
            if self._closed:
                raise RuntimeError("FoG mechanisms are already closed")
            try:
                await self.discovery.initialize()
                self._combat_initialized = True
                await self.combat.initialize()
                self._initialized = True
                self.battle_notification_task = self._services.start_task(
                    self.battle_notifications.run(), "battle-card-notifications"
                )
            except BaseException as error:
                try:
                    await self._close_unlocked()
                except BaseException as close_error:
                    error.add_note(
                        "FoG mechanism rollback also failed: "
                        f"{type(close_error).__name__}: {close_error}"
                    )
                raise

    def start_cycle(self, cycle_number: int) -> CycleDescriptor:
        if type(cycle_number) is not int or cycle_number < 1:
            raise ValueError("Cycle number must be a positive integer")
        if not self._initialized:
            raise RuntimeError("FoG mechanisms must be initialized before starting a cycle")
        if self._cycle is not None:
            self.discovery.reset_cycle()
            self.combat.reset()
        moves = self._settings.legacy_map_policy().moves_per_cycle
        cycle = CycleDescriptor(
            target=random.randint(moves.minimum, moves.maximum),
            minimum=moves.minimum,
            maximum=moves.maximum,
            unit_label="перемещений",
        )
        self.moves_in_cycle = 0
        self._cycle = cycle
        return cycle

    def cycle_descriptor(self) -> CycleDescriptor | None:
        return self._cycle

    def _update_hp(self, text: str) -> bool:
        hp = extract_player_hp(text, CHARACTER_NAME)
        if hp is None:
            return False
        current_hp, max_hp = hp
        changed = current_hp != self.context.current_hp or max_hp != self.context.max_hp
        if changed:
            self.context.current_hp = current_hp
            self.context.max_hp = max_hp
            self._services.log(f"Здоровье обновлено: {current_hp}/{max_hp}")
        return changed

    def has_battle_health(self) -> bool:
        return enough_health_for_battle(
            self.context.current_hp,
            self.context.max_hp,
            self._settings.legacy_combat_policy().battle_start_hp_percent,
        )

    def battle_health_is_low(self) -> bool:
        return (
            self.context.current_hp is not None
            and self.context.max_hp is not None
            and self.context.max_hp > 0
            and not self.has_battle_health()
        )

    def wait_for_battle_health(self) -> None:
        if self.state is BotState.WAITING_FOR_HEALTH:
            return
        self._set_state(BotState.WAITING_FOR_HEALTH)
        self._services.mark_progress("ожидание восстановления HP перед боем")

    async def handle(self, event: InboundEvent) -> bool:
        if not isinstance(event, InboundEvent):
            raise ValueError("FoG mechanisms require an immutable InboundEvent")
        text = event.snapshot.raw_text
        observation = self.discovery.observe_message(event)
        discovery_kind = observation.kind if observation is not None else None
        combat_observation = (
            self.combat.legacy_controller.observe_message(event)
            if observation is None
            else None
        )
        fact_key = MessageFactKey(event.snapshot.id, semantic_message_text(text))
        first_observation = (
            fact_key != self._last_observed_prompt
            if discovery_kind is DiscoveryEventKind.STATE
            else self._observed_events.remember(fact_key)
        )
        passive_health = is_passive_health_notification(text)
        if not passive_health:
            self._last_observed_prompt = fact_key
        hp_changed = False
        if first_observation and (
            combat_observation is None or combat_observation.authoritative
        ):
            hp_changed = self._update_hp(text)

        if combat_observation is not None:
            await self.combat.legacy_controller.handle_message(combat_observation)
            if combat_observation.kind is not CombatEventKind.UPDATE:
                return True
        if (
            discovery_kind
            not in {DiscoveryEventKind.STATE, DiscoveryEventKind.PROGRESS_CONFIRMED}
            and not passive_health
            and not self._services.is_current(event)
        ):
            return combat_observation is not None
        if (
            self.state is BotState.RECOVERY
            and hp_changed
            and discovery_kind is not DiscoveryEventKind.STATE
        ):
            await self.maybe_request_recovery_state()
        if self.state is BotState.RECOVERY and observation is None:
            return combat_observation is not None
        if self.state is BotState.WAITING_FOR_HEALTH:
            if discovery_kind is not DiscoveryEventKind.TARGET_SELECTION:
                if not self.has_battle_health():
                    return observation is not None or combat_observation is not None
                self._set_state(BotState.MAP)
                self._services.mark_progress("HP восстановлено для новых боёв")
                if discovery_kind is not DiscoveryEventKind.STATE:
                    await self._services.request_current_state()
                    return True
        if observation is not None:
            return await self.discovery.handle_message(observation)
        return combat_observation is not None or hp_changed

    async def request_state(self) -> ActionOutcome:
        return await self.discovery.request_state()

    def snapshot(self) -> MechanismSnapshot:
        state = self.state
        return MechanismSnapshot(
            phase_name=state.name,
            position=self.context.current_position,
            location_name=self.discovery.status().location_name,
            current_hp=self.context.current_hp,
            max_hp=self.context.max_hp,
            active_target=self.context.active_target,
            total_progress_units=self.context.move_count,
            cycle_progress_units=self.moves_in_cycle,
            liveness_phase=liveness_phase(state),
            liveness_suspended=liveness_is_suspended(state),
        )

    def diagnostics(self) -> Mapping[str, JsonValue]:
        pending_move = self.context.pending_move
        combat = self.combat.status()
        return {
            "location_name": self.discovery.status().location_name,
            "checked_empty_position": (
                list(self.context.checked_empty_position)
                if self.context.checked_empty_position is not None
                else None
            ),
            "failed_move_attempts": self.context.failed_move_attempts,
            "battle_target": self.context.battle_target,
            "combat_enemies": list(self.context.combat_enemies),
            "combat_pending_skill": combat.pending_action_label,
            "pending_move": (
                {
                    "origin": list(pending_move.origin),
                    "destination": list(pending_move.destination),
                    "button": pending_move.button,
                }
                if pending_move is not None
                else None
            ),
        }

    def session_elapsed_seconds(self) -> int:
        return self.statistics.elapsed_seconds()

    def format_session_report(self, title: str) -> str:
        return format_report(title, self.statistics.session_report())

    def recovery_elapsed(self) -> float:
        started_at = self.recovery_started_at
        return 0.0 if started_at is None else max(0.0, time.monotonic() - started_at)

    async def finish_health_recovery(
        self, current_hp: int, max_hp: int | None
    ) -> None:
        self.recovery_started_at = None
        self.recovery_refresh_requested = False
        self._services.mark_progress("здоровье восстановлено")
        await self._storage.add_event(
            "RECOVERY_FINISHED",
            f"HP восстановлено до {current_hp}/{max_hp}",
        )
        await self._notifier.send(
            f"✅ Здоровье восстановлено\nHP: {current_hp}/{max_hp}\nФарм продолжен."
        )
        task = self.recovery_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        self.recovery_task = None

    async def begin_death_recovery(self, target_name: str) -> None:
        await self._storage.add_event(
            "PLAYER_DEFEATED",
            f"Поражение от {target_name}; ожидание восстановления HP",
            level="WARNING",
        )
        if not self._services.running():
            return
        self._set_state(BotState.RECOVERY)
        self.recovery_started_at = time.monotonic()
        self.recovery_refresh_requested = False
        await self._notifier.send(
            f"☠️ Персонаж погиб\nЦель: {target_name}\nНачато восстановление здоровья."
        )
        self._services.mark_progress("начато восстановление после смерти")
        if self.recovery_task is not None:
            self.recovery_task.cancel()
        self.recovery_task = self._services.start_task(
            self._death_recovery_loop(), "death-recovery"
        )

    async def _death_recovery_loop(self) -> None:
        await asyncio.sleep(DEATH_RECOVERY_MIN_WAIT)
        if not self._services.running() or self.state is not BotState.RECOVERY:
            return
        await self.maybe_request_recovery_state()
        remaining = max(0, DEATH_RECOVERY_MAX_WAIT - DEATH_RECOVERY_MIN_WAIT)
        await asyncio.sleep(remaining)
        if self._services.running() and self.state is BotState.RECOVERY:
            await self._services.stop("HP не восстановилось за предельное время")

    async def maybe_request_recovery_state(self) -> bool:
        if self.state is not BotState.RECOVERY or self.recovery_refresh_requested:
            return False
        elapsed = self.recovery_elapsed()
        current_hp = self.context.current_hp or 0
        if elapsed < DEATH_RECOVERY_MIN_WAIT or current_hp < MIN_HP_AFTER_DEATH:
            return False
        self.recovery_refresh_requested = True
        self._services.log(
            f"HP восстановлено до {current_hp}; запрашиваю состояние один раз."
        )
        requested = await self._services.request_current_state()
        if not requested and self.state is BotState.RECOVERY:
            self.recovery_refresh_requested = False
        return requested

    async def recover_latest_state(self, reason: str) -> bool:
        return await self._services.request_current_state(
            force=True,
            recovery_reason=reason,
        )

    async def _close_unlocked(self) -> None:
        if self._closed:
            return
        notification_task = self.battle_notification_task
        if (
            notification_task is not None
            and notification_task is not asyncio.current_task()
            and not notification_task.done()
        ):
            notification_task.cancel()
            try:
                await notification_task
            except asyncio.CancelledError:
                pass
        self.battle_notification_task = None
        recovery_task = self.recovery_task
        if recovery_task is not None and recovery_task is not asyncio.current_task():
            recovery_task.cancel()
            try:
                await recovery_task
            except asyncio.CancelledError:
                pass
        self.recovery_task = None
        if self._combat_initialized:
            await self.combat.persist()
        self._closed = True
        self._initialized = False

    async def aclose(self) -> None:
        async with self._lifecycle_lock:
            await self._close_unlocked()


class LegacyFoGMechanismBundle:
    """Composition root for the current map and HP/mana/skill game version."""

    def __init__(
        self,
        settings: SettingsService,
        storage: Storage,
        notifier: Notifier,
    ) -> None:
        self._settings = settings
        self._storage = storage
        self._notifier = notifier

    def validate(self) -> None:
        if not isinstance(CHARACTER_NAME, str) or not CHARACTER_NAME.strip():
            raise ValueError("CHARACTER_NAME не заполнен.")
        if not self._settings.target_policy().enabled:
            raise ValueError("Нужно выбрать хотя бы одного моба.")
        self._settings.legacy_map_policy()
        self._settings.legacy_combat_policy()

    def build(self, services: MechanismServices) -> MechanismRuntime:
        runtime = LegacyFoGMechanismRuntime(
            services,
            self._settings,
            self._storage,
            self._notifier,
        )
        runtime.validate()
        return runtime


def default_legacy_fog_bundle(
    settings: SettingsService,
    storage: Storage,
    notifier: Notifier,
) -> MechanismBundle:
    return LegacyFoGMechanismBundle(settings, storage, notifier)
