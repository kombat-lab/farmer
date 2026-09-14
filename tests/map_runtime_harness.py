from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from automation_policy import DelayRange, IntegerRange, LegacyMapPolicy, TargetPolicy
from blessing import NON_COMBAT_SKILLS_BUTTON, BlessingManager
from game_input import ActionKey, ActionOutcome, InboundEvent, InputDescriptor, PromptToken
from message_snapshot import MessageSnapshot, ReadableMessage
from models import ActionType, BotState, ButtonPosition, Position, RuntimeContext

_DEFAULT_DELAY = DelayRange(0, 0)
_DEFAULT_MAP_POLICY = LegacyMapPolicy(
    moves_per_cycle=IntegerRange(1, 100),
    blessing_enabled=False,
    move_delay=_DEFAULT_DELAY,
    open_attack_delay=_DEFAULT_DELAY,
    target_selection_delay=_DEFAULT_DELAY,
)


@dataclass
class MapRuntimeHarness:
    context: RuntimeContext = field(default_factory=RuntimeContext)
    state: BotState = BotState.STARTING
    running: bool = True
    pause_requested: bool = False
    moves_in_cycle: int = 0
    cycle_move_target: int = 100
    recovery_refresh_requested: bool = False
    targets: tuple[str, ...] = ()
    policy: LegacyMapPolicy = _DEFAULT_MAP_POLICY
    latest: bool = True
    health_low: bool = False
    break_due: bool = False
    click_result: ActionOutcome = ActionOutcome.SENT
    elapsed: float = 1000.0
    model_version: int = 0
    sequence: int = 0
    current_event: InboundEvent | None = None
    obstacles: dict[str, set[Position]] = field(default_factory=dict)
    actions: list[tuple[ActionType, str | None, ButtonPosition | None]] = field(
        default_factory=list
    )
    action_events: list[InboundEvent] = field(default_factory=list)
    action_delays: list[DelayRange] = field(default_factory=list)
    requests: list[tuple[bool, str | None]] = field(default_factory=list)
    raw_messages: list[tuple[str, str]] = field(default_factory=list)
    events: list[tuple[str, str]] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    progress: list[str] = field(default_factory=list)
    movement_pauses: int = 0
    completed_cycles: int = 0
    blessing: BlessingManager = field(default_factory=BlessingManager)

    def capture(self, message: ReadableMessage) -> InboundEvent:
        self.sequence += 1
        snapshot = MessageSnapshot.from_message(message)
        descriptor = InputDescriptor(
            snapshot,
            (snapshot.id, snapshot.raw_text),
            (snapshot.id, snapshot.raw_text, snapshot.buttons),
            True,
        )
        token = PromptToken(UUID(int=2), self.sequence, snapshot.id)
        event = InboundEvent(
            self.sequence,
            descriptor,
            token,
            ActionKey(None, 0, descriptor.fact_key),
        )
        self.current_event = event
        return event

    def legacy_map_policy(self) -> LegacyMapPolicy:
        return self.policy

    def target_policy(self) -> TargetPolicy:
        return TargetPolicy(self.targets)

    def log(self, text: str) -> None:
        self.logs.append(text)

    def mark_progress(self, reason: str) -> None:
        self.progress.append(reason)

    def is_current(self, event: InboundEvent) -> bool:
        return self.latest and event is self.current_event

    def battle_health_is_low(self) -> bool:
        return self.health_low

    def wait_for_battle_health(self) -> None:
        self.state = BotState.WAITING_FOR_HEALTH

    def activity_break_is_due(self) -> bool:
        return self.break_due

    def recovery_elapsed(self) -> float:
        return self.elapsed

    def recovery_minimum_wait(self) -> float:
        return 30.0

    def recovery_minimum_hp(self) -> int:
        return 100

    async def navigation_model_is_current(self, version: int) -> bool:
        return self.model_version == version

    async def set_navigation_model_version(self, version: int) -> None:
        self.model_version = version

    async def clear_map_obstacles(self) -> int:
        count = sum(len(positions) for positions in self.obstacles.values())
        self.obstacles.clear()
        return count

    async def get_map_obstacles(self, location_name: str) -> set[Position]:
        return set(self.obstacles.get(location_name, set()))

    async def forget_map_obstacles(self, location_name: str, positions: set[Position]) -> int:
        existing = self.obstacles.setdefault(location_name, set())
        count = len(existing & positions)
        existing.difference_update(positions)
        return count

    async def remember_map_obstacle(self, location_name: str, position: Position) -> bool:
        existing = self.obstacles.setdefault(location_name, set())
        if position in existing:
            return False
        existing.add(position)
        return True

    async def record_map_event(self, event_type: str, message: str) -> None:
        self.events.append((event_type, message))

    async def stop(self, reason: str) -> None:
        self.running = False

    async def enter_paused(self) -> None:
        self.state = BotState.PAUSED

    async def complete_cycle(self) -> None:
        self.completed_cycles += 1

    async def start_activity_break(self) -> None:
        self.state = BotState.ACTIVITY_BREAK

    async def pause_after_movement(self) -> None:
        self.movement_pauses += 1

    async def try_refresh_blessing_from_map(
        self,
        event: InboundEvent,
        policy: LegacyMapPolicy,
    ) -> bool:
        if not policy.blessing_enabled:
            self.blessing.cancel()
            return False
        if not any(
            NON_COMBAT_SKILLS_BUTTON.casefold() in button.text.casefold()
            for row in event.snapshot.buttons
            for button in row
        ):
            return False

        async def click_for_event(**_kwargs: object) -> ActionOutcome:
            return await self.click_button(
                event,
                exact=NON_COMBAT_SKILLS_BUTTON,
                action_type=ActionType.OPEN_ATTACK,
                description=NON_COMBAT_SKILLS_BUTTON,
                delay_range=policy.open_attack_delay,
            )

        return await self.blessing.try_open_from_map(
            click_button=click_for_event,
            log=self.log,
            mark_progress=self.mark_progress,
        )

    async def handle_blessing_menu(
        self,
        event: InboundEvent,
        policy: LegacyMapPolicy,
    ) -> bool:
        return False

    def confirm_blessing_from_text(self, text: str, policy: LegacyMapPolicy) -> None:
        pass

    async def request_current_state(
        self, *, force: bool = False, recovery_reason: str | None = None
    ) -> bool:
        self.requests.append((force, recovery_reason))
        return True

    async def send_game_message(self, text: str, action_label: str) -> ActionOutcome:
        self.raw_messages.append((text, action_label))
        return ActionOutcome.SENT

    async def finish_health_recovery(self, current_hp: int, max_hp: int | None) -> None:
        self.recovery_refresh_requested = False
        self.events.append(("RECOVERY_FINISHED", str(current_hp)))

    async def click_button(
        self,
        event: InboundEvent,
        *,
        action_type: ActionType,
        description: str,
        delay_range: DelayRange,
        exact: str | None = None,
        position: ButtonPosition | None = None,
    ) -> ActionOutcome:
        self.action_events.append(event)
        self.action_delays.append(delay_range)
        self.actions.append((action_type, exact, position))
        return self.click_result
