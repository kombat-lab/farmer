from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from battle_notification_outbox import (
    BATTLE_NOTIFICATION_NAMESPACE,
    BattleNotificationOutbox,
)
from notifications import NotificationDelivery, NotificationStatus, Notifier
from settings_service import SettingsService
from storage import Storage
from tests.combat_runtime_harness import Message
from tests.legacy_fog_factory import legacy_farmer, legacy_runtime


class BattleOutcomeNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.storage = Storage(Path(":memory:"))
        self.settings = SettingsService(self.storage)
        await self.settings.load()
        self.notifier = MagicMock(spec=Notifier)
        self.notifier.card_drop = AsyncMock(
            return_value=NotificationDelivery(NotificationStatus.SENT)
        )
        self.client = MagicMock()
        self.client.disconnect = AsyncMock()
        with patch("tests.legacy_fog_factory.create_test_client", return_value=self.client):
            self.farmer = legacy_farmer(self.storage, self.notifier, self.settings)
        self.legacy = legacy_runtime(self.farmer)
        self.farmer.session_id = await self.storage.start_session(
            cycles_count=1,
            moves_per_cycle=10,
        )
        self.legacy.context.active_target = "Моль"

    async def asyncTearDown(self) -> None:
        await self.farmer.stop("test cleanup")
        await self.storage.close()

    async def dispatch_card_outcome(self, message_id: int = 1001) -> Message:
        message = Message(
            message_id,
            "Бой завершён. Победа!\nПредметы:\nКарта Моль x2",
        )
        await self.farmer.enqueue_message(message)
        with patch.object(self.farmer, "mark_progress"):
            await self.farmer.handle_message(message)
        return message

    async def pending(self):
        return await self.storage.pending_battle_events(
            namespace=BATTLE_NOTIFICATION_NAMESPACE,
            include_deferred=True,
        )

    async def test_outcome_and_intent_are_idempotent_and_delivered_once(self) -> None:
        message = await self.dispatch_card_outcome()
        with patch.object(self.farmer, "mark_progress"):
            await self.farmer.handle_message(message)

        pending = await self.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(
            self.storage.connection.execute(
                "SELECT COUNT(*) FROM battles"
            ).fetchone()[0],
            1,
        )
        self.notifier.card_drop.assert_not_awaited()

        result = await self.legacy.battle_notifications.drain_pending()

        self.assertEqual(result.acknowledged, 1)
        self.notifier.card_drop.assert_awaited_once_with("Карта Моль", None)
        self.assertEqual(await self.pending(), ())
        events = await self.storage.get_events()
        self.assertFalse(
            any(event["event_type"] == "MOB_CARD_DROPPED" for event in events)
        )
        dashboard = await self.storage.get_statistics_dashboard()
        self.assertEqual(dashboard["battle"]["wins"], 1)

    async def test_network_failure_stays_pending_without_breaking_combat_result(self) -> None:
        self.notifier.card_drop.return_value = NotificationDelivery(
            NotificationStatus.RETRYABLE_FAILURE,
            error="network unavailable",
        )
        await self.dispatch_card_outcome()

        result = await self.legacy.battle_notifications.drain_pending()

        self.assertEqual(result.acknowledged, 0)
        pending = await self.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].attempts, 1)
        dashboard = await self.storage.get_statistics_dashboard()
        self.assertEqual(dashboard["battle"]["wins"], 1)
        self.assertFalse(self.legacy.combat.status().active)

    async def test_ack_failure_may_repeat_delivery_but_not_the_battle(self) -> None:
        await self.dispatch_card_outcome()
        with (
            patch.object(
                self.storage,
                "ack_battle_event",
                side_effect=RuntimeError("ACK unavailable"),
            ),
            self.assertLogs("fog_farmer", level="ERROR"),
        ):
            first = await self.legacy.battle_notifications.drain_pending()
        self.assertEqual(first.acknowledged, 0)

        self.storage.connection.execute(
            "UPDATE battle_outbox SET next_attempt_at=NULL"
        )
        self.storage.connection.execute("DELETE FROM battle_outbox_barriers")
        self.storage.connection.commit()
        restarted = BattleNotificationOutbox(self.storage, self.notifier)
        second = await restarted.drain_pending()

        self.assertEqual(second.acknowledged, 1)
        self.assertEqual(self.notifier.card_drop.await_count, 2)
        self.assertEqual(
            self.storage.connection.execute(
                "SELECT COUNT(*) FROM battles"
            ).fetchone()[0],
            1,
        )
        dashboard = await self.storage.get_statistics_dashboard()
        self.assertEqual(dashboard["battle"]["wins"], 1)


if __name__ == "__main__":
    unittest.main()
