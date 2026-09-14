from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from battle_outbox import BattleEvent, BattleOutboxEnvelope, InvalidBattleOutboxEntry
from battle_records import BattleOutcome
from bounded_values import require_int64
from notifications import NotificationDelivery, NotificationStatus

logger = logging.getLogger("fog_farmer")

BATTLE_NOTIFICATION_NAMESPACE = "application:card-notifications"
BATTLE_NOTIFICATION_SCHEMA_VERSION = 1
CARD_DROP_EVENT_TYPE = "card-drop"


@dataclass(frozen=True, slots=True)
class DrainResult:
    """One bounded consumer pass and any session-wide pause it established."""

    fetched: int
    visited: int
    acknowledged: int
    retry_after_seconds: float | None = None

    def __post_init__(self) -> None:
        require_int64(self.fetched, "fetched", minimum=0)
        require_int64(self.visited, "visited", minimum=0)
        require_int64(self.acknowledged, "acknowledged", minimum=0)
        if not 0 <= self.acknowledged <= self.visited <= self.fetched:
            raise ValueError("Expected acknowledged <= visited <= fetched")
        delay = self.retry_after_seconds
        if delay is not None:
            if isinstance(delay, bool) or not isinstance(delay, (int, float)):
                raise ValueError("retry_after_seconds must be finite and nonnegative")
            try:
                valid = math.isfinite(delay) and delay >= 0
            except OverflowError:
                valid = False
            if not valid:
                raise ValueError("retry_after_seconds must be finite and nonnegative")


class InvalidNotificationEvent(ValueError):
    """A durable event cannot be interpreted by this consumer version."""


class _DeliverySuspended(Exception):
    """The session stopped before this notification entered its external effect."""


def _delivery_allowed_by_default() -> bool:
    return True


class BattleNotificationStore(Protocol):
    async def pending_battle_event_entries(
        self,
        *,
        namespace: str,
        limit: int = 100,
        after_id: int | None = None,
        include_deferred: bool = False,
    ) -> tuple[BattleOutboxEnvelope | InvalidBattleOutboxEntry, ...]: ...

    async def defer_invalid_battle_event(
        self, entry: InvalidBattleOutboxEntry, *, retry_after_seconds: float = 60,
    ) -> bool: ...

    async def extend_battle_event_barrier(
        self, *, namespace: str, blocked_until: datetime,
    ) -> datetime: ...

    async def get_battle_event_barrier(self, *, namespace: str) -> datetime | None: ...

    async def ack_battle_event(self, event_id: int, *, namespace: str) -> bool: ...

    async def fail_battle_event(
        self,
        event_id: int,
        *,
        namespace: str,
        error: str,
        retry_after_seconds: float = 60,
    ) -> bool: ...

    async def get_battle_outcome(self, battle_id: int) -> BattleOutcome | None: ...


class CardDropNotifier(Protocol):
    async def card_drop(
        self, item: str, position: tuple[int, int] | None
    ) -> NotificationDelivery: ...


def _card_event(card_name: str) -> BattleEvent:
    digest = hashlib.sha256(card_name.encode("utf-8")).hexdigest()
    return BattleEvent.from_payload(
        namespace=BATTLE_NOTIFICATION_NAMESPACE,
        idempotency_key=f"card:{digest}",
        event_type=CARD_DROP_EVENT_TYPE,
        schema_version=BATTLE_NOTIFICATION_SCHEMA_VERSION,
        payload={"card_name": card_name},
    )


def events_for(outcome: BattleOutcome) -> tuple[BattleEvent, ...]:
    """Prepare immutable intents to commit atomically with the mandatory outcome.

    One event represents each distinct card name, regardless of quantity or item
    order. Identity is scoped to its battle by the store. Runtime session, time,
    and position are deliberately absent so duplicate replay prepares equal events.
    """
    if not isinstance(outcome, BattleOutcome):
        raise ValueError("outcome must be an immutable BattleOutcome")
    names = sorted({item.name for item in outcome.rewards.items if item.is_card})
    return tuple(_card_event(name) for name in names)


