from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from pathlib import Path
from typing import cast

from telethon import TelegramClient

from config import API_HASH, API_ID, SESSION_NAME
from farmer import Farmer
from game_mechanisms import MechanismBundle
from notifications import Notifier
from session_lock import SessionLease
from settings_service import SettingsService
from storage import Storage
from storage_types import RuntimeStatus, TelegramSafetyStatus
from telegram_client_port import OwnedTelegramClient

logger = logging.getLogger("fog_farmer")
RUNNER_STOP_TIMEOUT = 15.0
SUPERVISOR_CLOSE_TIMEOUT = 60.0
SUPERVISOR_CLOSE_RETRY_DELAY = 1.0


class SupervisorShutdownError(RuntimeError):
    """Cleanup is incomplete; its owners and shared dependencies must stay alive."""


def create_telegram_client() -> OwnedTelegramClient:
    # Construction itself may open Telethon's SQLite session, so this factory
    # must only run after the supervisor has acquired its session lease.
    return cast(
        OwnedTelegramClient,
        TelegramClient(SESSION_NAME, API_ID, API_HASH, flood_sleep_threshold=0),
    )


class FarmerSupervisor:
    def __init__(
        self,
        storage: Storage,
        notifier: Notifier,
        settings: SettingsService,
        *,
        mechanism_bundle_factory: Callable[[], MechanismBundle],
        client_factory: Callable[[], OwnedTelegramClient] = create_telegram_client,
    ) -> None:
        self.storage = storage
        self.notifier = notifier
        self.settings = settings
        self._mechanism_bundle_factory = mechanism_bundle_factory
        self._client_factory = client_factory
        self._client: OwnedTelegramClient | None = None
        self._disconnect_task: asyncio.Task[None] | None = None
        self.farmer: Farmer | None = None
        self.task: asyncio.Task[None] | None = None
        self._unstarted_farmer: Farmer | None = None
        self._runner_finalizing = False
        self._terminal_failure_description: str | None = None
        self._terminal_failure_state_saved = False
        self._terminal_failure_event_saved = False
        self._terminal_failure_notification_attempted = False
        self._stop_task: asyncio.Task[tuple[bool, str]] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self.lock = asyncio.Lock()
        self._closing = False
        self._lease_owned = False
        self.session_lease = SessionLease(Path(f"{SESSION_NAME}.lock"))

    def is_running(self) -> bool:
        return self.task is not None and not self.task.done()

    def begin_shutdown(self) -> None:
        """Close admission before the control dispatcher drains its current handler."""
        self._closing = True

    async def start(self) -> tuple[bool, str]:
        async with self.lock:
            if self._closing:
                return False, "Приложение останавливается. Новый запуск недоступен."
            if self.is_running():
                return False, "Фармер уже запущен."
            if (
                self._client is not None
                or self.farmer is not None
                or self._unstarted_farmer is not None
            ):
                return False, "Предыдущая сессия ещё не закрыта. Повторите «Стоп»."
            # Bundle preflight is pure and does not need a Telegram session.
            try:
                bundle = self._mechanism_bundle_factory()
                bundle.validate()
            except ValueError as error:
                return False, str(error)
            if not self.session_lease.acquire():
                return False, (
                    "Telethon-сессия уже используется другим экземпляром. Сначала нажмите «Стоп»."
                )
            self._lease_owned = True
            farmer: Farmer | None = None
            try:
                self._client = self._client_factory()
                farmer = Farmer(
                    self.storage,
                    self.notifier,
                    self.settings,
                    mechanism_bundle=bundle,
                    client=self._client,
                )
                farmer.validate_config()
                await self.storage.set_setting("farmer_stop_requested", False)
                coroutine = self._runner(farmer)
                try:
                    task = asyncio.create_task(coroutine, name="fog-farmer")
                except BaseException:
                    coroutine.close()
                    raise
            except BaseException as error:
                # A build can fail before Farmer exists: the supervisor still
                # owns the already constructed client and lease in that case.
                self._unstarted_farmer = farmer
                try:
                    if farmer is not None:
                        await farmer.stop("ошибка запуска")
                    await self._disconnect_owned_client()
                except BaseException:
                    logger.exception("Не удалось закрыть сессию после ошибки запуска")
                    raise
                self._release_stopped_farmer()
                if isinstance(error, ValueError):
                    return False, str(error)
                raise
            self.farmer = farmer
            self.task = task
            task.add_done_callback(self._runner_completed)
            return True, "Фармер запущен."

    def _runner_completed(self, completed: asyncio.Task[None]) -> None:
        if not completed.cancelled():
            completed.exception()
        self._release_stopped_farmer()

    def _release_stopped_farmer(self) -> None:
        farmer = self.farmer or self._unstarted_farmer
        if self._client is not None or (farmer is not None and not farmer.shutdown_complete):
            return
        if self.task is not None and not self.task.done():
            return
        self.farmer = None
        self._unstarted_farmer = None
        self.task = None
        self._runner_finalizing = False
        self._terminal_failure_description = None
        self._terminal_failure_state_saved = False
        self._terminal_failure_event_saved = False
        self._terminal_failure_notification_attempted = False
        if self._lease_owned:
            self.session_lease.release()
            self._lease_owned = False

    async def _disconnect_client(self, client: OwnedTelegramClient) -> None:
        await client.disconnect()

    async def _disconnect_owned_client(self) -> None:
        client = self._client
        if client is None:
            return
        task = self._disconnect_task
        if task is None or (task.done() and (task.cancelled() or task.exception() is not None)):
            coroutine = self._disconnect_client(client)
            try:
                task = asyncio.create_task(coroutine, name="telegram-session-close")
            except BaseException:
                coroutine.close()
                raise
            self._disconnect_task = task
            task.add_done_callback(self._consume_close_result)
        done, _ = await asyncio.wait({task}, timeout=RUNNER_STOP_TIMEOUT)
        if not done:
            raise TimeoutError("Telegram session disconnect is still running")
        task.result()
        self._client = None
        self._disconnect_task = None

    @property
    def _terminal_failure_persisted(self) -> bool:
        return self._terminal_failure_description is None or (
            self._terminal_failure_state_saved and self._terminal_failure_event_saved
        )

    async def _finish_terminal_failure(self) -> None:
        description = self._terminal_failure_description
        if description is None:
            return
        if not self._terminal_failure_state_saved:
            await self.storage.update_state(
                process_status="ERROR", game_state="ERROR", last_error=description
            )
            self._terminal_failure_state_saved = True
        if not self._terminal_failure_event_saved:
            await self.storage.add_event(
                "FARMER_CRASHED", description, level="CRITICAL"
            )
            self._terminal_failure_event_saved = True
        if self._terminal_failure_notification_attempted:
            return
        # The notification transport is best effort. Mark the attempt before the
        # await because an error cannot prove whether the remote side delivered it.
        self._terminal_failure_notification_attempted = True
        try:
            await self.notifier.send(f"Фармер аварийно завершён\n{description}")
        except Exception:
            logger.exception("Не удалось сообщить об аварийной остановке фармера")

    @staticmethod
    def _completion_progress(farmer: Farmer) -> tuple[str, int]:
        view = farmer.mechanism_view()
        snapshot = view.snapshot
        cycle = view.cycle
        label = cycle.unit_label.capitalize() if cycle is not None else "Прогресс"
        return label, snapshot.total_progress_units

    async def _runner(self, farmer: Farmer) -> None:
        failure: Exception | None = None
        cancelled = False
        try:
            try:
                await farmer.run()
            except asyncio.CancelledError:
                cancelled = True
            except Exception as error:
                failure = error
                logger.exception("Критическая ошибка фармера")
            finally:
                # From this point onward the runner writes terminal facts and
                # releases its client. Stop callers must join, not cancel it.
                self._runner_finalizing = True
                reason = farmer.stop_reason or "сессия завершена"
                try:
                    # Also covers alternative/failed run implementations and early cancellation.
                    await farmer.stop(reason)
                except Exception:
                    logger.exception("Не удалось завершить очистку сессии фармера")

            if cancelled:
                raise asyncio.CancelledError
            try:
                if failure is not None:
                    self._terminal_failure_description = (
                        f"{type(failure).__name__}: {failure}"
                    )
                    await self._finish_terminal_failure()
                elif farmer.shutdown_complete:
                    if reason.startswith("завершены все циклы"):
                        await self.notifier.send_event(
                            "✅ Фарм завершён",
                            rows=[
                                ("Циклов выполнено", farmer.current_cycle),
                                self._completion_progress(farmer),
                                ("Причина", "все запланированные циклы завершены"),
                            ],
                        )
                    else:
                        await self.notifier.send_event(
                            "⏹ Фармер остановлен", rows=[("Причина", reason)]
                        )
            except Exception:
                logger.exception("Не удалось сообщить об остановке фармера")
        finally:
            # Terminal state writes must finish before a new session can acquire the lease.
            # Failed cleanup retains both the lease and farmer, so Stop can retry it.
            if farmer.shutdown_complete and self._terminal_failure_persisted:
                try:
                    await self._disconnect_owned_client()
                except Exception:
                    logger.exception("Не удалось закрыть Telegram-сессию")

    async def pause(self) -> tuple[bool, str]:
        farmer = self.farmer
        if not self.is_running() or farmer is None:
            return False, "Фармер не запущен."
        return await farmer.request_pause()

    async def resume(self) -> tuple[bool, str]:
        farmer = self.farmer
        if not self.is_running() or farmer is None:
            return False, "Фармер не запущен."
        return await farmer.resume()

    async def skip_rest(self, token: str) -> tuple[bool, str]:
        farmer = self.farmer
        if not self.is_running() or farmer is None:
            return False, "Фармер не запущен."
        return await farmer.skip_rest(token)

    async def _stop_once(self) -> tuple[bool, str]:
        async with self.lock:
            farmer = self.farmer or self._unstarted_farmer
            task = self.task
            if farmer is None and self._client is None:
                if self._lease_owned:
                    self._release_stopped_farmer()
                    return True, "Фармер остановлен."
                await self.storage.set_setting("farmer_stop_requested", True)
                return True, "Команда остановки передана другому экземпляру фармера."
            reason = "остановлен через служебного бота"
            if farmer is not None:
                farmer.stop_reason = farmer.stop_reason or reason
            if (
                task is not None
                and not task.done()
                and task.cancelling() == 0
                and not self._runner_finalizing
                and (farmer is None or not farmer.owns_run_task(task))
            ):
                # A task which has not entered Farmer.run() cannot observe its
                # stop event. Cancelling that pending/foreign runner prevents it
                # from starting after the Farmer has already been closed.
                task.cancel()
            try:
                if farmer is not None:
                    await farmer.stop(reason)
                if task is not None:
                    done, _ = await asyncio.wait({task}, timeout=RUNNER_STOP_TIMEOUT)
                    if not done:
                        return False, "Задача ещё останавливается; повторите «Стоп»."
                    if not task.cancelled():
                        error = task.exception()
                        if error is not None:
                            logger.error("Задача фармера завершилась с ошибкой: %s", error)
                if farmer is not None and not farmer.shutdown_complete:
                    return False, "Очистка сессии не завершена. Повторите «Стоп»."
                await self._finish_terminal_failure()
                await self._disconnect_owned_client()
            except Exception:
                logger.exception("Не удалось завершить остановку фармера")
                return False, (
                    "Очистка сессии не завершена. Повторите «Стоп»; подробности в журнале."
                )
            self._release_stopped_farmer()
            return True, "Фармер остановлен."

    @staticmethod
    def _consume_stop_result(task: asyncio.Task[tuple[bool, str]]) -> None:
        if not task.cancelled():
            task.exception()

    @staticmethod
    def _consume_close_result(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    def _ensure_stop_task(self) -> asyncio.Task[tuple[bool, str]]:
        task = self._stop_task
        if task is None or task.done():
            coroutine = self._stop_once()
            try:
                task = asyncio.create_task(coroutine, name="supervisor-stop")
            except BaseException:
                coroutine.close()
                raise
            self._stop_task = task
            task.add_done_callback(self._consume_stop_result)
        return task

    async def stop(self) -> tuple[bool, str]:
        """One bounded UI wait; unfinished cleanup remains owned for retry/close."""
        task = self._ensure_stop_task()
        done, _ = await asyncio.wait({task}, timeout=RUNNER_STOP_TIMEOUT)
        if not done:
            return False, "Задача ещё останавливается; повторите «Стоп»."
        return task.result()

    async def _close(self, timeout: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if (
                not self._lease_owned
                and self._client is None
                and self.farmer is None
                and self._unstarted_farmer is None
                and self.task is None
                and (self._stop_task is None or self._stop_task.done())
            ):
                return
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise SupervisorShutdownError(
                    "Farmer cleanup did not finish; shared resources must remain open"
                )
            task = self._ensure_stop_task()
            done, _ = await asyncio.wait({task}, timeout=remaining)
            if not done:
                raise SupervisorShutdownError(
                    "Farmer cleanup did not finish; shared resources must remain open"
                )
            try:
                succeeded, message = task.result()
            except Exception:
                logger.exception("Повтор остановки фармера завершился с ошибкой")
            else:
                if succeeded:
                    continue
                logger.warning("Приложение ожидает очистку фармера: %s", message)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining > 0:
                await asyncio.sleep(min(SUPERVISOR_CLOSE_RETRY_DELAY, remaining))

    async def close(self, *, timeout: float | None = None) -> None:
        """Join cleanup or raise without releasing its live owner/lease.

        Cancelling this waiter does not cancel cleanup. A deadline failure permits
        a later close() to retry; callers must keep shared resources open meanwhile.
        """
        self.begin_shutdown()
        if timeout is None:
            timeout = SUPERVISOR_CLOSE_TIMEOUT
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Shutdown timeout must be finite and positive")
        task = self._close_task
        if task is None or (task.done() and (task.cancelled() or task.exception() is not None)):
            coroutine = self._close(timeout)
            try:
                task = asyncio.create_task(coroutine, name="supervisor-close")
            except BaseException:
                coroutine.close()
                raise
            self._close_task = task
            task.add_done_callback(self._consume_close_result)
        cancelled = False
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancelled = True
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise
        if cancelled:
            raise asyncio.CancelledError

    async def status(self) -> RuntimeStatus:
        farmer = self.farmer
        safety: TelegramSafetyStatus = (
            farmer.telegram_safety_status()
            if farmer is not None
            else {
                "telegram_cooldown_remaining": 0,
                "telegram_cooldown_until": None,
                "telegram_cooldown_reason": None,
                "telegram_actions_1m": 0,
                "telegram_actions_10m": 0,
            }
        )
        return {
            **await self.storage.get_state(),
            **safety,
            "task_running": self.is_running(),
            "rest_token": farmer.rest_token if farmer is not None and farmer.running else None,
            "location_name": (
                farmer.mechanism_view().snapshot.location_name
                if farmer is not None
                else None
            ),
        }
