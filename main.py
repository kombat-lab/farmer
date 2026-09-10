from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import AsyncExitStack, suppress

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
from logger_setup import setup_logging
from notifications import Notifier
from settings_service import SettingsService
from storage import Storage
from supervisor import FarmerSupervisor

logger = logging.getLogger("fog_farmer")


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

    async with AsyncExitStack() as cleanup:
        storage = Storage(DATABASE_PATH)
        cleanup.push_async_callback(storage.close)
        settings = SettingsService(storage)
        await settings.load()

        telegram_session = AiohttpSession(timeout=30)
        cleanup.push_async_callback(telegram_session.close)
        bot = Bot(
            token=CONTROL_BOT_TOKEN,
            session=telegram_session,
            default=DefaultBotProperties(
                parse_mode=ParseMode.HTML,
                link_preview_is_disabled=True,
            ),
        )
        notifier = Notifier(bot, ADMIN_TELEGRAM_ID)
        supervisor = FarmerSupervisor(storage, notifier, settings)

        async def stop_owned_farmer() -> None:
            if supervisor.farmer is not None:
                await supervisor.stop()

        cleanup.push_async_callback(stop_owned_farmer)
        control_bot = ControlBot(bot, storage, supervisor, settings)
        # Stop accepting new control actions before closing the farmer and its storage.
        cleanup.push_async_callback(control_bot.stop)
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


if __name__ == "__main__":
    asyncio.run(main())
