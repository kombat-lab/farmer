from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass, fields
from typing import Protocol, cast

from automation_policy import DelayRange
from bounded_values import require_int64
from game_input import ActionOutcome, InboundEvent, InputPolicy
from json_types import JsonValue
from liveness import LivenessPhase
from runtime_state import require_phase_name


@dataclass(frozen=True, slots=True)
class CycleDescriptor:
    """Mechanism-neutral progress target for one application cycle."""

    target: int
    minimum: int
    maximum: int
    unit_label: str

    def __post_init__(self) -> None:
        target = require_int64(self.target, "Cycle target", minimum=1)
        minimum = require_int64(self.minimum, "Cycle minimum", minimum=1)
        maximum = require_int64(self.maximum, "Cycle maximum", minimum=1)
        if maximum < minimum or not minimum <= target <= maximum:
            raise ValueError("Cycle target must be inside its ordered range")
        if not isinstance(self.unit_label, str) or not self.unit_label.strip():
            raise ValueError("Cycle unit label must be a nonempty string")
        object.__setattr__(self, "unit_label", self.unit_label.strip())


@dataclass(frozen=True, slots=True)
class MechanismSnapshot:
    """Stable application view of replaceable game-mechanism state."""

    phase_name: str
    position: tuple[int, int] | None
    location_name: str | None
    current_hp: int | None
    max_hp: int | None
    active_target: str | None
    total_progress_units: int
    cycle_progress_units: int
    liveness_phase: LivenessPhase
    liveness_suspended: bool

    def __post_init__(self) -> None:
        phase_name = require_phase_name(self.phase_name)
        if self.position is not None:
            if not isinstance(self.position, tuple) or len(self.position) != 2:
                raise ValueError("Mechanism position must be a two-item tuple")
            x = require_int64(self.position[0], "Mechanism position x")
            y = require_int64(self.position[1], "Mechanism position y")
            object.__setattr__(self, "position", (x, y))
        current_hp = (
            None
            if self.current_hp is None
            else require_int64(self.current_hp, "Current HP", minimum=0)
        )
        max_hp = (
            None
            if self.max_hp is None
            else require_int64(self.max_hp, "Maximum HP", minimum=0)
        )
        if current_hp is not None and max_hp is not None and current_hp > max_hp:
            raise ValueError("Current HP cannot exceed maximum HP")
        for label, value in (
            ("Mechanism location", self.location_name),
            ("Active target", self.active_target),
        ):
            if value is not None and (
                not isinstance(value, str) or not value or value != value.strip()
            ):
                raise ValueError(f"{label} must be a nonempty canonical string")
        total = require_int64(
            self.total_progress_units,
            "Total progress units",
            minimum=0,
        )
        cycle = require_int64(
            self.cycle_progress_units,
            "Cycle progress units",
            minimum=0,
        )
        if cycle > total:
            raise ValueError("Cycle progress units cannot exceed total progress units")
        if type(self.liveness_phase) is not LivenessPhase:
            raise ValueError("Mechanism snapshot requires a LivenessPhase")
        if type(self.liveness_suspended) is not bool:
            raise ValueError("Liveness suspended marker must be bool")
        object.__setattr__(self, "phase_name", phase_name)
        object.__setattr__(self, "current_hp", current_hp)
        object.__setattr__(self, "max_hp", max_hp)
        object.__setattr__(self, "total_progress_units", total)
        object.__setattr__(self, "cycle_progress_units", cycle)


class _ClickButton(Protocol):
    def __call__(
        self,
        event: InboundEvent,
        *,
        description: str,
        delay_range: DelayRange,
        exact: str | None = None,
        contains: tuple[str, ...] = (),
        exclude: tuple[str, ...] = (),
        position: tuple[int, int] | None = None,
        urgent: bool = False,
        remaining_seconds: int | None = None,
    ) -> Awaitable[ActionOutcome]: ...


class _RequestState(Protocol):
    def __call__(
        self,
        *,
        force: bool = False,
        recovery_reason: str | None = None,
    ) -> Awaitable[bool]: ...


@dataclass(frozen=True, slots=True)
class MechanismServices:
    """Narrow application capabilities available to a mechanism bundle."""

    running: Callable[[], bool]
    session_id: Callable[[], int | None]
    state_name: Callable[[], str]
    set_state_name: Callable[[str], None]
    pause_requested: Callable[[], bool]
    is_current: Callable[[InboundEvent], bool]
    telegram_cooldown_remaining: Callable[[], float]
    log: Callable[[str], None]
    mark_progress: Callable[[str], None]
    activity_break_is_due: Callable[[int], bool]
    stop: Callable[[str], Awaitable[None]]
    enter_paused: Callable[[], Awaitable[None]]
    complete_cycle: Callable[[], Awaitable[None]]
    start_activity_break: Callable[[], Awaitable[None]]
    pause_after_progress: Callable[[], Awaitable[None]]
    request_current_state: _RequestState
    send_game_message: Callable[[str, str], Awaitable[ActionOutcome]]
    click_button: _ClickButton
    start_task: Callable[[Coroutine[object, object, None], str], asyncio.Task[None]]

    def __post_init__(self) -> None:
        for field in fields(self):
            if not callable(getattr(self, field.name)):
                raise ValueError(f"Mechanism service {field.name} must be callable")


class MechanismRuntime(Protocol):
    @property
    def input_policy(self) -> InputPolicy: ...

    def validate(self) -> None: ...

    async def initialize(self) -> None: ...

    def start_cycle(self, cycle_number: int) -> CycleDescriptor: ...

    async def handle(self, event: InboundEvent) -> bool: ...

    async def request_state(self) -> ActionOutcome: ...

    def snapshot(self) -> MechanismSnapshot: ...

    def cycle_descriptor(self) -> CycleDescriptor | None: ...

    def diagnostics(self) -> Mapping[str, JsonValue]: ...

    def session_elapsed_seconds(self) -> int: ...

    def format_session_report(self, title: str) -> str: ...

    async def aclose(self) -> None: ...


class MechanismBundle(Protocol):
    """Pure composition plan for one coherent set of game mechanics."""

    def validate(self) -> None: ...

    def build(self, services: MechanismServices) -> MechanismRuntime: ...


def require_mechanism_runtime(value: object) -> MechanismRuntime:
    """Reject incomplete alternative implementations at the composition boundary."""

    method_names = (
        "validate",
        "initialize",
        "start_cycle",
        "handle",
        "request_state",
        "snapshot",
        "cycle_descriptor",
        "diagnostics",
        "session_elapsed_seconds",
        "format_session_report",
        "aclose",
    )
    missing = tuple(name for name in method_names if not callable(getattr(value, name, None)))
    policy = getattr(value, "input_policy", None)
    if missing or not callable(getattr(policy, "describe", None)) or not callable(
        getattr(policy, "admit", None)
    ):
        details = ", ".join(missing) if missing else "input_policy"
        raise TypeError(f"Invalid mechanism runtime contract: {details}")
    return cast(MechanismRuntime, value)
