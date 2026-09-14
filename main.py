from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress

from aiogram import Bot
from aiogram import __version__ as aiogram_version
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

from config import (
    ADMIN_TELEGRAM_ID,
    CONTROL_BOT_TOKEN,
    DATABASE_PATH,
    LOG_BACKUP_COUNT,
    LOG_DIRECTORY,
    LOG_FILENAME,
    LOG_MAX_BYTES,
    prepare_runtime_directories,
    validate_runtime_config,
)
from control_bot import ControlBot
from legacy_fog_mechanisms import default_legacy_fog_bundle
from logger_setup import setup_logging
from notifications import Notifier
from settings_service import SettingsService
from storage import Storage
from supervisor import FarmerSupervisor

logger = logging.getLogger("fog_farmer")

CONTROL_STOP_TIMEOUT = 30.0


class ApplicationShutdownError(RuntimeError):
    """A live resource owner prevented safe closure of shared application resources."""


class _ApplicationResources:
    """Own one ordered shutdown, including partial startup and cancelled waiters."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self.telegram_session: AiohttpSession | None = None
        self.supervisor: FarmerSupervisor | None = None
        self.control_bot: ControlBot | None = None
        self._shutdown_task: asyncio.Task[None] | None = None
        self._control_stop_task: asyncio.Task[None] | None = None
        self._session_closed = False
        self._storage_closed = False

    @staticmethod
    def _consume_result(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    async def _close(self) -> None:
        if self.supervisor is not None:
            self.supervisor.begin_shutdown()
        if self.control_bot is not None:
            task = self._control_stop_task
            if task is None or task.done():
                coroutine = self.control_bot.stop()
                try:
                    task = asyncio.create_task(coroutine, name="control-shutdown")
                except BaseException:
                    coroutine.close()
                    raise
                self._control_stop_task = task
                task.add_done_callback(self._consume_result)
            done, _ = await asyncio.wait({task}, timeout=CONTROL_STOP_TIMEOUT)
            if not done:
                raise ApplicationShutdownError(
                    "Control handlers are still running; shared resources remain open"
                )
            task.result()
        if self.supervisor is not None:
            # Failure must exit before either shared resource is closed. Independent
            # exit-stack callbacks would incorrectly close them after failed cleanup.
            await self.supervisor.close()
        try:
            if self.telegram_session is not None and not self._session_closed:
                await self.telegram_session.close()
                self._session_closed = True
        finally:
            if not self._storage_closed:
                await self.storage.close()
                self._storage_closed = True

    async def close(self) -> None:
        task = self._shutdown_task
        if task is None or (task.done() and (task.cancelled() or task.exception() is not None)):
            coroutine = self._close()
            try:
                task = asyncio.create_task(coroutine, name="application-shutdown")
            except BaseException:
                coroutine.close()
                raise
            self._shutdown_task = task
            task.add_done_callback(self._consume_result)
        cancelled = False
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError



def _install_shutdown_handlers(shutdown_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signal_name in ("SIGINT", "SIGTERM"):
        signal_number = getattr(signal, signal_name, None)
        if signal_number is None:
            continue
        try:
            loop.add_signal_handler(signal_number, shutdown_event.set)
        except (NotImplementedError, RuntimeError):
            signal.signal(
                signal_number,
                lambda *_args: loop.call_soon_threadsafe(shutdown_event.set),
            )


async def _wait_for_shutdown(
    shutdown_event: asyncio.Event,
    polling_task: asyncio.Task[None],
) -> None:
    signal_task = asyncio.create_task(shutdown_event.wait(), name="shutdown-signal")
    try:
        done, _ = await asyncio.wait(
            {signal_task, polling_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if polling_task in done:
            exception = polling_task.exception()
            if exception is not None:
                raise exception
            raise RuntimeError("Polling панели управления неожиданно завершился")

        signal_task.result()
    finally:
        if not signal_task.done():
            signal_task.cancel()
            with suppress(asyncio.CancelledError):
                await signal_task


async def main() -> None:
    global logger
    validate_runtime_config()
    prepare_runtime_directories()
    logger = setup_logging(
        log_directory=LOG_DIRECTORY,
        log_filename=LOG_FILENAME,
        max_bytes=LOG_MAX_BYTES,
        backup_count=LOG_BACKUP_COUNT,
    )

    storage = Storage(DATABASE_PATH)
    resources = _ApplicationResources(storage)
    try:
        settings = SettingsService(storage)
        await settings.load()

        telegram_session = AiohttpSession(timeout=30)
        resources.telegram_session = telegram_session
        bot = Bot(
            token=CONTROL_BOT_TOKEN,
            session=telegram_session,
            default=DefaultBotProperties(
                parse_mode=ParseMode.HTML,
                link_preview_is_disabled=True,
            ),
        )
        notifier = Notifier(bot, ADMIN_TELEGRAM_ID)
        supervisor = FarmerSupervisor(
            storage,
            notifier,
            settings,
            mechanism_bundle_factory=lambda: default_legacy_fog_bundle(
                settings,
                storage,
                notifier,
            ),
        )
        resources.supervisor = supervisor
        control_bot = ControlBot(bot, storage, supervisor, settings)
        resources.control_bot = control_bot
        logger.info("Запуск панели управления FoG Farmer на aiogram %s", aiogram_version)
        await storage.add_event(
            "APPLICATION_STARTED",
            f"Контейнерное приложение запущено на aiogram {aiogram_version}",
        )
        await control_bot.start()
        if control_bot.polling_task is None:
            raise RuntimeError("Polling панели управления не был запущен")
        shutdown_event = asyncio.Event()
        _install_shutdown_handlers(shutdown_event)
        await _wait_for_shutdown(shutdown_event, control_bot.polling_task)
    finally:
        await resources.close()


if __name__ == "__main__":
    asyncio.run(main())
