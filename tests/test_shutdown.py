from __future__ import annotations

import asyncio
import inspect
import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch

from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, Update, User

import main as application
from control_bot import ControlBot
from farmer import Farmer
from notifications import Notifier
from settings_service import SettingsService
from storage import Storage
from supervisor import FarmerSupervisor
from tests.legacy_fog_factory import legacy_bundle, legacy_farmer


class FakeClient:
    def __init__(self) -> None:
        self.connected = True
        self.authorized = True
        self.disconnected = asyncio.get_running_loop().create_future()
        self.disconnect_calls = 0
        self.disconnect_error: Exception | None = None

    async def connect(self) -> None:
        self.connected = True

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        if self.disconnect_error is not None:
            raise self.disconnect_error
        self.connected = False
        if not self.disconnected.done():
            self.disconnected.set_result(None)


def make_farmer() -> tuple[Farmer, FakeClient]:
    storage = AsyncMock(spec=Storage)
    notifier = AsyncMock(spec=Notifier)
    settings = SettingsService(storage)
    client = FakeClient()
    with patch("tests.legacy_fog_factory.create_test_client", return_value=client):
        farmer = legacy_farmer(storage, notifier, settings)
    return farmer, client


def make_supervisor(farmer: Farmer) -> FarmerSupervisor:
    supervisor = FarmerSupervisor(
        farmer.storage,
        farmer.notifier,
        farmer.settings,
        client_factory=lambda: farmer.client,
        mechanism_bundle_factory=lambda: legacy_bundle(
            farmer.storage,
            farmer.notifier,
            farmer.settings,
        ),
    )
    supervisor._lease_owned = True
    supervisor._client = farmer.client
    supervisor.farmer = farmer
    supervisor.session_lease = Mock()
    return supervisor


class FarmerShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_storage_failure_retains_cleanup_until_retry(self) -> None:
        farmer, client = make_farmer()
        farmer.storage.update_state.side_effect = OSError("disk unavailable")
        worker = farmer._start_background(asyncio.Event().wait(), name="test-worker")
        with self.assertRaises(OSError):
            await farmer.stop("test")
        self.assertTrue(worker.done())
        self.assertTrue(client.connected)
        self.assertFalse(farmer.shutdown_complete)
        farmer.storage.update_state.side_effect = None
        await farmer.stop("retry")
        self.assertTrue(farmer.shutdown_complete)
        self.assertEqual(client.disconnect_calls, 0)

    async def test_storage_timeout_keeps_exact_write_owned_until_retry(self) -> None:
        farmer, client = make_farmer()
        release = asyncio.Event()

        async def stuck_write(**kwargs: object) -> None:
            await release.wait()

        farmer.storage.update_state.side_effect = stuck_write
        with patch("farmer.SHUTDOWN_STEP_TIMEOUT", 0.02):
            with self.assertRaises(TimeoutError):
                await farmer.stop("test")
        self.assertTrue(client.connected)
        self.assertFalse(farmer.shutdown_complete)
        write_task = farmer._stop_persist_task
        release.set()
        await farmer.stop("retry")
        self.assertIs(farmer._stop_persist_task, write_task)
        self.assertTrue(farmer.shutdown_complete)

    async def test_checkpoint_retry_does_not_repeat_finished_persistence_steps(self) -> None:
        farmer, _ = make_farmer()
        farmer.storage.checkpoint.side_effect = OSError("checkpoint failed")
        with self.assertRaises(OSError):
            await farmer.stop("test")
        self.assertFalse(farmer.shutdown_complete)
        farmer.storage.checkpoint.side_effect = None
        await farmer.stop("retry")
        farmer.storage.update_state.assert_awaited_once()
        farmer.storage.finish_session.assert_awaited_once()
        farmer.storage.add_event.assert_awaited_once()
        self.assertEqual(farmer.storage.checkpoint.await_count, 2)

    async def test_cancelled_stop_waiter_does_not_cancel_cleanup(self) -> None:
        farmer, client = make_farmer()
        write_started = asyncio.Event()
        release_write = asyncio.Event()

        async def delayed_write(**kwargs: object) -> None:
            write_started.set()
            await release_write.wait()

        farmer.storage.update_state.side_effect = delayed_write
        stopping = asyncio.create_task(farmer.stop("test"))
        await write_started.wait()
        stopping.cancel()
        await asyncio.sleep(0)
        self.assertFalse(stopping.done())
        release_write.set()
        with self.assertRaises(asyncio.CancelledError):
            await stopping
        self.assertTrue(client.connected)
        self.assertTrue(farmer.shutdown_complete)

    async def test_worker_initiated_stop_and_run_finalizer_do_not_deadlock(self) -> None:
        farmer, client = make_farmer()
        farmer._run_session = farmer._stop_requested.wait
        runner = asyncio.create_task(farmer.run())
        await asyncio.sleep(0)
        worker = farmer._start_background(farmer.stop("cycle complete"), name="cycle-worker")
        farmer.worker_task = worker
        results = await asyncio.wait_for(
            asyncio.gather(runner, worker, return_exceptions=True), timeout=1
        )
        self.assertIsNone(results[0])
        self.assertTrue(worker.done())
        self.assertTrue(farmer.shutdown_complete)
        self.assertEqual(client.disconnect_calls, 0)

    async def test_external_stop_can_cancel_worker_already_joining_cleanup(self) -> None:
        farmer, client = make_farmer()
        enter_stop = asyncio.Event()

        async def worker_stop() -> None:
            await enter_stop.wait()
            await farmer.stop("worker")

        worker = farmer._start_background(worker_stop(), name="stop-worker")
        farmer.worker_task = worker
        await asyncio.sleep(0)
        control = asyncio.create_task(farmer.stop("control"))
        enter_stop.set()
        with patch("farmer.SHUTDOWN_STEP_TIMEOUT", 0.02):
            results = await asyncio.wait_for(
                asyncio.gather(control, worker, return_exceptions=True), timeout=1
            )
        self.assertIsNone(results[0])
        self.assertTrue(worker.done())
        self.assertTrue(client.connected)
        self.assertTrue(farmer.shutdown_complete)

    async def test_normal_run_return_always_cleans_up(self) -> None:
        farmer, client = make_farmer()
        farmer._run_session = AsyncMock()
        worker = farmer._start_background(asyncio.Event().wait(), name="test-worker")
        farmer.worker_task = worker
        await farmer.run()
        self.assertTrue(worker.done())
        self.assertTrue(client.connected)
        self.assertTrue(farmer.shutdown_complete)

    async def test_cancelled_run_always_cleans_up(self) -> None:
        farmer, client = make_farmer()
        started = asyncio.Event()

        async def session() -> None:
            started.set()
            await asyncio.Event().wait()

        farmer._run_session = session
        runner = asyncio.create_task(farmer.run())
        await started.wait()
        runner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await runner
        self.assertTrue(client.connected)
        self.assertTrue(farmer.shutdown_complete)

    async def test_farmer_never_disconnects_borrowed_client(self) -> None:
        farmer, client = make_farmer()
        client.disconnect_error = OSError("disconnect must remain supervisor-owned")
        await farmer.stop("test")
        await farmer.stop("retry")
        self.assertTrue(farmer.shutdown_complete)
        self.assertEqual(client.disconnect_calls, 0)

    async def test_worker_cancellation_timeout_remains_retryable(self) -> None:
        farmer, client = make_farmer()
        worker_started = asyncio.Event()
        release = asyncio.Event()

        async def slow_worker() -> None:
            worker_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()

        worker = farmer._start_background(slow_worker(), name="slow-worker")
        farmer.worker_task = worker
        await worker_started.wait()
        try:
            with patch("farmer.SHUTDOWN_STEP_TIMEOUT", 0.02):
                with self.assertRaises(TimeoutError):
                    await farmer.stop("test")
            self.assertFalse(farmer.shutdown_complete)
            self.assertTrue(client.connected)
        finally:
            release.set()
            await worker
        await farmer.stop("retry")
        self.assertTrue(farmer.shutdown_complete)

    async def test_unauthorized_session_fails_without_interactive_login(self) -> None:
        farmer, client = make_farmer()
        client.authorized = False
        farmer.validate_config = Mock()
        with patch("builtins.input", side_effect=AssertionError("interactive login")):
            with self.assertRaisesRegex(RuntimeError, "python authorize.py"):
                await farmer.run()
        farmer.storage.start_session.assert_not_awaited()
        self.assertTrue(farmer.shutdown_complete)
        self.assertTrue(client.connected)


class SupervisorShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_creation_failure_does_not_publish_owner_or_leak_coroutine(self) -> None:
        farmer, _ = make_farmer()
        supervisor = make_supervisor(farmer)
        supervisor.farmer = None
        supervisor._client = None
        supervisor.session_lease.acquire.return_value = True
        farmer.validate_config = Mock()
        captured = []
        original_create = asyncio.create_task

        def fail_create(coroutine, *, name):
            if name != "fog-farmer":
                return original_create(coroutine, name=name)
            captured.append(coroutine)
            raise RuntimeError("task factory failed")

        with (
            patch("supervisor.Farmer", return_value=farmer),
            patch("supervisor.asyncio.create_task", side_effect=fail_create),
            self.assertRaisesRegex(RuntimeError, "task factory failed"),
        ):
            await supervisor.start()
        self.assertIsNone(supervisor.farmer)
        self.assertIsNone(supervisor.task)
        supervisor.session_lease.release.assert_called_once()
        self.assertEqual(len(captured), 1)
        self.assertEqual(inspect.getcoroutinestate(captured[0]), inspect.CORO_CLOSED)

    async def test_constructor_is_not_called_until_session_lease_is_owned(self) -> None:
        farmer, _ = make_farmer()
        supervisor = make_supervisor(farmer)
        supervisor.farmer = None
        supervisor._client = None
        supervisor.session_lease.acquire.return_value = False
        with patch("supervisor.Farmer") as factory:
            succeeded, _ = await supervisor.start()
        self.assertFalse(succeeded)
        factory.assert_not_called()

    async def test_preflight_failure_disconnects_before_releasing_lease(self) -> None:
        farmer, client = make_farmer()
        supervisor = make_supervisor(farmer)
        supervisor.farmer = None
        supervisor._client = None
        supervisor.session_lease.acquire.return_value = True
        farmer.validate_config = Mock(side_effect=ValueError("invalid policy"))
        released = []
        supervisor.session_lease.release.side_effect = lambda: released.append(not client.connected)
        with patch("supervisor.Farmer", return_value=farmer):
            succeeded, message = await supervisor.start()
        self.assertFalse(succeeded)
        self.assertEqual(message, "invalid policy")
        self.assertEqual(released, [True])
        self.assertIsNone(supervisor.farmer)
        self.assertIsNone(supervisor.task)
        farmer.storage.set_setting.assert_not_awaited()

    async def test_failed_preflight_disconnect_stays_owned_for_close_retry(self) -> None:
        farmer, client = make_farmer()
        supervisor = make_supervisor(farmer)
        supervisor.farmer = None
        supervisor._client = None
        supervisor.session_lease.acquire.return_value = True
        farmer.validate_config = Mock(side_effect=ValueError("invalid policy"))
        client.disconnect_error = OSError("disconnect failed")
        with (
            patch("supervisor.Farmer", return_value=farmer),
            self.assertLogs("fog_farmer", level="ERROR"),
            self.assertRaises(OSError),
        ):
            await supervisor.start()
        self.assertIsNone(supervisor.farmer)
        self.assertIsNone(supervisor.task)
        self.assertIs(supervisor._unstarted_farmer, farmer)
        supervisor.session_lease.release.assert_not_called()
        client.disconnect_error = None
        await supervisor.close(timeout=1)
        self.assertIsNone(supervisor._unstarted_farmer)
        supervisor.session_lease.release.assert_called_once()

    async def test_close_retries_failed_cleanup_before_returning(self) -> None:
        farmer, client = make_farmer()
        supervisor = make_supervisor(farmer)
        original = client.disconnect
        attempts = 0

        async def transient_disconnect() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("temporary disconnect failure")
            await original()

        client.disconnect = transient_disconnect
        with (
            patch("supervisor.SUPERVISOR_CLOSE_RETRY_DELAY", 0.001),
            self.assertLogs("fog_farmer", level="ERROR"),
        ):
            await supervisor.close(timeout=1)
        self.assertEqual(attempts, 2)
        self.assertTrue(supervisor._closing)
        self.assertIsNone(supervisor.farmer)
        supervisor.session_lease.release.assert_called_once()
        await supervisor.close(timeout=1)
        self.assertEqual(attempts, 2)

    async def test_close_deadline_retains_live_runner_and_lease(self) -> None:
        farmer, _ = make_farmer()
        supervisor = make_supervisor(farmer)
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_runner() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()

        task = asyncio.create_task(slow_runner())
        supervisor.task = task
        await started.wait()
        try:
            with self.assertRaisesRegex(RuntimeError, "shared resources must remain open"):
                await supervisor.close(timeout=0.02)
            self.assertFalse(task.done())
            self.assertIs(supervisor.farmer, farmer)
            supervisor.session_lease.release.assert_not_called()
        finally:
            release.set()
            await task
        await supervisor.close(timeout=1)
        supervisor.session_lease.release.assert_called_once()

    async def test_close_waiter_cancellation_waits_for_owned_shutdown(self) -> None:
        farmer, _ = make_farmer()
        supervisor = make_supervisor(farmer)
        entered = asyncio.Event()
        release = asyncio.Event()
        original_stop = farmer.stop

        async def delayed_stop(reason: str) -> None:
            entered.set()
            await release.wait()
            await original_stop(reason)

        farmer.stop = delayed_stop
        closing = asyncio.create_task(supervisor.close(timeout=1))
        await entered.wait()
        closing.cancel()
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        supervisor.session_lease.release.assert_not_called()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        supervisor.session_lease.release.assert_called_once()
        self.assertIsNone(supervisor.farmer)

    async def test_ui_stop_is_bounded_while_cleanup_remains_owned(self) -> None:
        farmer, _ = make_farmer()
        supervisor = make_supervisor(farmer)
        entered = asyncio.Event()
        release = asyncio.Event()
        original_stop = farmer.stop

        async def delayed_stop(reason: str) -> None:
            entered.set()
            await release.wait()
            await original_stop(reason)

        farmer.stop = delayed_stop
        with patch("supervisor.RUNNER_STOP_TIMEOUT", 0.01):
            succeeded, _ = await supervisor.stop()
        self.assertFalse(succeeded)
        self.assertTrue(entered.is_set())
        supervisor.session_lease.release.assert_not_called()
        release.set()
        await supervisor.close(timeout=1)
        supervisor.session_lease.release.assert_called_once()

    async def test_stop_before_runner_starts_releases_lease_after_disconnect(self) -> None:
        farmer, client = make_farmer()
        supervisor = make_supervisor(farmer)
        farmer._run_session = AsyncMock()
        supervisor.task = asyncio.create_task(supervisor._runner(farmer))
        released = []
        supervisor.session_lease.release.side_effect = lambda: released.append(
            not client.connected and farmer.shutdown_complete
        )
        succeeded, _ = await supervisor.stop()
        self.assertTrue(succeeded)
        self.assertEqual(released, [True])
        self.assertIsNone(supervisor.task)
        self.assertIsNone(supervisor.farmer)

    async def test_stop_of_active_runner_uses_stable_task_reference(self) -> None:
        farmer, client = make_farmer()
        supervisor = make_supervisor(farmer)
        started = asyncio.Event()

        async def session() -> None:
            started.set()
            await asyncio.Event().wait()

        farmer._run_session = session
        supervisor.task = asyncio.create_task(supervisor._runner(farmer))
        await started.wait()
        succeeded, _ = await supervisor.stop()
        self.assertTrue(succeeded)
        self.assertFalse(client.connected)
        supervisor.session_lease.release.assert_called_once()
        self.assertIsNone(supervisor.task)

    async def test_timeout_retains_lease_until_runner_exits(self) -> None:
        farmer, _ = make_farmer()
        supervisor = make_supervisor(farmer)
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_runner() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()

        task = asyncio.create_task(slow_runner())
        supervisor.task = task
        await started.wait()
        try:
            with patch("supervisor.RUNNER_STOP_TIMEOUT", 0.02):
                succeeded, _ = await supervisor.stop()
            self.assertFalse(succeeded)
            self.assertIs(supervisor.task, task)
            supervisor.session_lease.release.assert_not_called()
        finally:
            release.set()
            await task
        succeeded, _ = await supervisor.stop()
        self.assertTrue(succeeded)
        supervisor.session_lease.release.assert_called_once()

    async def test_terminal_error_is_written_before_allowing_restart(self) -> None:
        farmer, _ = make_farmer()
        supervisor = make_supervisor(farmer)
        farmer._run_session = AsyncMock(side_effect=RuntimeError("session failed"))
        writing_error = asyncio.Event()
        release_write = asyncio.Event()

        async def write_state(**values: object) -> None:
            if values.get("process_status") == "ERROR":
                writing_error.set()
                await release_write.wait()

        farmer.storage.update_state.side_effect = write_state
        with self.assertLogs("fog_farmer", level="ERROR"):
            task = asyncio.create_task(supervisor._runner(farmer))
            supervisor.task = task
            task.add_done_callback(supervisor._runner_completed)
            await writing_error.wait()
            supervisor.session_lease.release.assert_not_called()
            self.assertIs(supervisor.farmer, farmer)
            succeeded, _ = await supervisor.start()
            self.assertFalse(succeeded)
            release_write.set()
            await task
        supervisor.session_lease.release.assert_called_once()

    async def test_cleanup_failure_retains_lease_and_prevents_restart(self) -> None:
        farmer, client = make_farmer()
        supervisor = make_supervisor(farmer)
        farmer._run_session = AsyncMock()
        client.disconnect_error = OSError("disconnect failed")
        supervisor.task = asyncio.create_task(supervisor._runner(farmer))
        with self.assertLogs("fog_farmer", level="ERROR"):
            await supervisor.task
        supervisor.session_lease.release.assert_not_called()
        succeeded, _ = await supervisor.start()
        self.assertFalse(succeeded)
        client.disconnect_error = None
        succeeded, _ = await supervisor.stop()
        self.assertTrue(succeeded)
        supervisor.session_lease.release.assert_called_once()


