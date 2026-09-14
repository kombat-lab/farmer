from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from automation_policy import DelayRange, LegacyMapPolicy, TargetPolicy
from blessing import BlessingManager
from game_input import ActionOutcome, InboundEvent
from game_mechanisms import MechanismServices
from json_types import JsonValue
from legacy_map_controller import BACK_TO_MAP_BUTTON
from models import ActionType, BotState, ButtonPosition, Position, RuntimeContext
from telegram_buttons import find_button


class LegacyMapStore(Protocol):
    async def get_setting(self, key: str, default: JsonValue = None) -> JsonValue: ...

    async def set_setting(self, key: str, value: object) -> None: ...

    async def clear_map_obstacles(self) -> int: ...

    async def get_map_obstacles(self, location_name: str) -> set[Position]: ...

    async def forget_map_obstacles(
        self, location_name: str, positions: set[Position]
    ) -> int: ...

    async def remember_map_obstacle(
        self, location_name: str, position: Position
    ) -> bool: ...

    async def add_event(self, event_type: str, message: str) -> int: ...


class LegacyMapState(Protocol):
    @property
    def context(self) -> RuntimeContext: ...

    @property
    def moves_in_cycle(self) -> int: ...

    @moves_in_cycle.setter
    def moves_in_cycle(self, value: int) -> None: ...

    @property
    def cycle_target(self) -> int: ...

    @property
    def recovery_refresh_requested(self) -> bool: ...

    @recovery_refresh_requested.setter
    def recovery_refresh_requested(self, value: bool) -> None: ...

    def battle_health_is_low(self) -> bool: ...

    def wait_for_battle_health(self) -> None: ...

    def recovery_elapsed(self) -> float: ...

    async def finish_health_recovery(
        self, current_hp: int, max_hp: int | None
    ) -> None: ...


