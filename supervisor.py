from __future__ import annotations

import asyncio
import logging

from farmer import Farmer
from notifications import Notifier
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

    def is_running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def start(self):
        async with self.lock:
            if self.is_running():
                return False, "Фармер уже запущен."
            if not self.settings.values.enabled_targets:
                return False, "Нужно выбрать хотя бы одного моба."
            self.farmer = Farmer(
                self.storage,
                self.notifier,
                self.settings,
            )
            self.task = asyncio.create_task(self._runner(), name="fog-farmer")
            return True, "Фармер запущен."

    async def _runner(self):
        try:
            assert self.farmer is not None
            await self.farmer.run()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception("Критическая ошибка фармера")
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
            await self.notifier.send(
                f"🚨 Фармер аварийно завершён\n{type(error).__name__}: {error}"
            )
        finally:
            completed_farmer = self.farmer
            reason = (
                completed_farmer.stop_reason
                if completed_farmer is not None
                else None
            ) or "сессия завершена"

            completed_cycles = (
                completed_farmer.current_cycle
                if completed_farmer is not None
                else None
            )
            total_moves = (
                completed_farmer.context.move_count
                if completed_farmer is not None
                else None
            )

            # Сначала очищаем задачу, чтобы динамическая клавиатура
            # уже показывала только кнопку «▶️ Запустить».
            self.task = None
            self.farmer = None

            try:
                if reason.startswith("завершены все циклы"):
                    await self.notifier.send_event(
                        "✅ Фарм завершён",
                        rows=[
                            ("Циклов выполнено", completed_cycles or "—"),
                            ("Перемещений", total_moves or 0),
                            (
                                "Причина",
                                "все запланированные циклы завершены",
                            ),
                        ],
                    )
                else:
                    await self.notifier.send_event(
                        "⏹ Фармер остановлен",
                        rows=[("Причина", reason)],
                    )
            except Exception:
                logger.exception(
                    "Не удалось обновить клавиатуру после остановки"
                )

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
                return False, "Фармер уже остановлен."
            await self.farmer.stop("остановлен через служебного бота")
            if self.task and not self.task.done():
                try:
                    await asyncio.wait_for(self.task, timeout=15)
                except asyncio.TimeoutError:
                    self.task.cancel()
            return True, "Фармер остановлен."

    async def restart(self):
        if self.is_running():
            await self.stop()
        return await self.start()

    async def status(self):
        state = await self.storage.get_state()
        state["task_running"] = self.is_running()
        return state