class ApplicationShutdownTests(unittest.IsolatedAsyncioTestCase):
    def resources(self):
        storage = AsyncMock(spec=Storage)
        resources = application._ApplicationResources(storage)
        session = Mock()
        session.close = AsyncMock()
        resources.telegram_session = session
        supervisor = Mock(spec=FarmerSupervisor)
        supervisor.close = AsyncMock()
        resources.supervisor = supervisor
        control = Mock(spec=ControlBot)
        control.stop = AsyncMock()
        resources.control_bot = control
        return resources, storage, session, supervisor, control

    async def test_resources_close_only_after_control_and_farmer_finish(self) -> None:
        resources, storage, session, supervisor, control = self.resources()
        order = []
        supervisor.begin_shutdown.side_effect = lambda: order.append("admission")
        control.stop.side_effect = lambda: order.append("control")
        supervisor.close.side_effect = lambda: order.append("farmer")
        session.close.side_effect = lambda: order.append("session")
        storage.close.side_effect = lambda: order.append("storage")
        await resources.close()
        await resources.close()
        self.assertEqual(order, ["admission", "control", "farmer", "session", "storage"])

    async def test_failed_farmer_close_preserves_shared_resources_until_retry(self) -> None:
        resources, storage, session, supervisor, _ = self.resources()
        supervisor.close.side_effect = RuntimeError("farmer remains alive")
        with self.assertRaisesRegex(RuntimeError, "farmer remains alive"):
            await resources.close()
        session.close.assert_not_awaited()
        storage.close.assert_not_awaited()
        supervisor.close.side_effect = None
        await resources.close()
        session.close.assert_awaited_once()
        storage.close.assert_awaited_once()

    async def test_stuck_control_does_not_close_shared_resources(self) -> None:
        resources, storage, session, supervisor, control = self.resources()
        release = asyncio.Event()
        control.stop.side_effect = release.wait
        try:
            with patch("main.CONTROL_STOP_TIMEOUT", 0.01):
                with self.assertRaises(application.ApplicationShutdownError):
                    await resources.close()
            self.assertFalse(resources._control_stop_task.done())
            supervisor.close.assert_not_awaited()
            session.close.assert_not_awaited()
            storage.close.assert_not_awaited()
        finally:
            release.set()
            await resources._control_stop_task
        await resources.close()
        storage.close.assert_awaited_once()

    async def test_cancelled_application_waiter_cannot_close_resources_early(self) -> None:
        resources, storage, session, supervisor, _ = self.resources()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def close_farmer() -> None:
            entered.set()
            await release.wait()

        supervisor.close.side_effect = close_farmer
        closing = asyncio.create_task(resources.close())
        await entered.wait()
        closing.cancel()
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        session.close.assert_not_awaited()
        storage.close.assert_not_awaited()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        session.close.assert_awaited_once()
        storage.close.assert_awaited_once()

    async def test_session_close_failure_still_closes_unowned_storage(self) -> None:
        resources, storage, session, _, _ = self.resources()
        session.close.side_effect = OSError("HTTP close failed")
        with self.assertRaises(OSError):
            await resources.close()
        storage.close.assert_awaited_once()


