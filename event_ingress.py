from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Hashable
from uuid import uuid4

from game_input import (
    ActionKey,
    AdmissionDecision,
    AdmissionOutcome,
    AdmissionResult,
    InboundEvent,
    InputDescriptor,
    InputPolicy,
    PromptToken,
)
from message_snapshot import MessageSnapshot


class IngressClosedError(RuntimeError):
    """The input stream is closed and all queued observations have been consumed."""


class EventIngress:
    """Ordered observations and an independent, immediately updated prompt registry.

    Intended for one event-loop consumer. Producers are serialized through capacity
    waits; accepted observations are never evicted from the bounded queue. Closing
    wakes blocked producers/consumers without spawning helper tasks. Cancellation
    after publishing an observation closes admission: earlier tokens never revive.
    Closing admission never acknowledges queued or in-flight observations: consumers
    must drain get() until IngressClosedError and call task_done() after processing.
    """

    def __init__(
        self,
        policy: InputPolicy,
        *,
        capacity: int = 200,
        registry_capacity: int = 200,
    ) -> None:
        if (
            type(capacity) is not int
            or capacity <= 0
            or type(registry_capacity) is not int
            or registry_capacity <= 0
        ):
            raise ValueError("Ingress capacities must be positive integers")
        self._policy = policy
        self._capacity = capacity
        self._registry_capacity = registry_capacity
        self._ingress_id = uuid4()
        self._producer_lock = asyncio.Lock()
        self._registry: dict[int, InputDescriptor] = {}
        self._items: deque[InboundEvent] = deque()
        self._items_available = asyncio.Event()
        self._space_available = asyncio.Event()
        self._space_available.set()
        self._drained = asyncio.Event()
        self._drained.set()
        self._unfinished = 0
        self._closed = False
        self._sequence = 0
        self._generation = 0
        self._action_epochs: dict[str, int] = {}
        self._scope_fact_keys: dict[str, Hashable] = {}
        self._scope_prompts: dict[str, InputDescriptor] = {}
        self._latest_prompt: InboundEvent | None = None

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def latest_prompt(self) -> InboundEvent | None:
        return self._latest_prompt

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def registry_size(self) -> int:
        return len(self._registry)

    def latest_for(self, message_id: int) -> InputDescriptor | None:
        return self._registry.get(message_id)

    def action_epoch(self, scope: str) -> int:
        return self._action_epochs.get(scope, 0)

    def qsize(self) -> int:
        return len(self._items)

    def empty(self) -> bool:
        return not self._items

    def full(self) -> bool:
        return len(self._items) >= self._capacity

    def is_current(self, token: PromptToken | None) -> bool:
        latest = self._latest_prompt
        if self._closed or token is None or latest is None or latest.prompt_token != token:
            return False
        registered = self._registry.get(token.message_id)
        action_key = latest.action_key
        return (
            registered is not None
            and registered.state_key == latest.descriptor.state_key
            and action_key is not None
            and (
                action_key.scope is None
                or action_key.epoch == self.action_epoch(action_key.scope)
            )
        )

    def _publish(self, descriptor: InputDescriptor) -> InboundEvent:
        self._sequence += 1
        token: PromptToken | None = None
        action_key: ActionKey | None = None
        boundaries = set(descriptor.advances_action_scopes)
        latest = self._latest_prompt
        if (
            latest is not None
            and latest.action_key is not None
            and latest.action_key.scope in boundaries
        ):
            self._latest_prompt = None
        for advanced_scope in boundaries:
            self._action_epochs[advanced_scope] = self.action_epoch(advanced_scope) + 1
            self._scope_prompts.pop(advanced_scope, None)
        if descriptor.is_prompt:
            scope = descriptor.action_scope
            if scope is not None:
                if scope not in self._scope_fact_keys:
                    if self.action_epoch(scope) == 0:
                        self._action_epochs[scope] = 1
                elif (
                    self._scope_fact_keys[scope] != descriptor.fact_key and scope not in boundaries
                ):
                    self._action_epochs[scope] = self.action_epoch(scope) + 1
                self._scope_fact_keys[scope] = descriptor.fact_key
                self._scope_prompts[scope] = descriptor
            self._generation += 1
            token = PromptToken(self._ingress_id, self._generation, descriptor.snapshot.id)
            action_key = ActionKey(
                scope, self.action_epoch(scope) if scope is not None else 0, descriptor.fact_key
            )
        event = InboundEvent(self._sequence, descriptor, token, action_key)
        if descriptor.is_prompt:
            self._latest_prompt = event
        return event

    def _append(self, event: InboundEvent) -> None:
        self._items.append(event)
        self._unfinished += 1
        self._drained.clear()
        self._items_available.set()
        if self.full():
            self._space_available.clear()

    def _remember_revision(
        self,
        descriptor: InputDescriptor,
        *,
        preserve_latest: bool,
    ) -> None:
        message_id = descriptor.snapshot.id
        if message_id not in self._registry and len(self._registry) >= self._registry_capacity:
            latest = self._latest_prompt
            # Passive facts and semantic duplicates must leave the actionable
            # prompt registered. A newly accepted prompt will replace it.
            pinned_id = (
                latest.snapshot.id
                if latest is not None and (not descriptor.is_prompt or preserve_latest)
                else message_id
            )
            evicted_id = next(
                (registered_id for registered_id in self._registry if registered_id != pinned_id),
                None,
            )
            if evicted_id is None:
                # With capacity one, retain the prompt and still enqueue the
                # passive fact; only its optional duplicate watermark is omitted.
                return
            self._registry.pop(evicted_id)
        self._registry[message_id] = descriptor

    async def accept(self, snapshot: MessageSnapshot) -> AdmissionResult:
        async with self._producer_lock:
            if self._closed:
                return AdmissionResult(AdmissionOutcome.CLOSED)
            previous = self._registry.get(snapshot.id)
            if previous is not None and (
                snapshot.revision_timestamp < previous.snapshot.revision_timestamp
            ):
                return AdmissionResult(AdmissionOutcome.STALE_REVISION)
            descriptor = self._policy.describe(snapshot)
            if not isinstance(descriptor, InputDescriptor) or descriptor.snapshot is not snapshot:
                raise TypeError("InputPolicy.describe() must describe the exact supplied snapshot")
            current = (
                self._scope_prompts.get(descriptor.action_scope)
                if descriptor.action_scope is not None
                else self._latest_prompt.descriptor if self._latest_prompt is not None else None
            )
            decision = self._policy.admit(previous, current, descriptor)
            if not isinstance(decision, AdmissionDecision):
                raise TypeError("InputPolicy.admit() must return AdmissionDecision")
            # Even semantic no-ops update the revision watermark for this message.
            self._remember_revision(
                descriptor,
                preserve_latest=decision is AdmissionDecision.DUPLICATE,
            )
            if decision is AdmissionDecision.DUPLICATE:
                return AdmissionResult(AdmissionOutcome.DUPLICATE)
            # Invalidate stale callbacks before waiting for queue capacity. The
            # generation describes observed input, not the worker's processing position.
            event = self._publish(descriptor)
            try:
                while self.full() and not self._closed:
                    await self._space_available.wait()
            except asyncio.CancelledError:
                # The observed revision already invalidated older callbacks.
                # Rolling it back would revive them; continuing would suppress
                # retry delivery of an event that never reached the queue.
                self.close()
                raise
            if self.closed:
                return AdmissionResult(AdmissionOutcome.CLOSED)
            self._append(event)
            return AdmissionResult(AdmissionOutcome.ACCEPTED, event)

    async def get(self) -> InboundEvent:
        while not self._items:
            if self._closed:
                raise IngressClosedError("Event ingress is closed")
            await self._items_available.wait()
        event = self._items.popleft()
        self._space_available.set()
        if not self._items:
            self._items_available.clear()
        return event

    def task_done(self) -> None:
        """Acknowledge one consumed observation only after its processing succeeds."""
        # unfinished = queued + consumed-but-unacknowledged. A queued observation
        # must not be acknowledged early, even if another event was handled already.
        if self._unfinished <= len(self._items):
            raise ValueError("task_done() called more often than consumed observations")
        self._unfinished -= 1
        if self._unfinished == 0:
            self._drained.set()

    async def join(self) -> None:
        """Wait for processing ACKs; queue exhaustion/close alone is not completion."""
        # Event.wait() can resume after a new accept/requeue has cleared the event.
        # Recheck the counter so that a stale wake-up cannot report a drained queue.
        while self._unfinished:
            await self._drained.wait()

    def requeue_latest(self) -> bool:
        latest = self._latest_prompt
        if (
            self._closed
            or latest is None
            or self.full()
            or not self.is_current(latest.prompt_token)
        ):
            return False
        # Reprocessing reuses identities; it is not another incoming observation.
        self._append(latest)
        return True

    def close(self) -> None:
        """Idempotently reject further admission and wake waiters, preserving accepted work."""
        self._closed = True
        self._space_available.set()
        self._items_available.set()
