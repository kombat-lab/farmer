from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path

from config import SESSION_NAME
from farmer import Farmer
from notifications import Notifier
from session_lock import SessionLease
from settings_service import SettingsService
from storage import Storage

logger = logging.getLogger("fog_farmer")


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
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.session_lease = SessionLease(Path(f"{SESSION_NAME}.lock"))

    def is_running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def start(self):
        async with self.lock:
            if self.is_running():
                return False, "Фармер уже запущен."
            if not self.settings.values.enabled_targets:
                return False, "Нужно выбрать хотя бы одного моба."

            if not self.session_lease.acquire():
                return False, (
                    "Telethon-сессия уже используется другим экземпляром. Сначала нажмите «Стоп»."
                )

            try:
                await self.storage.set_setting("farmer_stop_requested", False)
                self.farmer = Farmer(self.storage, self.notifier, self.settings)
                self.task = asyncio.create_task(self._runner(), name="fog-farmer")
            except Exception:
                self.session_lease.release()
                raise
            return True, "Фармер запущен."

    async def _runner(self):
        crashed = False
        try:
            assert self.farmer is not None
            await self.farmer.run()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            crashed = True
            logger.exception("Критическая ошибка фармера")
            failed_farmer = self.farmer
            if failed_farmer is not None and failed_farmer.running:
                try:
                    await failed_farmer.stop(
                        f"аварийное завершение: {type(error).__name__}: {error}"
                    )
                except Exception:
                    logger.exception("Не удалось корректно завершить аварийную сессию фармера")
            await self.storage.update_state(
                process_status="ERROR",
                game_state="ERROR",
                last_error=f"{type(error).__name__}: {error}",
            )
            await self.storage.add_event(
                "FARMER_CRASHED",
                f"{type(error).__name__}: {error}",
                level="CRITICAL",
            )
            await self.notifier.send(f"Фармер аварийно завершён\n{type(error).__name__}: {error}")
        finally:
            completed_farmer = self.farmer
            reason = (
                completed_farmer.stop_reason if completed_farmer is not None else None
            ) or "сессия завершена"
            completed_cycles = (
                completed_farmer.current_cycle if completed_farmer is not None else None
            )
            total_moves = (
                completed_farmer.context.move_count if completed_farmer is not None else None
            )

            self.task = None
            self.farmer = None
            self.session_lease.release()

            try:
                if not crashed:
                    if reason.startswith("завершены все циклы"):
                        await self.notifier.send_event(
                            "✅ Фарм завершён",
                            rows=[
                                ("Циклов выполнено", completed_cycles or "—"),
                                ("Перемещений", total_moves or 0),
                                ("Причина", "все запланированные циклы завершены"),
                            ],
                        )
                    else:
                        await self.notifier.send_event(
                            "⏹ Фармер остановлен",
                            rows=[("Причина", reason)],
                        )
            except Exception:
                logger.exception("Не удалось обновить клавиатуру после остановки")

    async def pause(self):
        if not self.is_running() or self.farmer is None:
            return False, "Фармер не запущен."
        return await self.farmer.request_pause()

    async def resume(self):
        if not self.is_running() or self.farmer is None:
            return False, "Фармер не запущен."
        return await self.farmer.resume()

    async def stop(self):
        async with self.lock:
            if not self.is_running() or self.farmer is None:
                await self.storage.set_setting("farmer_stop_requested", True)
                return True, ("Команда остановки передана другому экземпляру фармера.")

            await self.farmer.stop("остановлен через служебного бота")
            if self.task and not self.task.done():
                try:
                    await asyncio.wait_for(self.task, timeout=15)
                except TimeoutError:
                    self.task.cancel()
                    with suppress(asyncio.CancelledError):
                        await self.task
            return True, "Фармер остановлен."

    async def status(self):
        state = await self.storage.get_state()
        state["task_running"] = self.is_running()
        if self.farmer is not None:
            state.update(self.farmer.telegram_safety_status())
        return state
