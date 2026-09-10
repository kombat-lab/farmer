from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch

from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, Update, User

from control_bot import ControlBot
from farmer import Farmer
from notifications import Notifier
from settings_service import SettingsService
from storage import Storage
from supervisor import FarmerSupervisor


class FakeClient:
    def __init__(self) -> None:
        self.connected = True
        self.authorized = True
        self.disconnected = asyncio.Event()
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
        self.disconnected.set()


def make_farmer() -> tuple[Farmer, FakeClient]:
    storage = AsyncMock(spec=Storage)
    notifier = AsyncMock(spec=Notifier)
    settings = SettingsService(storage)
    client = FakeClient()
    with patch("farmer.TelegramClient", return_value=client):
        farmer = Farmer(storage, notifier, settings)
    return farmer, client


def make_supervisor(farmer: Farmer) -> FarmerSupervisor:
    supervisor = FarmerSupervisor(farmer.storage, farmer.notifier, farmer.settings)
    supervisor.farmer = farmer
    supervisor.session_lease = Mock()
    return supervisor


class FarmerShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_storage_failure_still_joins_workers_and_disconnects(self) -> None:
        farmer, client = make_farmer()
        farmer.storage.update_state.side_effect = OSError("disk unavailable")
        worker = asyncio.create_task(asyncio.Event().wait())
        watchdog = asyncio.create_task(asyncio.Event().wait())
        farmer.worker_task = worker
        farmer.watchdog_task = watchdog
        with self.assertLogs("fog_farmer", level="ERROR"):
            await farmer.stop("test")
        self.assertTrue(worker.done())
        self.assertTrue(watchdog.done())
        self.assertFalse(client.connected)
        self.assertTrue(farmer.shutdown_complete)
        await farmer.stop("second stop")
        self.assertEqual(client.disconnect_calls, 1)

    async def test_storage_timeout_does_not_block_disconnect(self) -> None:
        farmer, client = make_farmer()

        async def stuck_write(**kwargs: object) -> None:
            await asyncio.Event().wait()

        farmer.storage.update_state.side_effect = stuck_write
        with patch("farmer.SHUTDOWN_STEP_TIMEOUT", 0.02):
            with self.assertLogs("fog_farmer", level="ERROR"):
                await asyncio.wait_for(farmer.stop("test"), timeout=1)
        self.assertFalse(client.connected)
        self.assertTrue(farmer.shutdown_complete)

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
        self.assertFalse(client.connected)
        self.assertTrue(farmer.shutdown_complete)

    async def test_worker_initiated_stop_and_run_finalizer_do_not_deadlock(self) -> None:
        farmer, client = make_farmer()
        farmer._run_session = client.disconnected.wait
        runner = asyncio.create_task(farmer.run())
        await asyncio.sleep(0)
        worker = asyncio.create_task(farmer.stop("cycle complete"))
        farmer.worker_task = worker
        results = await asyncio.wait_for(
            asyncio.gather(runner, worker, return_exceptions=True), timeout=1
        )
        self.assertIsNone(results[0])
        self.assertTrue(worker.done())
        self.assertTrue(farmer.shutdown_complete)
        self.assertEqual(client.disconnect_calls, 1)

    async def test_external_stop_can_cancel_worker_already_joining_cleanup(self) -> None:
        farmer, client = make_farmer()
        enter_stop = asyncio.Event()

        async def worker_stop() -> None:
            await enter_stop.wait()
            await farmer.stop("worker")

        worker = asyncio.create_task(worker_stop())
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
        self.assertFalse(client.connected)
        self.assertTrue(farmer.shutdown_complete)

    async def test_normal_run_return_always_cleans_up(self) -> None:
        farmer, client = make_farmer()
        farmer._run_session = AsyncMock()
        worker = asyncio.create_task(asyncio.Event().wait())
        farmer.worker_task = worker
        await farmer.run()
        self.assertTrue(worker.done())
        self.assertFalse(client.connected)
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
        self.assertFalse(client.connected)
        self.assertTrue(farmer.shutdown_complete)

    async def test_failed_disconnect_can_be_retried(self) -> None:
        farmer, client = make_farmer()
        client.disconnect_error = OSError("disconnect failed")
        with self.assertRaises(OSError):
            await farmer.stop("test")
        self.assertFalse(farmer.shutdown_complete)
        client.disconnect_error = None
        await farmer.stop("retry")
        self.assertTrue(farmer.shutdown_complete)
        self.assertEqual(client.disconnect_calls, 2)

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

        worker = asyncio.create_task(slow_worker())
        farmer.worker_task = worker
        await worker_started.wait()
        try:
            with patch("farmer.SHUTDOWN_STEP_TIMEOUT", 0.02):
                with self.assertRaises(TimeoutError):
                    await farmer.stop("test")
            self.assertFalse(farmer.shutdown_complete)
            self.assertFalse(client.connected)
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
        self.assertFalse(client.connected)


class SupervisorShutdownTests(unittest.IsolatedAsyncioTestCase):
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
        supervisor.begin_shutdown()
        succeeded, result = await supervisor.start()
        self.assertFalse(succeeded)
        self.assertIn("Приложение останавливается", result)
        supervisor.session_lease.acquire.assert_not_called()
        farmer.storage.set_setting.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
