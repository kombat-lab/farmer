from __future__ import annotations

import asyncio
import sqlite3
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot

from battle_notification_outbox import (
    BATTLE_NOTIFICATION_NAMESPACE,
    BATTLE_NOTIFICATION_SCHEMA_VERSION,
    BattleNotificationOutbox,
    DrainResult,
    events_for,
)
from battle_outbox import BattleEvent
from battle_records import BattleOutcome, ItemDrop, RewardBundle, SourceEventId
from notifications import NotificationDelivery, NotificationStatus, Notifier
from storage import Storage
from tests.test_notifications import network_error, retry_after


def card_outcome() -> BattleOutcome:
    return BattleOutcome(
        source_event_id=SourceEventId("test:notification:123"),
        source_message_id=123,
        session_id=None,
        target_name="Moth",
        result="VICTORY",
        rewards=RewardBundle(items=(ItemDrop("Moth card", quantity=2, is_card=True),)),
        position=(4, 5),
    )


class CardEventTests(unittest.TestCase):
    def test_preparation_is_immutable_and_independent_of_runtime_context(self) -> None:
        original = card_outcome()
        events = events_for(original)
        replay = replace(original, session_id=9, position=(100, 200))
        self.assertEqual(events, events_for(replay))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].decoded_payload(), {"card_name": "Moth card"})
        self.assertEqual(events[0].namespace, BATTLE_NOTIFICATION_NAMESPACE)
        self.assertEqual(events[0].schema_version, BATTLE_NOTIFICATION_SCHEMA_VERSION)
        detached = events[0].decoded_payload()
        detached["card_name"] = "mutated"
        self.assertEqual(events, events_for(original))
        with self.assertRaises(FrozenInstanceError):
            events[0].payload_json = "{}"

    def test_events_are_distinct_per_card_name_and_stable_across_item_order(self) -> None:
        items = (
            ItemDrop("B card", is_card=True),
            ItemDrop("A card", is_card=True),
            ItemDrop("B card", quantity=3, is_card=True),
            ItemDrop("dust"),
        )
        first = replace(card_outcome(), rewards=RewardBundle(items=items))
        second = replace(first, rewards=RewardBundle(items=tuple(reversed(items))))
        events = events_for(first)
        self.assertEqual(events, events_for(second))
        self.assertEqual(len(events), 2)
        self.assertEqual(len({event.idempotency_key for event in events}), 2)
        self.assertEqual(
            [event.decoded_payload()["card_name"] for event in events], ["A card", "B card"]
        )
        self.assertEqual(events_for(replace(first, rewards=RewardBundle())), ())

    def test_retry_configuration_is_validated(self) -> None:
        for values in (
            {"retry_base_seconds": 0},
            {"retry_base_seconds": True},
            {"retry_base_seconds": float("nan")},
            {"retry_max_seconds": float("inf")},
            {"retry_max_seconds": 1},
            {"retry_base_seconds": 10**1000},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                BattleNotificationOutbox(MagicMock(), MagicMock(), **values)


class BattleNotificationOutboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "notifications.sqlite3"
        self.store = Storage(self.path)
        self.outcome = card_outcome()
        self.notifier = MagicMock(spec=Notifier)
        self.notifier.card_drop = AsyncMock(
            return_value=NotificationDelivery(NotificationStatus.SENT)
        )
        self.outbox = BattleNotificationOutbox(
            self.store, self.notifier, retry_base_seconds=2, retry_max_seconds=5
        )

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self.directory.cleanup()

    async def record(self, events: tuple[BattleEvent, ...] | None = None) -> int:
        result = await self.store.record_battle_outcome(
            self.outcome, events=events_for(self.outcome) if events is None else events
        )
        return result.battle_id

    async def pending(self):
        return await self.store.pending_battle_events(
            namespace=BATTLE_NOTIFICATION_NAMESPACE, include_deferred=True
        )

    def make_due(self) -> None:
        self.store.connection.execute("UPDATE battle_outbox SET next_attempt_at=NULL")
        self.store.connection.execute("DELETE FROM battle_outbox_barriers")
        self.store.connection.commit()
        self.outbox._blocked_until = 0.0

    async def assert_deferred(self, seconds: float, attempts: int = 1) -> None:
        pending = await self.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].attempts, attempts)
        delay = datetime.fromisoformat(pending[0].next_attempt_at) - datetime.now(UTC)
        self.assertAlmostEqual(delay.total_seconds(), seconds, delta=1)
        self.assertIsNotNone(pending[0].last_error)
        self.assertEqual(
            await self.store.pending_battle_events(namespace=BATTLE_NOTIFICATION_NAMESPACE), ()
        )

    async def test_prepared_events_commit_atomically_with_battle(self) -> None:
        self.store.connection.executescript("""
            CREATE TRIGGER reject_notification BEFORE INSERT ON battle_outbox
            BEGIN SELECT RAISE(ABORT, 'offline event insert failure'); END;
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            await self.record()
        self.assertEqual(await self.pending(), ())
        count = self.store.connection.execute("SELECT count(*) FROM battles").fetchone()[0]
        self.assertEqual(count, 0)
        self.notifier.card_drop.assert_not_awaited()

    async def test_delivery_gate_blocks_before_reading_pending_batch(self) -> None:
        await self.record()
        outbox = BattleNotificationOutbox(
            self.store,
            self.notifier,
            delivery_allowed=lambda: False,
        )
        with patch.object(
            self.store,
            "pending_battle_event_entries",
            wraps=self.store.pending_battle_event_entries,
        ) as pending:
            result = await outbox.drain_pending()

        self.assertEqual(result, DrainResult(0, 0, 0))
        pending.assert_not_awaited()
        self.notifier.card_drop.assert_not_awaited()
        self.assertEqual(len(await self.pending()), 1)

    async def test_delivery_gate_rechecks_immediately_before_external_send(self) -> None:
        await self.record()
        allowed = True
        canonical = await self.store.get_battle_outcome(1)
        self.assertIsNotNone(canonical)

        async def stop_during_lookup(_battle_id: int):
            nonlocal allowed
            allowed = False
            return canonical

        outbox = BattleNotificationOutbox(
            self.store,
            self.notifier,
            delivery_allowed=lambda: allowed,
        )
        with patch.object(
            self.store,
            "get_battle_outcome",
            side_effect=stop_during_lookup,
        ):
            result = await outbox.drain_pending()

        self.assertEqual(result, DrainResult(1, 1, 0))
        self.notifier.card_drop.assert_not_awaited()
        pending = await self.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].attempts, 0)
        self.assertIsNone(pending[0].last_error)

        allowed = True
        self.assertEqual((await outbox.drain_pending()).acknowledged, 1)
        self.notifier.card_drop.assert_awaited_once()

    async def test_success_ack_uses_original_ledger_position_after_duplicate_replay(self) -> None:
        await self.record()
        replay = replace(self.outcome, session_id=999, position=(90, 91))
        await self.store.record_battle_outcome(replay, events=events_for(replay))
        self.assertEqual((await self.outbox.drain_pending()).acknowledged, 1)
        self.notifier.card_drop.assert_awaited_once_with("Moth card", (4, 5))
        self.assertEqual(await self.pending(), ())
        self.assertEqual((await self.outbox.drain_pending()).acknowledged, 0)
        self.notifier.card_drop.assert_awaited_once()

    async def test_retry_after_preserves_exact_delay_beyond_local_cap(self) -> None:
        await self.record()
        bot = MagicMock(spec=Bot)
        bot.send_rich_message = AsyncMock(side_effect=retry_after(75))
        outbox = BattleNotificationOutbox(
            self.store, Notifier(bot, 1), retry_base_seconds=2, retry_max_seconds=5
        )
        with self.assertLogs("fog_farmer", level="WARNING"):
            self.assertEqual((await outbox.drain_pending()).acknowledged, 0)
        await self.assert_deferred(75)
        bot.send_message.assert_not_called()

    async def test_network_failure_stays_pending_with_bounded_exponential_backoff(self) -> None:
        await self.record()
        bot = MagicMock(spec=Bot)
        bot.send_rich_message = AsyncMock(side_effect=network_error())
        outbox = BattleNotificationOutbox(
            self.store, Notifier(bot, 1), retry_base_seconds=2, retry_max_seconds=5
        )
        for attempt, delay in enumerate((2, 4, 5, 5), start=1):
            self.make_due()
            outbox._blocked_until = 0.0
            with self.assertLogs("fog_farmer", level="WARNING"):
                self.assertEqual((await outbox.drain_pending()).acknowledged, 0)
            await self.assert_deferred(delay, attempt)
        bot.send_message.assert_not_called()
        self.assertEqual(bot.send_rich_message.await_count, 4)

    async def test_due_pending_notification_survives_store_and_consumer_restart(self) -> None:
        await self.record()
        self.notifier.card_drop.return_value = NotificationDelivery(
            NotificationStatus.RETRYABLE_FAILURE, error="network down"
        )
        self.assertEqual((await self.outbox.drain_pending()).acknowledged, 0)
        await self.store.close()
        self.store = Storage(self.path)
        self.outbox = BattleNotificationOutbox(self.store, self.notifier)
        self.assertEqual((await self.outbox.drain_pending()).acknowledged, 0)
        self.notifier.card_drop.assert_awaited_once()
        self.make_due()
        self.notifier.card_drop.return_value = NotificationDelivery(NotificationStatus.SENT)
        self.assertEqual((await self.outbox.drain_pending()).acknowledged, 1)
        self.assertEqual(self.notifier.card_drop.await_count, 2)
        self.assertEqual(await self.pending(), ())

    async def test_concurrent_drains_of_one_instance_deliver_once(self) -> None:
        await self.record()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def deliver(*args: object) -> NotificationDelivery:
            entered.set()
            await release.wait()
            return NotificationDelivery(NotificationStatus.SENT)

        self.notifier.card_drop.side_effect = deliver
        first = asyncio.create_task(self.outbox.drain_pending())
        await entered.wait()
        second = asyncio.create_task(self.outbox.drain_pending())
        await asyncio.sleep(0)
        self.notifier.card_drop.assert_awaited_once()
        release.set()
        self.assertEqual(
            await asyncio.gather(first, second),
            [DrainResult(1, 1, 1), DrainResult(0, 0, 0)],
        )
        self.assertEqual(await self.pending(), ())

    async def test_future_or_malformed_event_is_retained_and_deferred(self) -> None:
        valid = events_for(self.outcome)[0]
        malformed = (
            replace(valid, schema_version=2),
            replace(valid, event_type="future-kind"),
            replace(valid, idempotency_key="wrong"),
            replace(valid, payload_json="{}"),
            replace(valid, payload_json='{"card_name":7}'),
            replace(valid, payload_json='{"card_name":"Moth card","position":[8,9]}'),
        )
        # Separate source battles let identical per-card keys exercise each variant.
        for index, event in enumerate(malformed, start=1):
            message_id = 123 + index
            self.outcome = replace(
                self.outcome,
                source_event_id=SourceEventId(f"test:notification:{message_id}"),
                source_message_id=message_id,
            )
            await self.record((event,))
        with self.assertLogs("fog_farmer", level="ERROR"):
            self.assertEqual((await self.outbox.drain_pending()).acknowledged, 0)
        pending = await self.pending()
        self.assertEqual(len(pending), len(malformed))
        self.assertEqual(tuple(row.event for row in pending), malformed)
        self.assertTrue(all(row.attempts == 1 and row.next_attempt_at for row in pending))
        self.notifier.card_drop.assert_not_awaited()

    async def test_absent_card_or_outcome_is_retained(self) -> None:
        wrong_card = events_for(
            replace(
                self.outcome, rewards=RewardBundle(items=(ItemDrop("Other card", is_card=True),))
            )
        )
        await self.record(wrong_card)
        with self.assertLogs("fog_farmer", level="ERROR"):
            self.assertEqual((await self.outbox.drain_pending()).acknowledged, 0)
        await self.assert_deferred(2)
        self.make_due()
        with (
            patch.object(self.store, "get_battle_outcome", return_value=None),
            self.assertLogs("fog_farmer", level="ERROR"),
        ):
            self.assertEqual((await self.outbox.drain_pending()).acknowledged, 0)
        await self.assert_deferred(4, 2)
        self.notifier.card_drop.assert_not_awaited()

    async def test_ack_failure_can_repeat_delivery_after_backoff(self) -> None:
        await self.record()
        with (
            patch.object(self.store, "ack_battle_event", side_effect=RuntimeError("ACK failed")),
            self.assertLogs("fog_farmer", level="ERROR"),
        ):
            self.assertEqual((await self.outbox.drain_pending()).acknowledged, 0)
        await self.assert_deferred(2)
        self.make_due()
        self.outbox._blocked_until = 0.0
        self.assertEqual((await self.outbox.drain_pending()).acknowledged, 1)
        self.assertEqual(self.notifier.card_drop.await_count, 2)
        self.assertEqual(await self.pending(), ())

    async def test_invalid_notifier_result_cannot_ack_event(self) -> None:
        await self.record()
        self.notifier.card_drop.return_value = None
        with self.assertLogs("fog_farmer", level="ERROR"):
            self.assertEqual((await self.outbox.drain_pending()).acknowledged, 0)
        await self.assert_deferred(2)

    async def test_cancellation_keeps_event_pending_and_releases_instance_lock(self) -> None:
        await self.record()
        self.notifier.card_drop.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.outbox.drain_pending()
        self.assertEqual(len(await self.pending()), 1)
        self.assertEqual((await self.pending())[0].attempts, 0)
        self.notifier.card_drop.side_effect = None
        self.assertEqual((await self.outbox.drain_pending()).acknowledged, 1)

    async def test_failure_to_persist_retry_metadata_does_not_ack_or_hot_loop(self) -> None:
        await self.record()
        self.notifier.card_drop.return_value = NotificationDelivery(
            NotificationStatus.RETRYABLE_FAILURE, error="network down"
        )
        with (
            patch.object(self.store, "fail_battle_event", side_effect=RuntimeError("disk full")),
            self.assertLogs("fog_farmer", level="ERROR"),
        ):
            self.assertEqual((await self.outbox.drain_pending()).acknowledged, 0)
        self.assertEqual(len(await self.pending()), 1)
        self.notifier.card_drop.assert_awaited_once()

    async def test_bad_event_does_not_block_another_due_notification(self) -> None:
        original = events_for(self.outcome)[0]
        await self.record((replace(original, schema_version=2),))
        self.outcome = replace(
            self.outcome,
            source_event_id=SourceEventId("test:notification:999"),
            source_message_id=999,
        )
        await self.record()
        with self.assertLogs("fog_farmer", level="ERROR"):
            self.assertEqual((await self.outbox.drain_pending()).acknowledged, 1)
        self.notifier.card_drop.assert_awaited_once()
        await self.assert_deferred(2)

    async def test_batch_limit_bounds_work_and_other_namespace_is_untouched(self) -> None:
        cards = (ItemDrop("A card", is_card=True), ItemDrop("B card", is_card=True))
        self.outcome = replace(self.outcome, rewards=RewardBundle(items=cards))
        foreign = replace(events_for(self.outcome)[0], namespace="other-consumer")
        await self.record((*events_for(self.outcome), foreign))
        self.assertEqual((await self.outbox.drain_pending(limit=1)).acknowledged, 1)
        self.assertEqual(len(await self.pending()), 1)
        self.assertEqual((await self.outbox.drain_pending(limit=1)).acknowledged, 1)
        self.assertEqual(len(await self.store.pending_battle_events(namespace="other-consumer")), 1)

    def test_drain_result_is_frozen_and_strictly_validated(self) -> None:
        result = DrainResult(3, 2, 1, 0)
        self.assertEqual((result.fetched, result.visited, result.acknowledged), (3, 2, 1))
        with self.assertRaises(FrozenInstanceError):
            result.acknowledged = 2
        for values in (
            (-1, 0, 0),
            (1, 2, 0),
            (1, 1, 2),
            (True, 0, 0),
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                DrainResult(*values)
        for delay in (-1, True, float("nan"), float("inf"), "1"):
            with self.subTest(delay=delay), self.assertRaises(ValueError):
                DrainResult(0, 0, 0, delay)

    async def test_retryable_delivery_stops_the_batch(self) -> None:
        self.outcome = replace(
            self.outcome,
            rewards=RewardBundle(
                items=(
                    ItemDrop("A card", is_card=True),
                    ItemDrop("B card", is_card=True),
                )
            ),
        )
        await self.record()
        self.notifier.card_drop.return_value = NotificationDelivery(
            NotificationStatus.RETRYABLE_FAILURE,
            error="network down",
        )

        result = await self.outbox.drain_pending()

        self.assertEqual(
            (result.fetched, result.visited, result.acknowledged),
            (2, 1, 0),
        )
        self.assertIsNotNone(result.retry_after_seconds)
        self.notifier.card_drop.assert_awaited_once_with("A card", (4, 5))
        pending = await self.pending()
        self.assertEqual([row.attempts for row in pending], [1, 0])

    async def test_retry_after_is_session_wide_and_wake_cannot_bypass_it(self) -> None:
        await self.record()
        self.notifier.card_drop.return_value = NotificationDelivery(
            NotificationStatus.RETRYABLE_FAILURE,
            retry_after_seconds=0.05,
            error="rate limited",
        )

        first = await self.outbox.drain_pending()
        self.outbox.wake()
        second = await self.outbox.drain_pending()

        self.assertGreater(first.retry_after_seconds or 0, 0)
        self.assertEqual(second.fetched, 0)
        self.assertGreater(second.retry_after_seconds or 0, 0)
        self.notifier.card_drop.assert_awaited_once()

    async def test_ack_failure_stops_before_sending_the_rest_of_batch(self) -> None:
        self.outcome = replace(
            self.outcome,
            rewards=RewardBundle(
                items=(
                    ItemDrop("A card", is_card=True),
                    ItemDrop("B card", is_card=True),
                )
            ),
        )
        await self.record()
        with (
            patch.object(
                self.store,
                "ack_battle_event",
                side_effect=RuntimeError("ACK unavailable"),
            ),
            self.assertLogs("fog_farmer", level="ERROR"),
        ):
            result = await self.outbox.drain_pending()

        self.assertEqual(
            (result.fetched, result.visited, result.acknowledged),
            (2, 1, 0),
        )
        self.assertIsNotNone(result.retry_after_seconds)
        self.notifier.card_drop.assert_awaited_once_with("A card", (4, 5))

    async def test_failed_defer_stops_before_sending_the_rest_of_batch(self) -> None:
        self.outcome = replace(
            self.outcome,
            rewards=RewardBundle(
                items=(
                    ItemDrop("A card", is_card=True),
                    ItemDrop("B card", is_card=True),
                )
            ),
        )
        await self.record()
        self.notifier.card_drop.return_value = NotificationDelivery(
            NotificationStatus.RETRYABLE_FAILURE,
            error="network down",
        )
        with (
            patch.object(
                self.store,
                "fail_battle_event",
                side_effect=RuntimeError("disk unavailable"),
            ),
            self.assertLogs("fog_farmer", level="ERROR"),
        ):
            result = await self.outbox.drain_pending()

        self.assertEqual(
            (result.fetched, result.visited, result.acknowledged),
            (2, 1, 0),
        )
        self.assertIsNotNone(result.retry_after_seconds)
        self.notifier.card_drop.assert_awaited_once_with("A card", (4, 5))
        self.assertTrue(all(row.attempts == 0 for row in await self.pending()))

    async def test_run_drains_immediately_and_wakes_for_a_new_intent(self) -> None:
        acknowledged = asyncio.Event()
        original_ack = self.store.ack_battle_event

        async def acknowledge(event_id: int, *, namespace: str) -> bool:
            result = await original_ack(event_id, namespace=namespace)
            acknowledged.set()
            return result

        await self.record()
        with patch.object(
            self.store,
            "ack_battle_event",
            side_effect=acknowledge,
        ):
            runner = asyncio.create_task(
                self.outbox.run(poll_interval_seconds=60),
                name="test-notification-consumer",
            )
            try:
                await asyncio.wait_for(acknowledged.wait(), timeout=1)
                self.assertEqual(await self.pending(), ())

                acknowledged.clear()
                self.outcome = replace(
                    self.outcome,
                    source_event_id=SourceEventId("test:notification:124"),
                    source_message_id=124,
                )
                await self.record()
                self.outbox.wake()
                await asyncio.wait_for(acknowledged.wait(), timeout=1)
                self.assertEqual(await self.pending(), ())
                self.assertEqual(self.notifier.card_drop.await_count, 2)
            finally:
                runner.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await runner

    async def test_run_backs_off_after_queue_read_failure_and_propagates_cancel(self) -> None:
        pending = AsyncMock(side_effect=RuntimeError("database unavailable"))
        outbox = BattleNotificationOutbox(
            self.store,
            self.notifier,
            retry_base_seconds=0.05,
            retry_max_seconds=0.1,
        )
        with (
            patch.object(self.store, "pending_battle_event_entries", pending),
            self.assertLogs("fog_farmer", level="ERROR"),
        ):
            runner = asyncio.create_task(
                outbox.run(poll_interval_seconds=0.001),
                name="test-failing-notification-consumer",
            )
            try:
                await asyncio.sleep(0.01)
                self.assertEqual(pending.await_count, 1)
                outbox.wake()
                await asyncio.sleep(0.01)
                self.assertEqual(pending.await_count, 1)
            finally:
                runner.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await runner

    async def test_run_rejects_concurrent_ownership(self) -> None:
        runner = asyncio.create_task(
            self.outbox.run(poll_interval_seconds=60),
            name="test-owned-notification-consumer",
        )
        try:
            await asyncio.sleep(0)
            with self.assertRaises(RuntimeError):
                await self.outbox.run(poll_interval_seconds=60)
        finally:
            runner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await runner


if __name__ == "__main__":
    unittest.main()
