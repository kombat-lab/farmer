from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass
from enum import Enum, auto
from typing import Protocol
from uuid import UUID

from message_snapshot import MessageSnapshot


def _require_hashable(value: object, name: str) -> None:
    try:
        hash(value)
    except TypeError as error:
        raise ValueError(f"{name} must be hashable") from error


def _validate_scope(scope: object) -> None:
    if not isinstance(scope, str) or not scope or scope != scope.strip():
        raise ValueError("Action scopes must be nonempty canonical strings")


@dataclass(frozen=True, slots=True)
class InputDescriptor:
    snapshot: MessageSnapshot
    fact_key: Hashable
    state_key: Hashable
    is_prompt: bool
    # A named scope advances when its own facts change. None uses epoch zero.
    action_scope: str | None = None
    # Accepted boundaries advance named scopes without assigning this prompt to them.
    advances_action_scopes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, MessageSnapshot):
            raise ValueError("Input descriptor requires a MessageSnapshot")
        _require_hashable(self.fact_key, "Fact key")
        _require_hashable(self.state_key, "State key")
        if type(self.is_prompt) is not bool:
            raise ValueError("Prompt marker must be bool")
        if self.action_scope is not None:
            _validate_scope(self.action_scope)
        if not isinstance(self.advances_action_scopes, (tuple, list)):
            raise ValueError("Advanced action scopes must be a list or tuple")
        scopes = tuple(self.advances_action_scopes)
        for scope in scopes:
            _validate_scope(scope)
        object.__setattr__(self, "advances_action_scopes", scopes)


class AdmissionDecision(Enum):
    ACCEPT = auto()
    DUPLICATE = auto()


class InputPolicy(Protocol):
    """Policy identity and admission, independent of transport and queue ownership.

    For an incoming named action scope, current_prompt is its last scoped prompt,
    or None after an explicit boundary. Unscoped inputs see the global prompt.
    """

    def describe(self, snapshot: MessageSnapshot) -> InputDescriptor: ...

    def admit(
        self,
        previous: InputDescriptor | None,
        current_prompt: InputDescriptor | None,
        incoming: InputDescriptor,
    ) -> AdmissionDecision: ...


@dataclass(frozen=True, slots=True)
class PromptToken:
    ingress_id: UUID
    generation: int
    message_id: int

    def __post_init__(self) -> None:
        if not isinstance(self.ingress_id, UUID):
            raise ValueError("Prompt token requires a UUID ingress id")
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("Prompt generation must be a positive integer")
        if type(self.message_id) is not int or self.message_id <= 0:
            raise ValueError("Prompt message id must be a positive integer")


@dataclass(frozen=True, slots=True)
class ActionKey:
    scope: str | None
    epoch: int
    fact_key: Hashable

    def __post_init__(self) -> None:
        if self.scope is not None:
            _validate_scope(self.scope)
        if type(self.epoch) is not int or self.epoch < 0:
            raise ValueError("Action epoch must be a nonnegative integer")
        _require_hashable(self.fact_key, "Action fact key")


@dataclass(frozen=True, slots=True)
class InboundEvent:
    sequence: int
    descriptor: InputDescriptor
    prompt_token: PromptToken | None
    action_key: ActionKey | None

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValueError("Inbound sequence must be a positive integer")
        if not isinstance(self.descriptor, InputDescriptor):
            raise ValueError("Inbound event requires an InputDescriptor")
        if self.descriptor.is_prompt:
            if self.prompt_token is None or self.action_key is None:
                raise ValueError("Prompt events require prompt and action identities")
            if self.prompt_token.message_id != self.descriptor.snapshot.id:
                raise ValueError("Prompt token belongs to another message")
            if (
                self.action_key.scope != self.descriptor.action_scope
                or self.action_key.fact_key != self.descriptor.fact_key
            ):
                raise ValueError("Action identity does not match its descriptor")
        elif self.prompt_token is not None or self.action_key is not None:
            raise ValueError("Passive events cannot carry action identities")

    @property
    def snapshot(self) -> MessageSnapshot:
        return self.descriptor.snapshot


class AdmissionOutcome(Enum):
    ACCEPTED = auto()
    DUPLICATE = auto()
    STALE_REVISION = auto()
    CLOSED = auto()


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    outcome: AdmissionOutcome
    event: InboundEvent | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, AdmissionOutcome):
            raise ValueError("Admission result requires AdmissionOutcome")
        if (self.outcome is AdmissionOutcome.ACCEPTED) != (self.event is not None):
            raise ValueError("Only an accepted admission may contain an event")
        if self.event is not None and not isinstance(self.event, InboundEvent):
            raise ValueError("Accepted admission requires an InboundEvent")

    @property
    def accepted(self) -> bool:
        return self.outcome is AdmissionOutcome.ACCEPTED


class ActionOutcome(Enum):
    SENT = auto()
    STALE = auto()
    DUPLICATE = auto()
    DEFERRED = auto()
    REJECTED = auto()
    DELIVERY_UNKNOWN = auto()