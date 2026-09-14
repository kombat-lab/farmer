from __future__ import annotations

import asyncio
import sqlite3
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

from battle_notification_outbox import (
    BATTLE_NOTIFICATION_NAMESPACE,
    BattleNotificationOutbox,
    events_for,
)
from battle_outbox import BattleOutboxEnvelope, InvalidBattleOutboxEntry
from battle_records import BattleOutcome, ItemDrop, RewardBundle, SourceEventId
from legacy_combat_diagnostics import LEGACY_DIAGNOSTICS_NAMESPACE, LegacyCombatDiagnostics
from notifications import NotificationDelivery, NotificationStatus, Notifier
from storage import SCHEMA_VERSION, Storage
from tests.test_legacy_combat_diagnostics import legacy_trace


class DurableOutboxRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "isolated.sqlite3"
        self.store = Storage(self.path)
        self.outcome = BattleOutcome(
            source_event_id=SourceEventId("test:durable:10"),
            source_message_id=10,
            session_id=None,
            target_name="Moth",
            result="VICTORY",
            rewards=RewardBundle(items=(
                ItemDrop("A card", is_card=True), ItemDrop("B card", is_card=True),
            )),
        )
        self.notifier = MagicMock(spec=Notifier)
        self.notifier.card_drop = AsyncMock(
            return_value=NotificationDelivery(NotificationStatus.SENT),
        )
        self.outbox = BattleNotificationOutbox(self.store, self.notifier)
        self.diagnostics = LegacyCombatDiagnostics(self.store)

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self.directory.cleanup()

    async def record_cards(self) -> None:
        await self.store.record_battle_outcome(self.outcome, events=events_for(self.outcome))

    def rate_limit(self) -> None:
        self.notifier.card_drop.return_value = NotificationDelivery(
            NotificationStatus.RETRYABLE_FAILURE, error="rate limit", retry_after_seconds=7200,
        )

    async def test_retry_after_gates_later_due_events_across_database_restart(self) -> None:
        await self.record_cards()
        self.rate_limit()
        first = await self.outbox.drain_pending()
        self.assertGreater(first.retry_after_seconds or 0, 7190)
        await self.store.close()
        self.store = Storage(self.path)
        restarted = BattleNotificationOutbox(self.store, self.notifier)
        restarted.wake()
        second = await restarted.drain_pending()
        self.assertEqual(second.fetched, 0)
        self.assertGreater(second.retry_after_seconds or 0, 7190)
        self.notifier.card_drop.assert_awaited_once()
        pending = await self.store.pending_battle_events(
            namespace=BATTLE_NOTIFICATION_NAMESPACE, include_deferred=True,
        )
        self.assertEqual([row.attempts for row in pending], [1, 0])

    async def test_metadata_failure_never_shortens_retry_after_and_restart_is_blocked(self) -> None:
        await self.record_cards()
        self.rate_limit()
        with (
            patch.object(self.store, "fail_battle_event", return_value=False),
            self.assertLogs("fog_farmer", level="ERROR"),
        ):
            result = await self.outbox.drain_pending()
        self.assertGreater(result.retry_after_seconds or 0, 7190)
        restarted = BattleNotificationOutbox(self.store, self.notifier)
        second = await restarted.drain_pending()
        self.assertGreater(second.retry_after_seconds or 0, 7190)
        self.notifier.card_drop.assert_awaited_once()

    async def test_all_writes_failed_preserves_full_in_memory_pause(self) -> None:
        await self.record_cards()
        self.rate_limit()
        with (
            patch.object(self.store, "extend_battle_event_barrier", side_effect=OSError("disk")),
            patch.object(self.store, "fail_battle_event", side_effect=OSError("disk")),
            self.assertLogs("fog_farmer", level="ERROR"),
        ):
            first = await self.outbox.drain_pending()
        self.assertGreater(first.retry_after_seconds or 0, 7190)
        self.assertIsNone(await self.store.get_battle_event_barrier(
            namespace=BATTLE_NOTIFICATION_NAMESPACE,
        ))
        self.outbox.wake()
        self.assertEqual((await self.outbox.drain_pending()).fetched, 0)
        self.notifier.card_drop.assert_awaited_once()
        # A restart guarantee is impossible if every durable write failed.

    async def test_cancellation_during_retry_persistence_does_not_clear_memory_block(self) -> None:
        await self.record_cards()
        self.rate_limit()
        with patch.object(
            self.store, "extend_battle_event_barrier", side_effect=asyncio.CancelledError,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.outbox.drain_pending()
        result = await self.outbox.drain_pending()
        self.assertGreater(result.retry_after_seconds or 0, 7190)
        self.notifier.card_drop.assert_awaited_once()

    async def test_barrier_is_utc_monotonic_and_shared_between_connections(self) -> None:
        first = datetime.now(UTC) + timedelta(hours=2)
        other = Storage(self.path)
        try:
            stored = await self.store.extend_battle_event_barrier(
                namespace=" shared ", blocked_until=first.astimezone(timezone(timedelta(hours=3))),
            )
            self.assertEqual(stored.tzinfo, UTC)
            self.assertEqual(await other.extend_battle_event_barrier(
                namespace="shared", blocked_until=first - timedelta(hours=1),
            ), stored)
            later = first + timedelta(hours=1)
            await other.extend_battle_event_barrier(namespace="shared", blocked_until=later)
            self.assertEqual(await self.store.get_battle_event_barrier(namespace="shared"), later)
            with self.assertRaises(ValueError):
                await self.store.extend_battle_event_barrier(
                    namespace="shared", blocked_until=datetime.now(),
                )
            with self.assertRaises(ValueError):
                await self.store.extend_battle_event_barrier(namespace=" ", blocked_until=first)
        finally:
            await other.close()

    async def test_barrier_trigger_failure_rolls_back_without_shortening(self) -> None:
        original = datetime.now(UTC) + timedelta(hours=2)
        await self.store.extend_battle_event_barrier(namespace="n", blocked_until=original)
        self.store.connection.executescript("""
            CREATE TRIGGER reject_barrier AFTER UPDATE ON battle_outbox_barriers
            BEGIN SELECT RAISE(FAIL, 'blocked'); END;
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            await self.store.extend_battle_event_barrier(
                namespace="n", blocked_until=original + timedelta(hours=1),
            )
        self.assertFalse(self.store.connection.in_transaction)
        self.assertEqual(await self.store.get_battle_event_barrier(namespace="n"), original)

    async def test_upgrade_preserves_v3_retry_deadline_as_namespace_barrier(self) -> None:
        await self.record_cards()
        rows = await self.store.pending_battle_events(namespace=BATTLE_NOTIFICATION_NAMESPACE)
        await self.store.fail_battle_event(
            rows[0].id, namespace=BATTLE_NOTIFICATION_NAMESPACE,
            error="rate", retry_after_seconds=7200,
        )
        self.store.connection.execute("DROP TABLE battle_outbox_barriers")
        self.store.connection.execute("PRAGMA user_version=3")
        self.store.connection.commit()
        await self.store.close()
        self.store = Storage(self.path)
        self.assertEqual(self.store.connection.execute("PRAGMA user_version").fetchone()[0],
                         SCHEMA_VERSION)
        consumer = BattleNotificationOutbox(self.store, self.notifier)
        self.assertGreater((await consumer.drain_pending()).retry_after_seconds or 0, 7190)
        self.notifier.card_drop.assert_not_awaited()

    async def test_corrupt_head_notification_is_deferred_and_healthy_row_delivers(self) -> None:
        await self.record_cards()
        self.store.connection.execute(
            "UPDATE battle_outbox SET payload_json='[bad' WHERE id=1",
        )
        self.store.connection.commit()
        with self.assertLogs("fog_farmer", level="ERROR"):
            result = await self.outbox.drain_pending()
        self.assertEqual((result.fetched, result.visited, result.acknowledged), (2, 2, 1))
        self.notifier.card_drop.assert_awaited_once_with("B card", None)
        self.assertEqual((await self.outbox.drain_pending()).fetched, 0)
        row = self.store.connection.execute("SELECT * FROM battle_outbox WHERE id=1").fetchone()
        self.assertIsNone(row["acknowledged_at"])
        self.assertEqual(row["payload_json"], "[bad")
        self.assertEqual(row["attempts"], 1)
        self.assertIn("JSONDecodeError", row["last_error"])

    async def test_corrupt_numeric_metadata_is_not_coerced_or_repaired(self) -> None:
        await self.record_cards()
        self.store.connection.execute("UPDATE battle_outbox SET attempts=1.5 WHERE id=1")
        self.store.connection.commit()
        entries = await self.store.pending_battle_event_entries(
            namespace=BATTLE_NOTIFICATION_NAMESPACE,
        )
        self.assertIsInstance(entries[0], InvalidBattleOutboxEntry)
        self.assertIsInstance(entries[1], BattleOutboxEnvelope)
        with self.assertLogs("fog_farmer", level="ERROR"):
            self.assertEqual((await self.outbox.drain_pending()).acknowledged, 1)
        self.assertEqual(self.store.connection.execute(
            "SELECT attempts FROM battle_outbox WHERE id=1",
        ).fetchone()[0], 1.5)
        self.assertEqual(await self.store.pending_battle_event_entries(
            namespace=BATTLE_NOTIFICATION_NAMESPACE,
        ), ())

    async def test_invalid_timestamp_is_exposed_instead_of_being_hidden_by_sql_filter(self) -> None:
        await self.record_cards()
        self.store.connection.execute(
            "UPDATE battle_outbox SET next_attempt_at='not-a-timestamp' WHERE id=1",
        )
        self.store.connection.commit()
        with self.assertLogs("fog_farmer", level="ERROR"):
            self.assertEqual((await self.outbox.drain_pending()).acknowledged, 1)
        row = self.store.connection.execute("SELECT * FROM battle_outbox WHERE id=1").fetchone()
        self.assertIsNotNone(row["next_attempt_at"])
        self.assertIsNotNone(row["last_error"])
        self.assertIsNone(row["acknowledged_at"])

    async def test_signed_corrupt_sqlite_row_id_can_be_quarantined_without_coercion(self) -> None:
        await self.record_cards()
        self.store.connection.execute("UPDATE battle_outbox SET id=-1 WHERE id=1")
        self.store.connection.commit()
        entries = await self.store.pending_battle_event_entries(
            namespace=BATTLE_NOTIFICATION_NAMESPACE,
        )
        self.assertEqual(entries[0].id, -1)
        with self.assertLogs("fog_farmer", level="ERROR"):
            self.assertEqual((await self.outbox.drain_pending()).acknowledged, 1)
        self.assertIsNotNone(self.store.connection.execute(
            "SELECT last_error FROM battle_outbox WHERE id=-1",
        ).fetchone()[0])

    async def test_corrupt_diagnostic_head_does_not_block_healthy_projection(self) -> None:
        with patch.object(self.diagnostics, "initialize", return_value=None):
            await self.diagnostics.record_payloads(self.outcome, combat_decisions=(legacy_trace(),))
            await self.diagnostics.record_payloads(
                replace(
                    self.outcome,
                    source_event_id=SourceEventId("test:durable:11"),
                    source_message_id=11,
                ),
                combat_decisions=(legacy_trace(),),
            )
        self.store.connection.execute("UPDATE battle_outbox SET payload_json='[bad' WHERE id=1")
        self.store.connection.commit()
        with self.assertLogs("fog_farmer", level="ERROR"):
            self.assertEqual(await self.diagnostics.drain_pending(), 1)
        self.assertEqual(await self.diagnostics.drain_pending(), 0)
        self.assertEqual(len(await self.diagnostics.learning_stats()), 1)
        rows = await self.store.pending_battle_event_entries(
            namespace=LEGACY_DIAGNOSTICS_NAMESPACE, include_deferred=True,
        )
        self.assertEqual(len(rows), 1)
        self.assertIsInstance(rows[0], InvalidBattleOutboxEntry)

    async def test_ignored_analysis_insert_cannot_ack_and_successful_retry_does(self) -> None:
        await self.diagnostics.initialize()
        self.store.connection.executescript("""
            CREATE TRIGGER ignore_analysis BEFORE INSERT ON combat_battle_analysis
            BEGIN SELECT RAISE(IGNORE); END;
        """)
        with self.assertLogs("fog_farmer", level="ERROR"):
            result = await self.diagnostics.record_payloads(
                self.outcome, combat_decisions=(legacy_trace(),),
            )
        self.assertTrue(result.inserted)
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM combat_battle_analysis",
        ).fetchone()[0], 0)
        pending = await self.store.pending_battle_events(
            namespace=LEGACY_DIAGNOSTICS_NAMESPACE, include_deferred=True,
        )
        self.assertEqual(len(pending), 1)
        self.store.connection.execute("DROP TRIGGER ignore_analysis")
        self.store.connection.commit()
        self.assertEqual(await self.diagnostics.drain_pending(include_deferred=True), 1)
        self.assertEqual(self.store.connection.execute(
            "SELECT total_actions FROM combat_battle_analysis",
        ).fetchone()[0], 1)

    async def test_extended_trace_cannot_ack_against_stale_projection(self) -> None:
        await self.diagnostics.record_payloads(self.outcome, combat_decisions=(legacy_trace(),))
        with self.assertLogs("fog_farmer", level="ERROR"):
            await self.diagnostics.record_payloads(
                self.outcome,
                combat_decisions=(legacy_trace(), legacy_trace(telegram_message_id=11)),
            )
        rows = await self.store.pending_battle_events(
            namespace=LEGACY_DIAGNOSTICS_NAMESPACE, include_deferred=True,
        )
        self.assertEqual(len(rows), 1)
        self.assertIn("Conflicting diagnostic analysis content", rows[0].last_error)
        self.assertEqual(self.store.connection.execute(
            "SELECT total_actions FROM combat_battle_analysis",
        ).fetchone()[0], 1)
        # Explicitly rebuilding the stale projection permits the durable retry.
        self.store.connection.execute("DELETE FROM combat_battle_analysis")
        self.store.connection.commit()
        self.assertEqual(await self.diagnostics.drain_pending(include_deferred=True), 1)
        self.assertEqual(self.store.connection.execute(
            "SELECT total_actions FROM combat_battle_analysis",
        ).fetchone()[0], 2)

    async def test_existing_analysis_compares_content_not_just_action_count(self) -> None:
        with (
            patch.object(self.store, "ack_battle_event", side_effect=OSError("ack")),
            self.assertLogs("fog_farmer", level="ERROR"),
        ):
            await self.diagnostics.record_payloads(self.outcome, combat_decisions=(legacy_trace(),))
        self.store.connection.execute("UPDATE combat_battle_analysis SET target_name='wrong'")
        self.store.connection.commit()
        with self.assertLogs("fog_farmer", level="ERROR"):
            self.assertEqual(await self.diagnostics.drain_pending(include_deferred=True), 0)
        pending = await self.store.pending_battle_events(
            namespace=LEGACY_DIAGNOSTICS_NAMESPACE, include_deferred=True,
        )
        self.assertEqual(len(pending), 1)
        self.assertIn("Conflicting diagnostic analysis content", pending[0].last_error)


if __name__ == "__main__":
    unittest.main()
