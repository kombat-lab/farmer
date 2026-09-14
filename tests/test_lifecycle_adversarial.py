from __future__ import annotations

import asyncio
import unittest
from collections import Counter, defaultdict
from unittest.mock import AsyncMock, patch

from tests.test_shutdown import make_farmer, make_supervisor


class FarmerLifecycleAdversarialTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_loss_during_authorization_aborts_startup(self) -> None:
        farmer, client = make_farmer()
        authorization_started = asyncio.Event()

        async def connect_then_drop() -> None:
            if not client.disconnected.done():
                client.disconnected.set_result(None)

        async def wait_for_authorization_forever() -> bool:
            authorization_started.set()
            await asyncio.Event().wait()
            return True

        client.connect = AsyncMock(side_effect=connect_then_drop)
        client.is_user_authorized = AsyncMock(side_effect=wait_for_authorization_forever)
        farmer.validate_config = unittest.mock.Mock()
        farmer.mechanisms.initialize = AsyncMock()
        runner = asyncio.create_task(farmer.run())
        try:
            await authorization_started.wait()
            await asyncio.wait_for(asyncio.shield(runner), timeout=0.2)
        finally:
            if not runner.done():
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)

        self.assertTrue(farmer.shutdown_complete)
        farmer.mechanisms.initialize.assert_not_awaited()
        self.assertEqual(client.disconnect_calls, 0)

    async def test_mechanism_can_stop_cleanly_during_initialization(self) -> None:
        farmer, _ = make_farmer()
        farmer.validate_config = unittest.mock.Mock()

        async def initialize_and_stop() -> None:
            await farmer.stop("mechanism declined startup")

        farmer.mechanisms.initialize = AsyncMock(side_effect=initialize_and_stop)
        await asyncio.wait_for(farmer.run(), timeout=1)

        self.assertEqual(farmer.stop_reason, "mechanism declined startup")
        self.assertTrue(farmer.shutdown_complete)
        farmer.storage.start_session.assert_not_awaited()
    async def test_cancelled_stop_waiter_preserves_cancellation_when_cleanup_fails(self) -> None:
        farmer, _ = make_farmer()
        write_started = asyncio.Event()
        release_write = asyncio.Event()

        async def fail_after_release(**values: object) -> None:
            write_started.set()
            await release_write.wait()
            raise OSError("persistence failed")

        farmer.storage.update_state.side_effect = fail_after_release
        waiter = asyncio.create_task(farmer.stop("cancelled waiter"))
        await write_started.wait()
        waiter.cancel()
        release_write.set()

        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertFalse(farmer.shutdown_complete)
        farmer.storage.update_state.side_effect = None
        await farmer.stop("retry cleanup")
        self.assertTrue(farmer.shutdown_complete)

    async def test_partial_metrics_flush_retries_only_uncommitted_buckets(self) -> None:
        farmer, _ = make_farmer()
        farmer.telegram_metrics_pending = defaultdict(Counter)
        farmer.telegram_metrics_pending["first"]["sent"] = 2
        farmer.telegram_metrics_pending["second"]["sent"] = 3
        calls: list[str] = []
        fail_second_once = True

        async def increment(bucket: str, metrics: dict[str, int]) -> None:
            nonlocal fail_second_once
            calls.append(bucket)
            if bucket == "second" and fail_second_once:
                fail_second_once = False
                raise OSError("temporary write failure")

        farmer.storage.increment_telegram_activity.side_effect = increment
        with self.assertRaisesRegex(OSError, "temporary write failure"):
            await farmer.flush_telegram_metrics()
        await farmer.flush_telegram_metrics()

        self.assertEqual(calls, ["first", "second", "second"])
        await farmer.stop("test cleanup")


class SupervisorLifecycleAdversarialTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_close_waiter_preserves_cancellation_on_deadline(self) -> None:
        farmer, _ = make_farmer()
        supervisor = make_supervisor(farmer)
        stop_started = asyncio.Event()
        release_stop = asyncio.Event()
        original_stop = farmer.stop

        async def delayed_stop(reason: str) -> None:
            stop_started.set()
            await release_stop.wait()
            await original_stop(reason)

        farmer.stop = delayed_stop
        waiter = asyncio.create_task(supervisor.close(timeout=0.03))
        await stop_started.wait()
        waiter.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await waiter
        supervisor.session_lease.release.assert_not_called()
        release_stop.set()
        await supervisor.close(timeout=1)
        supervisor.session_lease.release.assert_called_once()

    async def test_crash_persistence_failure_retains_client_and_lease_for_retry(self) -> None:
        farmer, client = make_farmer()
        supervisor = make_supervisor(farmer)
        farmer._run_session = AsyncMock(side_effect=RuntimeError("session failed"))

        async def fail_crash_state(**values: object) -> None:
            if values.get("process_status") == "ERROR":
                raise OSError("storage unavailable")

        farmer.storage.update_state.side_effect = fail_crash_state
        runner = asyncio.create_task(supervisor._runner(farmer))
        supervisor.task = runner
        runner.add_done_callback(supervisor._runner_completed)
        with self.assertLogs("fog_farmer", level="ERROR"):
            await runner

        self.assertTrue(client.connected)
        self.assertIs(supervisor.farmer, farmer)
        supervisor.session_lease.release.assert_not_called()

        farmer.storage.update_state.side_effect = None
        succeeded, _ = await supervisor.stop()
        self.assertTrue(succeeded)
        self.assertFalse(client.connected)
        farmer.storage.add_event.assert_any_await(
            "FARMER_CRASHED",
            "RuntimeError: session failed",
            level="CRITICAL",
        )
        supervisor.session_lease.release.assert_called_once()

    async def test_stop_does_not_cancel_terminal_crash_persistence(self) -> None:
        farmer, _ = make_farmer()
        supervisor = make_supervisor(farmer)
        farmer._run_session = AsyncMock(side_effect=RuntimeError("session failed"))
        crash_write_started = asyncio.Event()
        release_crash_write = asyncio.Event()

        async def update_state(**values: object) -> None:
            if values.get("process_status") == "ERROR":
                crash_write_started.set()
                await release_crash_write.wait()

        farmer.storage.update_state.side_effect = update_state
        runner = asyncio.create_task(supervisor._runner(farmer))
        supervisor.task = runner
        runner.add_done_callback(supervisor._runner_completed)
        await crash_write_started.wait()

        try:
            with patch("supervisor.RUNNER_STOP_TIMEOUT", 0.02):
                succeeded, _ = await supervisor.stop()
            self.assertFalse(succeeded)
            self.assertFalse(runner.done())
            supervisor.session_lease.release.assert_not_called()
        finally:
            release_crash_write.set()

        await supervisor.close(timeout=1)
        farmer.storage.add_event.assert_any_await(
            "FARMER_CRASHED",
            "RuntimeError: session failed",
            level="CRITICAL",
        )
        supervisor.session_lease.release.assert_called_once()


if __name__ == "__main__":
    unittest.main()