class ControlShutdownTests(unittest.IsolatedAsyncioTestCase):
    def make_control(self) -> tuple[ControlBot, Bot, FarmerSupervisor]:
        farmer, _ = make_farmer()
        supervisor = make_supervisor(farmer)
        bot = Bot(token="123456:test_token")
        bot.me = AsyncMock(return_value=User(id=123456, is_bot=True, first_name="Test"))
        bot.delete_webhook = AsyncMock()
        bot.set_my_commands = AsyncMock()
        control = ControlBot(bot, farmer.storage, supervisor, farmer.settings)
        control.dispatcher = Dispatcher()
        return control, bot, supervisor

    async def test_polling_stop_joins_active_handler(self) -> None:
        control, bot, supervisor = self.make_control()
        entered = asyncio.Event()
        exited = asyncio.Event()

        @control.dispatcher.message()
        async def handler(message: Message) -> None:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                exited.set()

        async def updates(*args: object, **kwargs: object):
            yield Update(
                update_id=1,
                message=Message(
                    message_id=1,
                    date=datetime.now(UTC),
                    chat=Chat(id=7, type="private"),
                    from_user=User(id=7, is_bot=False, first_name="Admin"),
                    text="test",
                ),
            )
            await asyncio.Event().wait()

        try:
            # Fake only Telegram's incoming stream; use the real dispatcher lifecycle.
            with patch.object(control.dispatcher, "_listen_updates", new=updates):
                await control.start()
                await asyncio.wait_for(entered.wait(), timeout=1)
                await asyncio.wait_for(control.stop(), timeout=1)
            self.assertTrue(exited.is_set())
            self.assertIsNone(control.polling_task)
            self.assertTrue(supervisor._closing)
        finally:
            await bot.session.close()

    async def test_stop_immediately_after_start_does_not_hang(self) -> None:
        control, bot, _ = self.make_control()

        async def updates(*args: object, **kwargs: object):
            await asyncio.Event().wait()
            yield Update(update_id=1)

        try:
            with patch.object(control.dispatcher, "_listen_updates", new=updates):
                await control.start()
                await asyncio.wait_for(control.stop(), timeout=1)
            self.assertIsNone(control.polling_task)
        finally:
            await bot.session.close()

    async def test_shutdown_admission_rejects_new_farmer(self) -> None:
        farmer, _ = make_farmer()
        supervisor = make_supervisor(farmer)
        supervisor.farmer = None
        supervisor._client = None
        supervisor.begin_shutdown()
        succeeded, result = await supervisor.start()
        self.assertFalse(succeeded)
        self.assertIn("Приложение останавливается", result)
        supervisor.session_lease.acquire.assert_not_called()
        farmer.storage.set_setting.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
