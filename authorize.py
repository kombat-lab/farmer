"""One-time interactive authorization of the persisted Telegram user session."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from telethon import TelegramClient

from config import API_HASH, API_ID, SESSION_NAME, prepare_runtime_directories
from session_lock import SessionLease


async def authorize() -> None:
    if not sys.stdin.isatty():
        raise RuntimeError(
            "Авторизацию нужно запускать в интерактивном терминале; "
            "для Docker добавьте -it."
        )
    if API_ID <= 0 or not API_HASH:
        raise RuntimeError("Задайте TELEGRAM_API_ID и TELEGRAM_API_HASH.")
    prepare_runtime_directories()
    lease = SessionLease(Path(f"{SESSION_NAME}.lock"))
    if not lease.acquire():
        raise RuntimeError("Сессия занята. Сначала остановите фармер.")
    try:
        client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    except BaseException:
        lease.release()
        raise
    try:
        await client.start()
        print("Сессия авторизована. Теперь можно запускать main.py.")
    finally:
        # Keep the lease until the client has closed its session file.
        await client.disconnect()
        lease.release()


if __name__ == "__main__":
    try:
        asyncio.run(authorize())
    except (RuntimeError, EOFError) as error:
        raise SystemExit(str(error)) from error