def _positive_delay(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite and positive")
    try:
        valid = math.isfinite(value) and value > 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be finite and positive")
    return float(value)


class BattleNotificationOutbox:
    """Consume durable card intents for one leased application session.

    Delivery is at least once: a crash or ACK failure after Telegram accepts a
    notification can cause a duplicate after restart. The instance lock prevents
    concurrent drains here; multiple instances or processes require an external
    lease. The run method owns no task and must be run by the application's task
    scope.

    A retryable delivery or persistence failure stops its batch. The resulting
    namespace-wide UTC barrier gates every subsequent delivery, including after
    restart and explicit wake requests. Invalid events are quarantined individually
    and do not establish a delivery barrier. A barrier is persisted before retry
    metadata; failure of both writes preserves the full pause in this instance,
    but no storage system can guarantee it survives a crash while writes fail.
    """

    def __init__(
        self,
        store: BattleNotificationStore,
        notifier: CardDropNotifier,
        *,
        retry_base_seconds: float = 30,
        retry_max_seconds: float = 3600,
        delivery_allowed: Callable[[], bool] = _delivery_allowed_by_default,
    ) -> None:
        base = _positive_delay(retry_base_seconds, "retry_base_seconds")
        maximum = _positive_delay(retry_max_seconds, "retry_max_seconds")
        if maximum < base:
            raise ValueError("retry_max_seconds must not be less than retry_base_seconds")
        if not callable(delivery_allowed):
            raise ValueError("delivery_allowed must be callable")
        self.store = store
        self.notifier = notifier
        self.retry_base_seconds = base
        self.retry_max_seconds = maximum
        self._delivery_allowed = delivery_allowed
        self._drain_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._blocked_until = 0.0
        self._running = False

    def wake(self) -> None:
        """Request a prompt drain on the event loop that owns run."""
        self._wake.set()

    def _can_deliver(self) -> bool:
        allowed = self._delivery_allowed()
        if type(allowed) is not bool:
            raise RuntimeError("delivery_allowed must return bool")
        return allowed

    def _backoff(self, attempts: int) -> float:
        cap_exponent = math.log2(self.retry_max_seconds) - math.log2(self.retry_base_seconds)
        if attempts >= cap_exponent:
            return self.retry_max_seconds
        return min(
            self.retry_max_seconds,
            math.ldexp(self.retry_base_seconds, attempts),
        )

    def _consumer_delay(self, delay: float) -> float:
        # Even an explicit zero RetryAfter must yield a bounded pause rather than
        # turning a broken delivery or ACK path into a tight loop.
        return max(delay, min(1.0, self.retry_base_seconds))

    def _establish_block(self, delay: float) -> float:
        consumer_delay = self._consumer_delay(delay)
        loop = asyncio.get_running_loop()
        self._blocked_until = max(self._blocked_until, loop.time() + consumer_delay)
        return max(0.0, self._blocked_until - loop.time())

    def _block_remaining(self) -> float:
        return max(0.0, self._blocked_until - asyncio.get_running_loop().time())

    async def _deliver(self, envelope: BattleOutboxEnvelope) -> NotificationDelivery:
        event = envelope.event
        if event.namespace != BATTLE_NOTIFICATION_NAMESPACE:
            raise InvalidNotificationEvent("Unexpected notification namespace")
        if (
            event.schema_version != BATTLE_NOTIFICATION_SCHEMA_VERSION
            or event.event_type != CARD_DROP_EVENT_TYPE
        ):
            raise InvalidNotificationEvent("Unsupported notification type or schema version")
        payload = event.decoded_payload()
        name = payload.get("card_name")
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise InvalidNotificationEvent("Notification has no canonical card name")
        if event != _card_event(name):
            raise InvalidNotificationEvent(
                "Notification identity or payload does not match its card"
            )
        outcome = await self.store.get_battle_outcome(envelope.battle_id)
        if outcome is None:
            raise InvalidNotificationEvent("Notification has no canonical battle outcome")
        for card in outcome.rewards.items:
            if card.is_card and card.name == name:
                if not self._can_deliver():
                    raise _DeliverySuspended
                delivery = await self.notifier.card_drop(card.name, outcome.position)
                if not isinstance(delivery, NotificationDelivery):
                    raise RuntimeError("Notifier returned an invalid delivery result")
                return delivery
        raise InvalidNotificationEvent(
            "Notification card is absent from the canonical battle outcome"
        )

    async def _defer(
        self,
        envelope: BattleOutboxEnvelope,
        error: str,
        delay: float,
    ) -> bool:
        try:
            persisted = await self.store.fail_battle_event(
                envelope.id,
                namespace=BATTLE_NOTIFICATION_NAMESPACE,
                error=error,
                retry_after_seconds=delay,
            )
        except Exception:
            # The unacknowledged event remains durable even if retry metadata fails.
            logger.exception("Не удалось отложить уведомление события %s", envelope.id)
            return False
        if not persisted:
            logger.error(
                "Хранилище не подтвердило отсрочку уведомления события %s",
                envelope.id,
            )
        return persisted

    async def _defer_delivery(
        self, envelope: BattleOutboxEnvelope, error: str, delay: float,
    ) -> None:
        # Establish memory protection before the first cancellable persistence call.
        # Never replace an authoritative RetryAfter with a shorter fallback.
        self._establish_block(delay)
        try:
            until = datetime.now(UTC) + timedelta(seconds=self._consumer_delay(delay))
            persisted_until = await self.store.extend_battle_event_barrier(
                namespace=BATTLE_NOTIFICATION_NAMESPACE, blocked_until=until,
            )
            remaining = (persisted_until - datetime.now(UTC)).total_seconds()
            if remaining > 0:
                self._establish_block(remaining)
        except Exception:
            logger.exception("Failed to persist notification namespace retry barrier")
        await self._defer(envelope, error, delay)

    def _halted(
        self,
        *,
        fetched: int,
        visited: int,
        acknowledged: int,
        delay: float,
    ) -> DrainResult:
        return DrainResult(
            fetched=fetched,
            visited=visited,
            acknowledged=acknowledged,
            retry_after_seconds=self._establish_block(delay),
        )

    async def drain_pending(self, *, limit: int = 100) -> DrainResult:
        """Attempt one due batch without letting a failing side effect cascade."""
        require_int64(limit, "limit", minimum=1)
        async with self._drain_lock:
            if not self._can_deliver():
                return DrainResult(0, 0, 0)
            remaining = self._block_remaining()
            if remaining > 0:
                return DrainResult(0, 0, 0, remaining)

            barrier = await self.store.get_battle_event_barrier(
                namespace=BATTLE_NOTIFICATION_NAMESPACE,
            )
            if barrier is not None:
                remaining = (barrier - datetime.now(UTC)).total_seconds()
                if remaining > 0:
                    return DrainResult(0, 0, 0, self._establish_block(remaining))

            pending = await self.store.pending_battle_event_entries(
                namespace=BATTLE_NOTIFICATION_NAMESPACE,
                limit=limit,
            )
            fetched = len(pending)
            visited = 0
            acknowledged = 0
            for envelope in pending:
                if not self._can_deliver():
                    return DrainResult(fetched, visited, acknowledged)
                visited += 1
                if isinstance(envelope, InvalidBattleOutboxEntry):
                    logger.error("Corrupt notification event %s: %s", envelope.id,
                                 envelope.decode_error)
                    try:
                        deferred = await self.store.defer_invalid_battle_event(
                            envelope, retry_after_seconds=self.retry_base_seconds,
                        )
                    except Exception:
                        logger.exception("Failed to quarantine notification event %s", envelope.id)
                        deferred = False
                    if not deferred:
                        return self._halted(
                            fetched=fetched, visited=visited, acknowledged=acknowledged,
                            delay=self.retry_base_seconds,
                        )
                    continue
                try:
                    delivery = await self._deliver(envelope)
                except _DeliverySuspended:
                    return DrainResult(fetched, visited, acknowledged)
                except InvalidNotificationEvent as error:
                    if not self._can_deliver():
                        return DrainResult(fetched, visited, acknowledged)
                    logger.exception("Некорректное уведомление события %s", envelope.id)
                    delay = self._backoff(envelope.attempts)
                    if not await self._defer(
                        envelope,
                        f"{type(error).__name__}: {error}",
                        delay,
                    ):
                        return self._halted(
                            fetched=fetched,
                            visited=visited,
                            acknowledged=acknowledged,
                            delay=self.retry_base_seconds,
                        )
                    continue
                except Exception as error:
                    if not self._can_deliver():
                        return DrainResult(fetched, visited, acknowledged)
                    logger.exception("Не доставлено уведомление события %s", envelope.id)
                    delay = self._backoff(envelope.attempts)
                    await self._defer_delivery(
                        envelope, f"{type(error).__name__}: {error}", delay,
                    )
                    return self._halted(
                        fetched=fetched,
                        visited=visited,
                        acknowledged=acknowledged,
                        delay=delay,
                    )

                if delivery.status is NotificationStatus.RETRYABLE_FAILURE:
                    assert delivery.error is not None
                    delay = (
                        self._backoff(envelope.attempts)
                        if delivery.retry_after_seconds is None
                        else delivery.retry_after_seconds
                    )
                    await self._defer_delivery(envelope, delivery.error, delay)
                    return self._halted(
                        fetched=fetched,
                        visited=visited,
                        acknowledged=acknowledged,
                        delay=delay,
                    )

                try:
                    persisted = await self.store.ack_battle_event(
                        envelope.id,
                        namespace=BATTLE_NOTIFICATION_NAMESPACE,
                    )
                except Exception as error:
                    logger.exception(
                        "Не удалось подтвердить уведомление события %s",
                        envelope.id,
                    )
                    delay = self._backoff(envelope.attempts)
                    await self._defer_delivery(
                        envelope, f"{type(error).__name__}: {error}", delay,
                    )
                    return self._halted(
                        fetched=fetched,
                        visited=visited,
                        acknowledged=acknowledged,
                        delay=delay,
                    )
                if not persisted:
                    logger.error(
                        "Хранилище не подтвердило ACK уведомления события %s",
                        envelope.id,
                    )
                    delay = self._backoff(envelope.attempts)
                    await self._defer_delivery(
                        envelope, "Storage did not confirm notification ACK", delay,
                    )
                    return self._halted(
                        fetched=fetched,
                        visited=visited,
                        acknowledged=acknowledged,
                        delay=delay,
                    )
                acknowledged += 1

            return DrainResult(fetched, visited, acknowledged)

    async def run(
        self,
        *,
        poll_interval_seconds: float = 30,
        batch_size: int = 100,
    ) -> None:
        """Drain immediately, on wake, and periodically until cancelled."""
        poll_interval = _positive_delay(
            poll_interval_seconds,
            "poll_interval_seconds",
        )
        require_int64(batch_size, "batch_size", minimum=1)
        if self._running:
            raise RuntimeError("Battle notification consumer is already running")
        self._running = True
        try:
            while True:
                # Clear before draining: a wake during the drain remains visible.
                self._wake.clear()
                try:
                    result = await self.drain_pending(limit=batch_size)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Не удалось прочитать очередь уведомлений")
                    result = DrainResult(
                        0,
                        0,
                        0,
                        self._establish_block(self.retry_base_seconds),
                    )

                if result.retry_after_seconds is not None:
                    # Deliberately ignore wake requests during a global cooldown.
                    await asyncio.sleep(self._block_remaining())
                    continue
                if result.fetched == batch_size:
                    # Drain a backlog without waiting a full polling interval.
                    await asyncio.sleep(0)
                    continue
                try:
                    async with asyncio.timeout(poll_interval):
                        await self._wake.wait()
                except TimeoutError:
                    pass
        finally:
            self._running = False
