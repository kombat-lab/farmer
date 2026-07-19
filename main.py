from __future__ import annotations

import asyncio

from aiogram import Bot
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
)
from control_bot import ControlBot
from logger_setup import setup_logging
from notifications import Notifier
from settings_service import SettingsService
from storage import Storage
from supervisor import FarmerSupervisor

logger = setup_logging(
    log_directory=LOG_DIRECTORY,
    log_filename=LOG_FILENAME,
    max_bytes=LOG_MAX_BYTES,
    backup_count=LOG_BACKUP_COUNT,
)


async def main() -> None:
    storage = Storage(DATABASE_PATH)
    settings = SettingsService(storage)
    await settings.load()

    telegram_session = AiohttpSession(timeout=30)
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
    control_bot = ControlBot(bot, storage, supervisor, settings)
    notifier.set_keyboard_provider(control_bot.current_keyboard)

    logger.info("Запуск панели управления FoG Farmer на aiogram")
    await storage.add_event(
        "APPLICATION_STARTED",
        "Контейнерное приложение запущено на aiogram",
    )

    await control_bot.start()
    try:
        await asyncio.Event().wait()
    finally:
        if supervisor.is_running():
            await supervisor.stop()
        await control_bot.stop()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
