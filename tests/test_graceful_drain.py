from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from automation_policy import DelayRange
from battle_notification_outbox import BATTLE_NOTIFICATION_NAMESPACE
from event_ingress import EventIngress
from game_input import ActionOutcome
from notifications import Notifier
from settings_service import SettingsService
from storage import Storage
from tests.combat_runtime_harness import Message
from tests.legacy_fog_factory import legacy_farmer, legacy_runtime
from tests.test_shutdown import make_farmer, make_supervisor


class GracefulDrainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.farmer, self.client = make_farmer()
        self.order = []
        self.handled = []
        self.releases = []

        async def handle(event):
            self.handled.append(event)
            self.order.append(f"handle:{event.snapshot.id}")
            self.assertFalse(self.farmer.running)
            return True

        async def close():
            self.order.append("close")

        async def persist(reason):
            self.order.append("persist")

        self.farmer.mechanisms.handle = AsyncMock(side_effect=handle)
        self.farmer.mechanisms.aclose = AsyncMock(side_effect=close)
        self.farmer._persist_stop = AsyncMock(side_effect=persist)

    async def asyncTearDown(self) -> None:
        for release in self.releases:
            release.set()
        self.farmer.mechanisms.handle.side_effect = None
        self.farmer.mechanisms.aclose.side_effect = None
        self.farmer._persist_stop.side_effect = None
        await self.farmer.stop("test cleanup")

    async def enqueue(self, identifier=1, text="accepted fact"):
        message = Message(identifier, text)
        await self.farmer.enqueue_message(message)
        return message

    async def test_accepted_facts_drain_before_scope_close_and_persistence(self) -> None:
        entered = asyncio.Event()

        async def timer():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.order.append("timer-exit")

        self.farmer._start_background(timer(), name="mechanism-timer")
        await entered.wait()
        await self.enqueue(1)
        await self.enqueue(2)
        await self.farmer.stop("done")
        self.assertEqual(self.order, ["handle:1", "handle:2", "timer-exit", "close", "persist"])
        await asyncio.wait_for(self.farmer.ingress.join(), 0.2)
        self.assertTrue(self.farmer.shutdown_complete)
        self.assertEqual(self.client.disconnect_calls, 0)
        self.assertFalse(self.farmer._event_references)

    async def test_failed_handler_retains_exact_event_and_sidecar_for_retry(self) -> None:
        source = await self.enqueue()
        event = self.farmer.input_event(source)
        self.farmer.mechanisms.handle.side_effect = OSError("ledger unavailable")
        for _ in range(2):
            with self.assertRaisesRegex(OSError, "ledger unavailable"):
                await self.farmer.stop("retryable failure")
            self.assertIs(self.farmer._inflight_event, event)
            self.assertIs(self.farmer._event_messages[event.sequence].rpc, source)
            self.assertEqual(self.farmer._event_references[event.sequence], 1)
            self.assertFalse(self.farmer.shutdown_complete)
            self.farmer.mechanisms.aclose.assert_not_awaited()
        self.farmer.mechanisms.handle.side_effect = None
        await self.farmer.stop("retry")
        calls = self.farmer.mechanisms.handle.await_args_list
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call.args[0] is event for call in calls))
        self.assertIsNone(self.farmer._inflight_event)

    async def test_timeout_retains_running_handler_and_does_not_repeat_it(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        self.releases.append(release)
        source = await self.enqueue()
        captured = []

        async def handle(event):
            captured.append(event)
            entered.set()
            await release.wait()
            return True

        self.farmer.mechanisms.handle.side_effect = handle
        with patch("farmer.SHUTDOWN_STEP_TIMEOUT", 0.01):
            for _ in range(2):
                with self.assertRaises(TimeoutError):
                    await self.farmer.stop("blocked handler")
        self.assertTrue(entered.is_set())
        self.assertEqual(len(captured), 1)
        self.assertFalse(self.farmer.shutdown_complete)
        self.assertIs(self.farmer._event_messages[captured[0].sequence].rpc, source)
        release.set()
        await self.farmer.stop("released")
        self.assertEqual(len(captured), 1)

    async def test_failed_live_worker_leaves_fact_for_cleanup(self) -> None:
        source = await self.enqueue()
        self.farmer.mechanisms.handle.side_effect = OSError("first handling failed")
        with self.assertLogs("fog_farmer", level="ERROR"):
            worker = self.farmer._start_background(self.farmer.event_worker(), name="events")
            with self.assertRaises(OSError):
                await worker
        event = self.farmer.input_event(source)
        self.assertIs(self.farmer._inflight_event, event)
        self.farmer.mechanisms.handle.side_effect = None
        await self.farmer.stop("retry worker fact")
        self.assertEqual(self.farmer.mechanisms.handle.await_count, 2)
        self.assertTrue(self.farmer.shutdown_complete)

    async def test_worker_stop_drains_current_and_next_fact_without_self_join(self) -> None:
        await self.enqueue(1)
        await self.enqueue(2)
        self.farmer._run_session = self.farmer._stop_requested.wait
        handled = []

        async def handle(event):
            handled.append(event.snapshot.id)
            await self.farmer.stop("requested inside handler")
            return True

        self.farmer.mechanisms.handle.side_effect = handle
        runner = asyncio.create_task(self.farmer.run())
        self.farmer.worker_task = self.farmer._start_background(
            self.farmer.event_worker(), name="events"
        )
        await asyncio.wait_for(runner, 1)
        self.assertEqual(handled, [1, 2])
        self.assertTrue(self.farmer.worker_task.done())
        self.assertTrue(self.farmer.shutdown_complete)

    async def test_close_rejects_blocked_producer_but_drains_accepted_head(self) -> None:
        self.farmer.ingress = EventIngress(self.farmer.input_policy, capacity=1)
        await self.enqueue(1)
        producer = asyncio.create_task(self.enqueue(2))
        await asyncio.sleep(0)
        self.assertFalse(producer.done())
        await self.farmer.stop("full queue")
        await producer
        self.assertEqual([event.snapshot.id for event in self.handled], [1])
        self.assertEqual(self.farmer.ingress.qsize(), 0)

    async def test_all_game_action_ports_are_disabled_while_draining(self) -> None:
        source = await self.enqueue()
        outcomes = []

        async def handle(event):
            outcomes.append(await self.farmer.send_game_message("state", action_label="state"))
            outcomes.append(await self.farmer.click_button_outcome(
                source, description="action", exact="go", delay_range=DelayRange(0, 0)
            ))
            self.assertFalse(await self.farmer.request_current_state(force=True))
            await self.farmer.complete_cycle()
            await self.farmer.start_activity_break()
            await self.farmer.pause_after_movement()
            await self.farmer.enter_paused()
            return True

        self.farmer.mechanisms.handle.side_effect = handle
        self.farmer.intentional_sleep = AsyncMock()
        await self.farmer.stop("disabled effects")
        self.assertEqual(outcomes, [ActionOutcome.DEFERRED, ActionOutcome.DEFERRED])
        self.farmer.intentional_sleep.assert_not_awaited()
        self.assertIsNone(self.farmer.rest_task)
        self.assertIsNone(self.farmer.activity_break_task)

    async def test_mechanism_close_failure_retries_without_replaying_drained_facts(self) -> None:
        await self.enqueue()
        self.farmer.mechanisms.aclose.side_effect = OSError("mechanism persistence failed")
        with self.assertRaises(OSError):
            await self.farmer.stop("close failure")
        self.assertEqual(self.farmer.mechanisms.handle.await_count, 1)
        self.farmer._persist_stop.assert_not_awaited()
        self.farmer.mechanisms.aclose.side_effect = None
        await self.farmer.stop("retry close")
        self.assertEqual(self.farmer.mechanisms.handle.await_count, 1)
        self.assertEqual(self.farmer.mechanisms.aclose.await_count, 2)

    async def test_stop_waits_for_cancelled_startup_before_closing_mechanisms(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        self.releases.append(release)

        async def session():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()

        self.farmer._run_session = session
        runner = asyncio.create_task(self.farmer.run())
        await entered.wait()
        with patch("farmer.SHUTDOWN_STEP_TIMEOUT", 0.01):
            with self.assertRaises(TimeoutError):
                await self.farmer.stop("startup still unwinding")
        self.farmer.mechanisms.aclose.assert_not_awaited()
        self.assertFalse(self.farmer.shutdown_complete)
        release.set()
        await asyncio.wait_for(runner, 1)
        self.assertTrue(self.farmer.shutdown_complete)

    async def test_stop_requested_inside_startup_does_not_join_itself(self) -> None:
        async def session():
            await self.farmer.stop("mechanism requested stop during startup")

        self.farmer._run_session = session
        await asyncio.wait_for(self.farmer.run(), 1)
        self.assertTrue(self.farmer.shutdown_complete)

    async def test_transport_watcher_cancellation_does_not_cancel_transport_future(self) -> None:
        task = self.farmer._start_background(self.farmer._watch_transport(), name="transport")
        await asyncio.sleep(0)
        await self.farmer.stop("shutdown")
        self.assertTrue(task.done())
        self.assertFalse(self.client.disconnected.done())
        self.assertEqual(self.client.disconnect_calls, 0)

    async def test_transport_completion_wakes_runner_without_disconnect(self) -> None:
        self.farmer._run_session = self.farmer._stop_requested.wait
        runner = asyncio.create_task(self.farmer.run())
        self.farmer._start_background(self.farmer._watch_transport(), name="transport")
        self.client.disconnected.set_result(None)
        await asyncio.wait_for(runner, 1)
        self.assertTrue(self.farmer.shutdown_complete)
        self.assertEqual(self.client.disconnect_calls, 0)


class DurableBattleDrainTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_outcome_and_card_intent_commit_before_stop(self) -> None:
        storage = Storage(Path(":memory:"))
        settings = SettingsService(storage)
        await settings.load()
        notifier = AsyncMock(spec=Notifier)
        farmer = legacy_farmer(storage, notifier, settings)
        legacy = legacy_runtime(farmer)
        await legacy.initialize()
        legacy.context.active_target = "Моль"
        farmer.session_id = await storage.start_session(cycles_count=1, moves_per_cycle=10)
        try:
            await farmer.enqueue_message(Message(
                1001, "Бой завершён. Победа!\nПредметы:\nКарта Моль x2"
            ))
            await farmer.stop("accepted battle pending")
            row = storage.connection.execute(
                "SELECT id FROM battles WHERE source_message_id=1001"
            ).fetchone()
            self.assertIsNotNone(row)
            outcome = await storage.get_battle_outcome(row["id"])
            self.assertIsNotNone(outcome)
            pending = await storage.pending_battle_events(
                namespace=BATTLE_NOTIFICATION_NAMESPACE, include_deferred=True
            )
            self.assertTrue(pending)
            self.assertTrue(farmer.shutdown_complete)
            farmer.client.send_message.assert_not_awaited()
            notifier.card_drop.assert_not_awaited()
        finally:
            await farmer.stop("test cleanup")
            await storage.close()

    async def test_death_fact_drains_without_starting_recovery_timer(self) -> None:
        storage = Storage(Path(":memory:"))
        settings = SettingsService(storage)
        await settings.load()
        notifier = AsyncMock(spec=Notifier)
        farmer = legacy_farmer(storage, notifier, settings)
        legacy = legacy_runtime(farmer)
        await legacy.initialize()
        legacy.context.active_target = "Моль"
        try:
            await farmer.enqueue_message(Message(1002, "Бой завершён\nПоражение"))
            await farmer.stop("accepted death pending")
            row = storage.connection.execute(
                "SELECT id FROM battles WHERE source_message_id=1002"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertIsNotNone(await storage.get_battle_outcome(row["id"]))
            self.assertIsNone(legacy.recovery_task)
            self.assertFalse(farmer.task_scope.snapshot())
            notifier.send.assert_not_awaited()
        finally:
            await farmer.stop("test cleanup")
            await storage.close()


class ClientOwnershipTests(unittest.IsolatedAsyncioTestCase):
    def unstarted_supervisor(self):
        farmer, client = make_farmer()
        supervisor = make_supervisor(farmer)
        supervisor._client = None
        supervisor._lease_owned = False
        supervisor.farmer = None
        bundle = Mock()
        supervisor._mechanism_bundle_factory = Mock(return_value=bundle)
        return supervisor, farmer, client, bundle

    async def test_bundle_validation_precedes_lease_and_client_creation(self) -> None:
        supervisor, _, _, bundle = self.unstarted_supervisor()
        bundle.validate.side_effect = ValueError("invalid mechanism")
        supervisor._client_factory = Mock()
        result, message = await supervisor.start()
        self.assertFalse(result)
        self.assertEqual(message, "invalid mechanism")
        supervisor.session_lease.acquire.assert_not_called()
        supervisor._client_factory.assert_not_called()

    async def test_build_failure_disconnects_constructed_client_before_release(self) -> None:
        supervisor, _, client, bundle = self.unstarted_supervisor()
        order = []
        bundle.validate.side_effect = lambda: order.append("validate")
        supervisor.session_lease.acquire.side_effect = lambda: order.append("lease") or True
        supervisor._client_factory = Mock(side_effect=lambda: order.append("client") or client)
        client.disconnect = AsyncMock(side_effect=lambda: order.append("disconnect"))
        supervisor.session_lease.release.side_effect = lambda: order.append("release")

        def fail_build(*args, **kwargs):
            order.append("build")
            self.assertIs(kwargs["client"], client)
            raise RuntimeError("build failed")

        with patch("supervisor.Farmer", side_effect=fail_build):
            with self.assertRaisesRegex(RuntimeError, "build failed"):
                await supervisor.start()
        self.assertEqual(order, ["validate", "lease", "client", "build", "disconnect", "release"])
        self.assertIsNone(supervisor._client)
        self.assertIsNone(supervisor.farmer)

    async def test_build_failure_and_disconnect_failure_retain_client_only_owner(self) -> None:
        supervisor, _, client, _ = self.unstarted_supervisor()
        client.disconnect_error = OSError("disconnect failed")
        with patch("supervisor.Farmer", side_effect=RuntimeError("build failed")):
            with self.assertLogs("fog_farmer", level="ERROR"), self.assertRaises(OSError):
                await supervisor.start()
        self.assertIs(supervisor._client, client)
        self.assertIsNone(supervisor.farmer)
        self.assertIsNone(supervisor._unstarted_farmer)
        supervisor.session_lease.release.assert_not_called()
        self.assertFalse((await supervisor.start())[0])
        client.disconnect_error = None
        await supervisor.close(timeout=1)
        self.assertIsNone(supervisor._client)
        supervisor.session_lease.release.assert_called_once()

    async def test_client_factory_failure_releases_lease_without_publishing_owner(self) -> None:
        supervisor, _, _, _ = self.unstarted_supervisor()
        supervisor._client_factory = Mock(side_effect=OSError("session open failed"))
        with self.assertRaises(OSError):
            await supervisor.start()
        supervisor.session_lease.release.assert_called_once()
        self.assertIsNone(supervisor._client)
        self.assertIsNone(supervisor.farmer)

    async def test_successful_runner_disconnects_and_releases_once(self) -> None:
        supervisor, farmer, client, _ = self.unstarted_supervisor()
        farmer.validate_config = Mock()
        farmer._run_session = AsyncMock()
        with patch("supervisor.Farmer", return_value=farmer):
            self.assertTrue((await supervisor.start())[0])
        await supervisor.close(timeout=1)
        self.assertTrue(farmer.shutdown_complete)
        self.assertEqual(client.disconnect_calls, 1)
        supervisor.session_lease.release.assert_called_once()
        self.assertIsNone(supervisor._client)

    async def test_disconnect_timeout_is_coalesced_and_lease_stays_owned(self) -> None:
        farmer, client = make_farmer()
        supervisor = make_supervisor(farmer)
        release = asyncio.Event()
        client.disconnect = AsyncMock(side_effect=release.wait)
        with patch("supervisor.RUNNER_STOP_TIMEOUT", 0.01):
            with self.assertLogs("fog_farmer", level="ERROR"):
                with self.assertRaisesRegex(RuntimeError, "shared resources"):
                    await supervisor.close(timeout=0.05)
        self.assertIs(supervisor._client, client)
        supervisor.session_lease.release.assert_not_called()
        self.assertEqual(client.disconnect.await_count, 1)
        release.set()
        await supervisor.close(timeout=1)
        self.assertEqual(client.disconnect.await_count, 1)
        supervisor.session_lease.release.assert_called_once()