class FarmerMapRuntime:
    """FoG map adapter over explicit application and legacy-owned capabilities."""

    def __init__(
        self,
        services: MechanismServices,
        state: LegacyMapState,
        store: LegacyMapStore,
        *,
        legacy_map_policy: Callable[[], LegacyMapPolicy],
        target_policy: Callable[[], TargetPolicy],
        recovery_minimum_wait: float,
        recovery_minimum_hp: int,
    ) -> None:
        self._services = services
        self._state = state
        self._store = store
        self._legacy_map_policy = legacy_map_policy
        self._target_policy = target_policy
        self._recovery_minimum_wait = recovery_minimum_wait
        self._recovery_minimum_hp = recovery_minimum_hp
        self._blessing = BlessingManager()

    @property
    def context(self) -> RuntimeContext:
        return self._state.context

    @property
    def state(self) -> BotState:
        name = self._services.state_name()
        try:
            return BotState[name]
        except KeyError as error:
            raise RuntimeError(f"Unknown legacy FoG state: {name}") from error

    @state.setter
    def state(self, value: BotState) -> None:
        if not isinstance(value, BotState):
            raise ValueError("Legacy FoG state must be BotState")
        self._services.set_state_name(value.name)

    @property
    def pause_requested(self) -> bool:
        return self._services.pause_requested()

    @property
    def moves_in_cycle(self) -> int:
        return self._state.moves_in_cycle

    @moves_in_cycle.setter
    def moves_in_cycle(self, value: int) -> None:
        self._state.moves_in_cycle = value

    @property
    def cycle_move_target(self) -> int:
        return self._state.cycle_target

    @property
    def recovery_refresh_requested(self) -> bool:
        return self._state.recovery_refresh_requested

    @recovery_refresh_requested.setter
    def recovery_refresh_requested(self, value: bool) -> None:
        self._state.recovery_refresh_requested = value

    def legacy_map_policy(self) -> LegacyMapPolicy:
        return self._legacy_map_policy()

    def target_policy(self) -> TargetPolicy:
        return self._target_policy()

    def log(self, text: str) -> None:
        self._services.log(text)

    def mark_progress(self, reason: str) -> None:
        self._services.mark_progress(reason)

    def is_current(self, event: InboundEvent) -> bool:
        return self._services.is_current(event)

    def battle_health_is_low(self) -> bool:
        return self._state.battle_health_is_low()

    def wait_for_battle_health(self) -> None:
        self._state.wait_for_battle_health()

    def activity_break_is_due(self) -> bool:
        return self._services.activity_break_is_due(self.moves_in_cycle)

    def recovery_elapsed(self) -> float:
        return self._state.recovery_elapsed()

    def recovery_minimum_wait(self) -> float:
        return self._recovery_minimum_wait

    def recovery_minimum_hp(self) -> int:
        return self._recovery_minimum_hp

    async def navigation_model_is_current(self, version: int) -> bool:
        stored_version = await self._store.get_setting("navigation_model_version", 0)
        return stored_version == version

    async def set_navigation_model_version(self, version: int) -> None:
        await self._store.set_setting("navigation_model_version", version)

    async def clear_map_obstacles(self) -> int:
        return await self._store.clear_map_obstacles()

    async def get_map_obstacles(self, location_name: str) -> set[Position]:
        return await self._store.get_map_obstacles(location_name)

    async def forget_map_obstacles(
        self,
        location_name: str,
        positions: set[Position],
    ) -> int:
        return await self._store.forget_map_obstacles(location_name, positions)

    async def remember_map_obstacle(self, location_name: str, position: Position) -> bool:
        return await self._store.remember_map_obstacle(location_name, position)

    async def record_map_event(self, event_type: str, message: str) -> None:
        await self._store.add_event(event_type, message)

    async def stop(self, reason: str) -> None:
        await self._services.stop(reason)

    async def enter_paused(self) -> None:
        await self._services.enter_paused()

    async def complete_cycle(self) -> None:
        await self._services.complete_cycle()

    async def start_activity_break(self) -> None:
        await self._services.start_activity_break()

    async def pause_after_movement(self) -> None:
        await self._services.pause_after_progress()

    def _blessing_clicker(
        self,
        event: InboundEvent,
        policy: LegacyMapPolicy,
    ) -> Callable[..., Awaitable[ActionOutcome]]:
        async def click_button(
            *,
            contains: tuple[str, ...],
            action_type: ActionType,
            description: str,
        ) -> ActionOutcome:
            del action_type
            return await self._services.click_button(
                event,
                contains=contains,
                description=description,
                delay_range=policy.open_attack_delay,
            )

        return click_button

    async def try_refresh_blessing_from_map(
        self,
        event: InboundEvent,
        policy: LegacyMapPolicy,
    ) -> bool:
        if not policy.blessing_enabled:
            self._blessing.cancel()
            return False
        if not self.is_current(event):
            return False
        return await self._blessing.try_open_from_map(
            click_button=self._blessing_clicker(event, policy),
            log=self.log,
            mark_progress=self.mark_progress,
        )

    async def handle_blessing_menu(
        self,
        event: InboundEvent,
        policy: LegacyMapPolicy,
    ) -> bool:
        if not self.is_current(event):
            return False
        if not policy.blessing_enabled:
            if not self._blessing.cancel():
                return False
            await self.click_button(
                event,
                exact=BACK_TO_MAP_BUTTON,
                action_type=ActionType.OPEN_ATTACK,
                description=BACK_TO_MAP_BUTTON,
                delay_range=policy.open_attack_delay,
            )
            return True
        return await self._blessing.handle_menu(
            event.snapshot,
            find_button=find_button,
            click_button=self._blessing_clicker(event, policy),
            mark_progress=self.mark_progress,
        )

    def confirm_blessing_from_text(self, text: str, policy: LegacyMapPolicy) -> None:
        if not policy.blessing_enabled:
            self._blessing.cancel()
            return
        self._blessing.confirm_from_text(
            text,
            log=self.log,
            mark_progress=self.mark_progress,
        )

    async def request_current_state(
        self,
        *,
        force: bool = False,
        recovery_reason: str | None = None,
    ) -> bool:
        return await self._services.request_current_state(
            force=force,
            recovery_reason=recovery_reason,
        )

    async def send_game_message(self, text: str, action_label: str) -> ActionOutcome:
        return await self._services.send_game_message(text, action_label)

    async def finish_health_recovery(self, current_hp: int, max_hp: int | None) -> None:
        await self._state.finish_health_recovery(current_hp, max_hp)

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
        del action_type
        return await self._services.click_button(
            event,
            description=description,
            delay_range=delay_range,
            exact=exact,
            position=position,
        )
