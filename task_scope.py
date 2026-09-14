from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Coroutine


class TaskScopeClosedError(RuntimeError):
    """No more tasks may be admitted to this scope."""


class TaskScope:
    """Own tasks until their result is consumed, independently of public handles.

    The first task Exception closes admission and invokes on_failure once. The
    caller decides how that notification stops its runner or other resources.
    Task cancellation is an expected completion, not a reported failure.
    """

    def __init__(self, on_failure: Callable[[Exception], None] | None = None) -> None:
        self._on_failure = on_failure
        self._tasks: set[asyncio.Task[None]] = set()
        self._failure: Exception | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def failure(self) -> Exception | None:
        return self._failure

    def create(
        self,
        coroutine: Coroutine[object, object, None],
        *,
        name: str,
    ) -> asyncio.Task[None]:
        if self._closed:
            coroutine.close()
            raise TaskScopeClosedError("Task scope is closed")
        try:
            task = asyncio.create_task(coroutine, name=name)
        except BaseException:
            coroutine.close()
            raise
        # A task factory can return an already completed task. Register ownership
        # before its callback, whose result consumption releases the task.
        self._tasks.add(task)
        task.add_done_callback(self._completed)
        return task

    def snapshot(self) -> tuple[asyncio.Task[None], ...]:
        """Owned tasks, including completed tasks awaiting their result callback."""
        return tuple(self._tasks)

    def _completed(self, task: asyncio.Task[None]) -> None:
        try:
            if task.cancelled():
                return
            error = task.exception()
            if not isinstance(error, Exception) or self._failure is not None:
                return
            self._failure = error
            self.close()
            if self._on_failure is not None:
                self._on_failure(error)
        finally:
            self._tasks.discard(task)

    def close(self) -> None:
        self._closed = True

    async def cancel_and_wait(
        self,
        timeout: float,
        *,
        exclude: asyncio.Task[None] | None = None,
    ) -> None:
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("Task cancellation timeout must be nonnegative and finite")
        self.close()
        current = asyncio.current_task()
        tasks = tuple(task for task in self._tasks if task is not exclude and task is not current)
        for task in tasks:
            # Repeated cleanup calls must not interrupt a task's cancellation
            # cleanup with additional CancelledErrors.
            if not task.done() and task.cancelling() == 0:
                task.cancel()
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            names = ", ".join(sorted(task.get_name() for task in pending))
            raise TimeoutError(f"Tasks did not stop after cancellation: {names}")
