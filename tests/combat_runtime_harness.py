from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from uuid import UUID

from automation_policy import DelayRange, LegacyCombatPolicy, TargetPolicy
from battle_records import BattleOutcome, RecordBattleResult, SourceEventId
from combat import CombatAction, CombatObservation
from combat_strategy import CombatDecisionTrace
from game_input import ActionKey, ActionOutcome, InboundEvent, InputDescriptor, PromptToken
from json_types import JsonValue
from message_snapshot import MessageSnapshot, ReadableMessage, derive_source_event_id
from rewards import BattleReward


@dataclass
class Button:
    text: str
    data: bytes | None = None


@dataclass
class Message:
    id: int
    raw_text: str
    buttons: list[list[Button]] = field(default_factory=list)
    edit_date: datetime | None = None

    async def click(self, row: int, column: int) -> object:
        raise AssertionError("The combat controller must use the runtime action port")


@dataclass
class Context:
    current_hp: int | None = 400
    max_hp: int | None = 400
    active_target: str | None = None
    battle_target: str | None = None
    combat_enemies: list[str] = field(default_factory=list)

    def add_combat_enemy(self, name: str) -> None:
        if name and name not in self.combat_enemies:
            self.combat_enemies.append(name)
            if self.battle_target is None:
                self.battle_target = name

    def remove_combat_enemy(self, name: str) -> None:
        self.combat_enemies = [item for item in self.combat_enemies if item != name]
        if self.active_target == name:
            self.active_target = next(iter(self.combat_enemies), None)

    def clear_combat(self) -> None:
        self.active_target = None
        self.battle_target = None
        self.combat_enemies.clear()


@dataclass
class Runtime:
    context: Context = field(default_factory=Context)
    running: bool = True
    session_id: int | None = 1
    targets: TargetPolicy = TargetPolicy(("Фонарщик", "Пепельник"))
    policy: LegacyCombatPolicy = LegacyCombatPolicy(
        (), 160, 100, "shadow", DelayRange(0, 0), DelayRange(0, 0)
    )
    clock: datetime = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    position: tuple[int, int] | None = None
    latest: bool = True
    cooldown: float = 0
    action_result: ActionOutcome = ActionOutcome.SENT
    sequence: int = 0
    current_event: InboundEvent | None = None
    in_combat: bool = False
    interruptions: int = 0
    reports: int = 0
    policy_after_confirmation: TargetPolicy | None = None
    actions: list[tuple[InboundEvent, MessageSnapshot, CombatAction]] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    progress: list[str] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    recoveries: list[str] = field(default_factory=list)
    events: list[tuple[str, str]] = field(default_factory=list)
    victories: dict[int, BattleReward] = field(default_factory=dict)
    defeats: set[int] = field(default_factory=set)
    outcomes: dict[int, BattleOutcome] = field(default_factory=dict)
    decisions: dict[int, tuple[CombatDecisionTrace, ...]] = field(default_factory=dict)
    record_calls: int = 0
    knowledge: dict[tuple[str, int], object] = field(default_factory=dict)
    loads: list[str] = field(default_factory=list)
    saves: list[tuple[str, int, Mapping[str, object]]] = field(default_factory=list)
    source_scope: str = "test:legacy-combat"

    def legacy_combat_policy(self) -> LegacyCombatPolicy:
        return self.policy

    def target_policy(self) -> TargetPolicy:
        return self.targets

    def now(self) -> datetime:
        return self.clock

    def battle_position(self) -> tuple[int, int] | None:
        return self.position

    def source_event_id(self, snapshot: MessageSnapshot) -> SourceEventId:
        return derive_source_event_id(self.source_scope, snapshot)

    def capture(self, message: ReadableMessage) -> InboundEvent:
        self.sequence += 1
        snapshot = MessageSnapshot.from_message(message)
        token = PromptToken(UUID(int=1), self.sequence, snapshot.id)
        descriptor = InputDescriptor(snapshot, (snapshot.id, snapshot.raw_text), snapshot, True)
        action = ActionKey(None, 0, descriptor.fact_key)
        event = InboundEvent(self.sequence, descriptor, token, action)
        self.current_event = event
        return event

    def is_current(self, event: InboundEvent) -> bool:
        return self.latest and event is self.current_event

    def action_cooldown_remaining(self) -> float:
        return self.cooldown

    def enter_combat(self) -> None:
        self.in_combat = True

    def interrupt_discovery(self) -> None:
        self.interruptions += 1

    def log(self, text: str) -> None:
        self.logs.append(text)

    def mark_progress(self, reason: str) -> None:
        self.progress.append(reason)

    def record_session_victory(self, battle_id: int, reward: BattleReward) -> bool:
        if battle_id in self.victories:
            return False
        self.victories[battle_id] = reward
        return True

    def record_session_defeat(self, battle_id: int) -> bool:
        if battle_id in self.defeats:
            return False
        self.defeats.add(battle_id)
        return True

    def report_session(self) -> None:
        self.reports += 1

    async def load_combat_knowledge(self, *, namespace: str) -> Mapping[int, object]:
        self.loads.append(namespace)
        return {
            profile: deepcopy(payload)
            for (stored_namespace, profile), payload in self.knowledge.items()
            if stored_namespace == namespace
        }

    async def save_combat_knowledge(
        self, profile_max_hp: int, payload: Mapping[str, JsonValue], *, namespace: str
    ) -> None:
        # Retain the supplied value as well as the stored copy to detect aliasing.
        self.saves.append((namespace, profile_max_hp, payload))
        self.knowledge[namespace, profile_max_hp] = deepcopy(payload)

    async def add_treatment_enemy_target(self, target_name: str) -> bool:
        added = target_name not in self.policy.treatment_enemies
        if added:
            self.policy = replace(
                self.policy,
                treatment_enemies=(*self.policy.treatment_enemies, target_name),
            )
        if self.policy_after_confirmation is not None:
            self.targets = self.policy_after_confirmation
        return added

    async def remove_treatment_enemy_target(self, target_name: str) -> bool:
        removed = target_name in self.policy.treatment_enemies
        self.policy = replace(
            self.policy,
            treatment_enemies=tuple(
                target for target in self.policy.treatment_enemies if target != target_name
            ),
        )
        return removed

    async def record_combat_event(self, event_type: str, message: str) -> None:
        self.events.append((event_type, message))

    async def record_battle(
        self, outcome: BattleOutcome, decisions: tuple[CombatDecisionTrace, ...]
    ) -> RecordBattleResult:
        self.record_calls += 1
        previous = next(
            (
                battle_id
                for battle_id, stored in self.outcomes.items()
                if stored.source_event_id == outcome.source_event_id
            ),
            None,
        )
        if previous is not None:
            return RecordBattleResult(False, previous)
        preferred = outcome.source_message_id
        battle_id = (
            preferred
            if preferred is not None and preferred not in self.outcomes
            else max(self.outcomes, default=0) + 1
        )
        self.outcomes[battle_id] = outcome
        self.decisions[battle_id] = decisions
        return RecordBattleResult(True, battle_id)

    async def begin_death_recovery(self, target_name: str) -> None:
        self.in_combat = False
        self.recoveries.append(target_name)

    async def recover_latest_state(self, reason: str) -> bool:
        self.requests.append(reason)
        return True

    async def execute_combat_action(
        self, observation: CombatObservation, action: CombatAction
    ) -> ActionOutcome:
        snapshot = observation.snapshot
        if not self.running or not self.latest or self.cooldown > 0:
            return ActionOutcome.DEFERRED
        self.actions.append((observation.event, snapshot, action))
        return self.action_result
