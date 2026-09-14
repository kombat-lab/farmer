from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from battle_notification_outbox import BATTLE_NOTIFICATION_NAMESPACE, events_for
from battle_outbox import BattleOutboxEnvelope
from battle_records import BattleOutcome, ItemDrop, RewardBundle, SourceEventId
from game_input import ActionOutcome, InboundEvent
from legacy_fog_mechanisms import ManagedLegacyCombatController
from models import BotState
from notifications import NotificationDelivery, NotificationStatus, Notifier
from settings_service import SettingsService
from storage import Storage
from tests.combat_runtime_harness import Button, Message
from tests.legacy_fog_factory import legacy_farmer, legacy_runtime


class CombatIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.storage = Storage(Path(":memory:"))
        self.settings = SettingsService(self.storage)
        await self.settings.load()
        self.client = MagicMock()
        self.client.connect = AsyncMock()
        self.client.disconnect = AsyncMock()
        self.client.disconnected = asyncio.get_running_loop().create_future()
        self.client.is_user_authorized = AsyncMock(return_value=True)
        self.client.get_input_entity = AsyncMock(return_value="offline-peer")
        self.client.run_until_disconnected = AsyncMock()
        self.notifier = AsyncMock(spec=Notifier)
        with patch("tests.legacy_fog_factory.create_test_client", return_value=self.client):
            self.farmer = legacy_farmer(self.storage, self.notifier, self.settings)
        self.legacy = legacy_runtime(self.farmer)
        self._session_task: asyncio.Task[None] | None = None
        self.legacy.context.current_hp = 780
        self.legacy.context.max_hp = 780
        progress = patch.object(self.farmer, "mark_progress")
        progress.start()
        self.addCleanup(progress.stop)

    async def asyncTearDown(self) -> None:
        await self.farmer.stop("test cleanup")
        if self._session_task is not None:
            await self._session_task
        self.assertTrue(self.farmer.shutdown_complete)
        self.assertFalse(self.farmer.task_scope.snapshot())
        await self.storage.close()

    async def dispatch(self, message: Message) -> None:
        await self.farmer.enqueue_message(message)
        await self.farmer.handle_message(message)

    async def start_offline_session(self) -> None:
        if self._session_task is not None:
            raise RuntimeError("Offline session already started")
        ready = asyncio.Event()

        async def signal_ready() -> None:
            ready.set()

        with (
            patch.object(self.farmer, "validate_config"),
            patch.object(self.farmer, "cleanup_old_log_files", return_value=0),
            patch.object(
                self.farmer,
                "process_latest_state",
                new=AsyncMock(side_effect=signal_ready),
            ),
        ):
            self._session_task = asyncio.create_task(self.farmer._run_session())
            try:
                await asyncio.wait_for(ready.wait(), timeout=1)
            except BaseException:
                if self._session_task.done():
                    self._session_task.result()
                raise
            if self._session_task.done():
                self._session_task.result()
                raise AssertionError("Offline session exited before the stop signal")

    async def test_old_packet_preserves_new_hp_and_battle_but_records_its_outcome(self) -> None:
        with patch.object(
            self.farmer, "click_button_outcome", new=AsyncMock(return_value=ActionOutcome.SENT)
        ):
            await self.dispatch(Message(10, "Вы напали: Фонарщик"))
            await self.dispatch(Message(20, "Вы напали: Пепельник"))
            await self.dispatch(
                Message(
                    21, "⚔️ Раунд 3\nKombat\n❤️ 600/780\nВыберите навык:", [[Button("Атака аколита")]]
                )
            )
        managed = self.legacy.combat
        assert isinstance(managed, ManagedLegacyCombatController)
        controller = managed.legacy_controller
        pending = controller.pending_combat_decision
        await self.dispatch(Message(15, "Бой завершён\nПоражение\nKombat\n❤️ 0/780"))
        self.assertEqual(self.legacy.context.current_hp, 600)
        self.assertIs(self.farmer.state, BotState.COMBAT)
        self.assertIsNone(self.legacy.recovery_task)
        self.assertIs(controller.pending_combat_decision, pending)
        self.assertEqual(controller.status().target_name, "Пепельник")
        with self.storage.connection:
            row = self.storage.connection.execute(
                "SELECT target_name,result FROM battles WHERE source_message_id=15"
            ).fetchone()
        assert row is not None
        self.assertEqual(tuple(row), ("Фонарщик", "DEFEAT"))

    async def test_farmer_passes_only_immutable_input_to_coordinator(self) -> None:
        handler = AsyncMock(return_value=True)
        source = Message(1, "Новая боёвка")
        await self.farmer.enqueue_message(source)
        object.__setattr__(source, "raw_text", "Изменено")
        with patch.object(self.legacy, "handle", new=handler):
            await self.farmer.handle_message(source)

        handler.assert_awaited_once()
        event = handler.await_args.args[0]
        self.assertIsInstance(event, InboundEvent)
        self.assertEqual(event.snapshot.raw_text, "Новая боёвка")
        self.assertFalse(hasattr(event, "click"))
        self.assertFalse(hasattr(event.snapshot, "click"))

    async def test_startup_initializes_controller_and_owned_diagnostics(self) -> None:
        managed = self.legacy.combat
        assert isinstance(managed, ManagedLegacyCombatController)
        diagnostics = managed.diagnostics
        with (
            patch.object(diagnostics, "initialize", wraps=diagnostics.initialize) as initialize,
            patch.object(diagnostics, "backfill", wraps=diagnostics.backfill) as backfill,
            patch.object(diagnostics, "cleanup", wraps=diagnostics.cleanup) as cleanup,
            patch.object(managed.legacy_controller, "initialize", new=AsyncMock()) as controller,
            patch.object(
                self.storage, "cleanup_old_data", wraps=self.storage.cleanup_old_data
            ) as retention,
        ):
            await self.start_offline_session()
        initialize.assert_awaited()
        controller.assert_awaited_once()
        backfill.assert_awaited_once()
        cleanup.assert_awaited_once()
        self.assertEqual(
            retention.call_args.kwargs["event_types_to_delete"],
            ("LOW_HP_WAIT_STARTED", "LOW_HP_WAIT_FINISHED"),
        )
        self.assertIsNotNone(self.farmer.session_id)

    async def test_startup_replays_pending_notification_from_canonical_outcome(self) -> None:
        outcome = BattleOutcome(
            source_event_id=SourceEventId("test:integration:701"),
            source_message_id=701,
            session_id=None,
            target_name="Фонарщик",
            result="VICTORY",
            rewards=RewardBundle(items=(ItemDrop("Карта Фонарщика", is_card=True),)),
            position=(4, 5),
        )
        await self.storage.record_battle_outcome(outcome, events=events_for(outcome))
        delivered = asyncio.Event()

        async def card_drop(item: str, position: tuple[int, int] | None) -> NotificationDelivery:
            delivered.set()
            return NotificationDelivery(NotificationStatus.SENT)

        self.notifier.card_drop.side_effect = card_drop
        await self.start_offline_session()
        await asyncio.wait_for(delivered.wait(), timeout=1)

        self.notifier.card_drop.assert_awaited_once_with("Карта Фонарщика", (4, 5))
        self.assertEqual(
            await self.storage.pending_battle_events(
                namespace=BATTLE_NOTIFICATION_NAMESPACE,
                include_deferred=True,
            ),
            (),
        )

    async def test_notification_outbox_read_error_does_not_stop_farmer(self) -> None:
        queried = asyncio.Event()
        original_pending = self.storage.pending_battle_event_entries

        async def failing_notification_read(
            *,
            namespace: str,
            limit: int = 100,
            after_id: int | None = None,
            include_deferred: bool = False,
        ) -> tuple[BattleOutboxEnvelope, ...]:
            if namespace == BATTLE_NOTIFICATION_NAMESPACE:
                queried.set()
                raise OSError("outbox unavailable")
            return await original_pending(
                namespace=namespace,
                limit=limit,
                after_id=after_id,
                include_deferred=include_deferred,
            )

        with (
            patch.object(
                self.storage,
                "pending_battle_event_entries",
                side_effect=failing_notification_read,
            ),
            self.assertLogs("fog_farmer", level="ERROR"),
        ):
            await self.start_offline_session()
            await asyncio.wait_for(queried.wait(), timeout=1)

        self.assertIsNone(self.farmer._background_error)
        task = self.legacy.battle_notification_task
        self.assertIsNotNone(task)
        assert task is not None
        self.assertFalse(task.done())

    async def test_shutdown_cancellation_preserves_pending_notification(self) -> None:
        outcome = BattleOutcome(
            source_event_id=SourceEventId("test:integration:702"),
            source_message_id=702,
            session_id=None,
            target_name="Фонарщик",
            result="VICTORY",
            rewards=RewardBundle(items=(ItemDrop("Карта Фонарщика", is_card=True),)),
            position=(8, 9),
        )
        await self.storage.record_battle_outcome(outcome, events=events_for(outcome))
        delivery_started = asyncio.Event()

        async def blocked_delivery(
            item: str, position: tuple[int, int] | None
        ) -> NotificationDelivery:
            delivery_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        self.notifier.card_drop.side_effect = blocked_delivery
        await self.start_offline_session()
        await asyncio.wait_for(delivery_started.wait(), timeout=1)
        task = self.legacy.battle_notification_task
        self.assertIsNotNone(task)
        assert task is not None

        await self.farmer.stop("test shutdown")

        self.assertTrue(task.done())
        self.assertIsNone(self.legacy.battle_notification_task)
        pending = await self.storage.pending_battle_events(
            namespace=BATTLE_NOTIFICATION_NAMESPACE,
            include_deferred=True,
        )
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].attempts, 0)


if __name__ == "__main__":
    unittest.main()
