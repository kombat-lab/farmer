from __future__ import annotations

import asyncio
import unittest
from collections.abc import Coroutine
from unittest.mock import patch

from task_scope import TaskScope, TaskScopeClosedError


async def never_finishes() -> None:
    await asyncio.Event().wait()


class TaskScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_is_owned_until_completion_callback(self) -> None:
        calls: list[Exception] = []
        scope = TaskScope(calls.append)

        async def succeed() -> None:
            return

        task = scope.create(succeed(), name="successful-task")
        self.assertEqual(task.get_name(), "successful-task")
        self.assertEqual(scope.snapshot(), (task,))
        await task
        await asyncio.sleep(0)
        self.assertEqual(scope.snapshot(), ())
        self.assertEqual(calls, [])
        self.assertIsNone(scope.failure)
        self.assertFalse(scope.closed)

    async def test_task_finished_before_callback_registration_is_not_lost(self) -> None:
        error = ValueError("early failure")

        async def fail() -> None:
            raise error

        completed = asyncio.create_task(fail())
        await asyncio.sleep(0)
        self.assertTrue(completed.done())
        failures: list[Exception] = []
        scope = TaskScope(failures.append)

        def eager_task_factory(
            coroutine: Coroutine[object, object, None], *, name: str
        ) -> asyncio.Task[None]:
            coroutine.close()
            return completed

        with patch("task_scope.asyncio.create_task", side_effect=eager_task_factory):
            registered = scope.create(never_finishes(), name="already-done")
        self.assertIs(registered, completed)
        self.assertEqual(scope.snapshot(), (completed,))
        await asyncio.sleep(0)
        self.assertEqual(scope.snapshot(), ())
        self.assertEqual(failures, [error])
        self.assertIs(scope.failure, error)
        self.assertTrue(scope.closed)

    async def test_first_failure_is_reported_once_after_consuming_each_result(self) -> None:
        failures: list[Exception] = []
        scope = TaskScope(failures.append)
        ready = asyncio.Event()
        first_error = ValueError("first")
        second_error = RuntimeError("second")

        async def fail(error: Exception) -> None:
            await ready.wait()
            raise error

        first = scope.create(fail(first_error), name="first")
        second = scope.create(fail(second_error), name="second")
        ready.set()
        await asyncio.gather(first, second, return_exceptions=True)
        self.assertEqual(failures, [first_error])
        self.assertIs(scope.failure, first_error)
        self.assertEqual(scope.snapshot(), ())
        self.assertTrue(scope.closed)
        await scope.cancel_and_wait(0)
        self.assertEqual(failures, [first_error])

    async def test_failure_callback_observes_owned_task_before_release(self) -> None:
        observed: list[asyncio.Task[None]] = []
        scope = TaskScope(lambda error: observed.extend(scope.snapshot()))

        async def fail() -> None:
            raise ValueError("failure")

        task = scope.create(fail(), name="retained-through-callback")
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(observed, [task])
        self.assertEqual(scope.snapshot(), ())

    async def test_cleared_public_handle_does_not_lose_ownership(self) -> None:
        scope = TaskScope()
        handle: asyncio.Task[None] | None = scope.create(never_finishes(), name="owned")
        task = handle
        handle = None
        await scope.cancel_and_wait(1)
        self.assertIsNone(handle)
        self.assertTrue(task.cancelled())
        self.assertEqual(scope.snapshot(), ())

    async def test_close_and_repeated_cancellation_are_idempotent(self) -> None:
        failures: list[Exception] = []
        scope = TaskScope(failures.append)
        task = scope.create(never_finishes(), name="cancelled")
        scope.close()
        scope.close()
        await scope.cancel_and_wait(1)
        await scope.cancel_and_wait(1)
        self.assertTrue(task.cancelled())
        self.assertEqual(scope.snapshot(), ())
        self.assertIsNone(scope.failure)
        self.assertEqual(failures, [])

    async def test_closed_scope_closes_rejected_coroutine(self) -> None:
        scope = TaskScope()
        scope.close()
        coroutine = never_finishes()
        with self.assertRaises(TaskScopeClosedError):
            scope.create(coroutine, name="rejected")
        self.assertIsNone(coroutine.cr_frame)
        self.assertEqual(scope.snapshot(), ())

    async def test_task_creation_error_closes_rejected_coroutine(self) -> None:
        scope = TaskScope()
        coroutine = never_finishes()
        with patch("task_scope.asyncio.create_task", side_effect=RuntimeError("factory failure")):
            with self.assertRaisesRegex(RuntimeError, "factory failure"):
                scope.create(coroutine, name="rejected")
        self.assertIsNone(coroutine.cr_frame)
        self.assertEqual(scope.snapshot(), ())

    async def test_timeout_retains_task_and_retry_does_not_interrupt_cleanup(self) -> None:
        scope = TaskScope()
        entered = asyncio.Event()
        cleaning = asyncio.Event()
        release = asyncio.Event()

        async def cleanup_after_cancellation() -> None:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaning.set()
                await release.wait()

        task = scope.create(cleanup_after_cancellation(), name="slow-cleanup")
        try:
            await entered.wait()
            with self.assertRaisesRegex(TimeoutError, "slow-cleanup"):
                await scope.cancel_and_wait(0.01)
            self.assertTrue(cleaning.is_set())
            self.assertEqual(scope.snapshot(), (task,))
            self.assertFalse(task.done())
            self.assertEqual(task.cancelling(), 1)
            with self.assertRaises(TimeoutError):
                await scope.cancel_and_wait(0.01)
            self.assertEqual(task.cancelling(), 1)
            self.assertFalse(task.done())
        finally:
            release.set()
            await scope.cancel_and_wait(1)
        self.assertEqual(scope.snapshot(), ())
        self.assertIsNone(scope.failure)

    async def test_excluded_task_remains_owned_until_later_cleanup(self) -> None:
        scope = TaskScope()
        excluded = scope.create(never_finishes(), name="excluded")
        other = scope.create(never_finishes(), name="other")
        await scope.cancel_and_wait(1, exclude=excluded)
        self.assertEqual(scope.snapshot(), (excluded,))
        self.assertFalse(excluded.done())
        self.assertTrue(other.cancelled())
        await scope.cancel_and_wait(1)
        self.assertTrue(excluded.cancelled())
        self.assertEqual(scope.snapshot(), ())

    async def test_worker_can_cancel_scope_without_cancelling_itself(self) -> None:
        scope = TaskScope()
        other = scope.create(never_finishes(), name="other")

        async def stop_from_worker() -> None:
            await scope.cancel_and_wait(1)
            self.assertTrue(other.cancelled())
            self.assertIn(asyncio.current_task(), scope.snapshot())

        worker = scope.create(stop_from_worker(), name="stop-initiator")
        await worker
        self.assertFalse(worker.cancelled())
        self.assertEqual(scope.snapshot(), ())

    async def test_cancelled_waiter_does_not_release_unfinished_owned_task(self) -> None:
        scope = TaskScope()
        entered = asyncio.Event()
        cleaning = asyncio.Event()
        release = asyncio.Event()

        async def slow() -> None:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaning.set()
                await release.wait()

        task = scope.create(slow(), name="slow")
        await entered.wait()
        waiter = asyncio.create_task(scope.cancel_and_wait(1))
        try:
            await cleaning.wait()
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            self.assertEqual(scope.snapshot(), (task,))
            self.assertFalse(task.done())
        finally:
            release.set()
            await scope.cancel_and_wait(1)
        self.assertEqual(scope.snapshot(), ())

    async def test_cleanup_exception_is_reported_once(self) -> None:
        failures: list[Exception] = []
        scope = TaskScope(failures.append)
        entered = asyncio.Event()
        error = RuntimeError("cleanup failed")

        async def fail_during_cleanup() -> None:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                raise error

        scope.create(fail_during_cleanup(), name="cleanup-error")
        await entered.wait()
        await scope.cancel_and_wait(1)
        self.assertEqual(failures, [error])
        self.assertEqual(scope.snapshot(), ())

    async def test_invalid_wait_timeout_is_rejected_before_closing_scope(self) -> None:
        for timeout in (-1, float("nan"), float("inf"), float("-inf")):
            with self.subTest(timeout=timeout):
                scope = TaskScope()
                with self.assertRaises(ValueError):
                    await scope.cancel_and_wait(timeout)
                self.assertFalse(scope.closed)


if __name__ == "__main__":
    unittest.main()
