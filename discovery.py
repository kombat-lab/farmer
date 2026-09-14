from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from game_input import ActionOutcome, InboundEvent
from message_snapshot import MessageSnapshot


class DiscoveryEventKind(Enum):
    STATE = "state"
    PROGRESS_CONFIRMED = "progress_confirmed"
    TARGET_SELECTION = "target_selection"
    TARGET_GONE = "target_gone"
    AUXILIARY = "auxiliary"


class DiscoveryObservation(Protocol):
    @property
    def event(self) -> InboundEvent: ...

    @property
    def snapshot(self) -> MessageSnapshot: ...

    @property
    def kind(self) -> DiscoveryEventKind: ...


@dataclass(frozen=True, slots=True)
class DiscoveryStatus:
    location_name: str | None
    progress_units: int
    progress_label: str

    def __post_init__(self) -> None:
        if self.location_name is not None and (
            not isinstance(self.location_name, str) or not self.location_name.strip()
        ):
            raise ValueError("Discovery location must be a nonempty string or None")
        if type(self.progress_units) is not int or self.progress_units < 0:
            raise ValueError("Discovery progress must be a nonnegative integer")
        if not isinstance(self.progress_label, str) or not self.progress_label.strip():
            raise ValueError("Discovery progress label must be a nonempty string")


class DiscoveryController(Protocol):
    def status(self) -> DiscoveryStatus: ...

    async def request_state(self) -> ActionOutcome:
        """Request state through an application-owned effect boundary."""
        ...

    async def initialize(self) -> int: ...

    def reset_cycle(self) -> None: ...

    def observe_message(self, event: InboundEvent) -> DiscoveryObservation | None: ...

    async def handle_message(self, observation: DiscoveryObservation) -> bool: ...
