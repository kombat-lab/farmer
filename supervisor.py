from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from config import SESSION_NAME
from farmer import Farmer
from notifications import Notifier
from session_lock import SessionLease
from settings_service import SettingsService
from storage import Storage
from storage_types import RuntimeStatus, TelegramSafetyStatus

logger = logging.getLogger("fog_farmer")
RUNNER_STOP_TIMEOUT = 15.0


class FarmerSupervisor:
    def __init__(
        self,
        storage: Storage,
        notifier: Notifier,
        settings: SettingsService,
    ) -> None:
        self.storage = storage
        self.notifier = notifier
        self.settings = settings
        self.farmer: Farmer | None = None
        self.task: asyncio.Task[None] | None = None
        self.lock = asyncio.Lock()
        self._closing = False
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
            if self.farmer is not None:
                return False, "Предыдущая сессия ещё не закрыта. Повторите «Стоп»."
            if not self.settings.values.enabled_targets:
                return False, "Нужно выбрать хотя бы одного моба."
            if not self.session_lease.acquire():
                return False, (
                    "Telethon-сессия уже используется другим экземпляром. Сначала нажмите «Стоп»."
                )
            try:
                await self.storage.set_setting("farmer_stop_requested", False)
                farmer = Farmer(self.storage, self.notifier, self.settings)
                self.farmer = farmer
                self.task = asyncio.create_task(self._runner(farmer), name="fog-farmer")
            except BaseException:
                self.session_lease.release()
                raise
            return True, "Фармер запущен."

    def _release_stopped_farmer(self, farmer: Farmer) -> None:
        if self.farmer is farmer and farmer.shutdown_complete:
            self.farmer = None
            self.task = None
            self.session_lease.release()

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
                    description = f"{type(failure).__name__}: {failure}"
                    await self.storage.update_state(
                        process_status="ERROR", game_state="ERROR", last_error=description
                    )
                    await self.storage.add_event("FARMER_CRASHED", description, level="CRITICAL")
                    await self.notifier.send(f"Фармер аварийно завершён\n{description}")
                elif farmer.shutdown_complete:
                    if reason.startswith("завершены все циклы"):
                        await self.notifier.send_event(
                            "✅ Фарм завершён",
                            rows=[
                                ("Циклов выполнено", farmer.current_cycle),
                                ("Перемещений", farmer.context.move_count),
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
            self._release_stopped_farmer(farmer)

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

    async def stop(self) -> tuple[bool, str]:
        async with self.lock:
            farmer = self.farmer
            task = self.task
            if farmer is None:
                await self.storage.set_setting("farmer_stop_requested", True)
                return True, "Команда остановки передана другому экземпляру фармера."

            reason = "остановлен через служебного бота"
            farmer.stop_reason = farmer.stop_reason or reason
            if task is not None and not task.done():
                # Stop initialization too: it must not connect again after cleanup.
                task.cancel()
            try:
                await farmer.stop(reason)
            except Exception:
                logger.exception("Не удалось завершить остановку фармера")
                return False, (
                    "Очистка сессии не завершена. Повторите «Стоп»; подробности в журнале."
                )

            if task is not None:
                done, _ = await asyncio.wait({task}, timeout=RUNNER_STOP_TIMEOUT)
                if not done:
                    return False, "Задача ещё останавливается; повторите «Стоп»."
                if not task.cancelled():
                    error = task.exception()
                    if error is not None:
                        logger.error("Задача фармера завершилась с ошибкой: %s", error)
            self._release_stopped_farmer(farmer)
            if not farmer.shutdown_complete:
                return False, "Очистка сессии не завершена. Повторите «Стоп»."
            return True, "Фармер остановлен."

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
            "location_name": farmer.navigator.location_name if farmer is not None else None,
        }
