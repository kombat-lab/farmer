from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol

from automation_policy import LegacyCombatPolicy, TargetPolicy
from battle_notification_outbox import events_for
from battle_records import BattleOutcome, RecordBattleResult, SourceEventId
from combat import CombatAction, CombatActionKind, CombatObservation
from combat_strategy import CombatDecisionTrace
from farm_statistics import FarmStatistics, format_report
from game_input import ActionOutcome, InboundEvent
from game_mechanisms import MechanismServices
from json_types import JsonValue
from legacy_combat_diagnostics import LegacyCombatDiagnostics
from message_snapshot import (
    MessageSnapshot,
    canonical_source_scope,
    derive_source_event_id,
    legacy_v4_message_source_event_id,
)
from models import RuntimeContext
from rewards import BattleReward

logger = logging.getLogger("fog_farmer")


class LegacyCombatStore(Protocol):
    async def load_combat_knowledge(self, *, namespace: str) -> Mapping[int, object]: ...

    async def save_combat_knowledge(
        self,
        profile_max_hp: int,
        payload: Mapping[str, JsonValue],
        *,
        namespace: str,
    ) -> None: ...

    async def add_event(self, event_type: str, message: str) -> int: ...


class LegacyCombatRecovery(Protocol):
    async def begin_death_recovery(self, target_name: str) -> None: ...

    async def recover_latest_state(self, reason: str) -> bool: ...


class FarmerCombatRuntime:
    """FoG combat adapter over explicit application and legacy-owned capabilities."""

    def __init__(
        self,
        services: MechanismServices,
        context: RuntimeContext,
        diagnostics: LegacyCombatDiagnostics,
        store: LegacyCombatStore,
        statistics: FarmStatistics,
        recovery: LegacyCombatRecovery,
        *,
        source_scope: str,
        legacy_combat_policy: Callable[[], LegacyCombatPolicy],
        target_policy: Callable[[], TargetPolicy],
        add_treatment_target: Callable[[str], Awaitable[bool]],
        remove_treatment_target: Callable[[str], Awaitable[bool]],
        wake_battle_notifications: Callable[[], None],
    ) -> None:
        self._services = services
        self._context = context
        self._diagnostics = diagnostics
        self._store = store
        self._statistics = statistics
        self._recovery = recovery
        self._source_scope = canonical_source_scope(source_scope)
        self._legacy_combat_policy = legacy_combat_policy
        self._target_policy = target_policy
        self._add_treatment_target = add_treatment_target
        self._remove_treatment_target = remove_treatment_target
        self._wake_battle_notifications = wake_battle_notifications

    @property
    def context(self) -> RuntimeContext:
        return self._context

    @property
    def running(self) -> bool:
        return self._services.running()

    @property
    def session_id(self) -> int | None:
        return self._services.session_id()

    def legacy_combat_policy(self) -> LegacyCombatPolicy:
        return self._legacy_combat_policy()

    def target_policy(self) -> TargetPolicy:
        return self._target_policy()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def battle_position(self) -> tuple[int, int] | None:
        return self.context.current_position

    def source_event_id(self, snapshot: MessageSnapshot) -> SourceEventId:
        return derive_source_event_id(self._source_scope, snapshot)

    def is_current(self, event: InboundEvent) -> bool:
        return self._services.is_current(event)

    def action_cooldown_remaining(self) -> float:
        return self._services.telegram_cooldown_remaining()

    def enter_combat(self) -> None:
        self._services.set_state_name("COMBAT")

    def interrupt_discovery(self) -> None:
        self.context.pending_move = None
        self.context.failed_move_attempts = 0
        self.context.checked_empty_position = None

    def log(self, text: str) -> None:
        self._services.log(text)

    def mark_progress(self, reason: str) -> None:
        self._services.mark_progress(reason)

    def record_session_victory(self, battle_id: int, reward: BattleReward) -> bool:
        return self._statistics.add_victory(battle_id, reward)

    def record_session_defeat(self, battle_id: int) -> bool:
        return self._statistics.add_defeat(battle_id)

    def report_session(self) -> None:
        logger.info(
            "\n%s",
            format_report("СТАТИСТИКА ТЕКУЩЕЙ СЕССИИ", self._statistics.session_report()),
        )

    async def load_combat_knowledge(self, *, namespace: str) -> Mapping[int, object]:
        return await self._store.load_combat_knowledge(namespace=namespace)

    async def save_combat_knowledge(
        self, profile_max_hp: int, payload: Mapping[str, JsonValue], *, namespace: str
    ) -> None:
        await self._store.save_combat_knowledge(
            profile_max_hp, dict(payload), namespace=namespace
        )

    async def add_treatment_enemy_target(self, target_name: str) -> bool:
        return await self._add_treatment_target(target_name)

    async def remove_treatment_enemy_target(self, target_name: str) -> bool:
        return await self._remove_treatment_target(target_name)

    async def record_combat_event(self, event_type: str, message: str) -> None:
        await self._store.add_event(event_type, message)

    async def record_battle(
        self, outcome: BattleOutcome, decisions: tuple[CombatDecisionTrace, ...]
    ) -> RecordBattleResult:
        notification_events = events_for(outcome)
        recorded = await self._diagnostics.record_battle(
            outcome,
            decisions=decisions,
            events=notification_events,
            legacy_source_event_ids=(
                (legacy_v4_message_source_event_id(outcome.source_message_id),)
                if outcome.source_message_id is not None
                else ()
            ),
        )
        if notification_events and self.running:
            self._wake_battle_notifications()
        return recorded

    async def begin_death_recovery(self, target_name: str) -> None:
        await self._recovery.begin_death_recovery(target_name)

    async def recover_latest_state(self, reason: str) -> bool:
        return await self._recovery.recover_latest_state(reason)

    async def execute_combat_action(
        self, observation: CombatObservation, action: CombatAction
    ) -> ActionOutcome:
        event = observation.event
        if not self.is_current(event) or event.snapshot != observation.snapshot:
            return ActionOutcome.STALE
        row, column = action.position
        if row >= len(event.snapshot.buttons) or column >= len(event.snapshot.buttons[row]):
            return ActionOutcome.REJECTED
        policy = self.legacy_combat_policy()
        is_skill = action.kind is CombatActionKind.ACTION
        return await self._services.click_button(
            event,
            position=action.position,
            description=action.label,
            urgent=action.urgent,
            remaining_seconds=action.remaining_seconds,
            delay_range=(
                policy.skill_delay if is_skill else policy.target_selection_delay
            ),
        )
