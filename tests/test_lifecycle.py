from __future__ import annotations

import asyncio
import unittest
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

import authorize as authorization
import config
import main as application
from main import _wait_for_shutdown


class ConfigurationTests(unittest.TestCase):
    def test_runtime_validation_reports_all_missing_secrets(self) -> None:
        with patch.multiple(
            config,
            API_ID=0,
            API_HASH="",
            CONTROL_BOT_TOKEN="",
            ADMIN_TELEGRAM_ID=0,
        ):
            with self.assertRaises(RuntimeError) as raised:
                config.validate_runtime_config()

        message = str(raised.exception)
        self.assertIn("TELEGRAM_API_ID", message)
        self.assertIn("TELEGRAM_API_HASH", message)
        self.assertIn("CONTROL_BOT_TOKEN", message)
        self.assertIn("ADMIN_TELEGRAM_ID", message)

    def test_runtime_directories_are_created_explicitly(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db_dir = root / "db"
            session_dir = root / "telegram"
            log_dir = root / "logs"
            with patch.multiple(
                config,
                DB_DIR=db_dir,
                SESSION_DIR=session_dir,
                LOG_DIRECTORY=str(log_dir),
            ):
                config.prepare_runtime_directories()

            self.assertTrue(db_dir.is_dir())
            self.assertTrue(session_dir.is_dir())
            self.assertTrue(log_dir.is_dir())


class ApplicationLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_signal_releases_main_wait(self) -> None:
        never_finishes = asyncio.Event()
        polling_task = asyncio.create_task(never_finishes.wait())
        shutdown_event = asyncio.Event()
        shutdown_event.set()

        try:
            await _wait_for_shutdown(shutdown_event, polling_task)
            self.assertFalse(polling_task.done())
        finally:
            polling_task.cancel()
            with suppress(asyncio.CancelledError):
                await polling_task

    async def test_polling_failure_is_propagated(self) -> None:
        async def fail() -> None:
            raise RuntimeError("polling failed")

        polling_task = asyncio.create_task(fail())

        with self.assertRaisesRegex(RuntimeError, "polling failed"):
            await _wait_for_shutdown(asyncio.Event(), polling_task)


class AuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorization_requires_terminal_before_creating_client(self) -> None:
        with patch.object(authorization.sys.stdin, "isatty", return_value=False):
            with patch.object(authorization, "TelegramClient") as client_factory:
                with self.assertRaisesRegex(RuntimeError, "-it"):
                    await authorization.authorize()
        client_factory.assert_not_called()

    async def test_interactive_bootstrap_disconnects_before_releasing_lease(self) -> None:
        order: list[str] = []
        client = Mock()
        client.start = AsyncMock()
        client.disconnect = AsyncMock(side_effect=lambda: order.append("disconnect"))
        lease = Mock()
        lease.acquire.return_value = True
        lease.release.side_effect = lambda: order.append("release")
        with (
            patch.object(authorization.sys.stdin, "isatty", return_value=True),
            patch.multiple(authorization, API_ID=1, API_HASH="test", SESSION_NAME="test"),
            patch.object(authorization, "prepare_runtime_directories"),
            patch.object(authorization, "TelegramClient", return_value=client),
            patch.object(authorization, "SessionLease", return_value=lease),
            patch("builtins.print"),
        ):
            await authorization.authorize()
        client.start.assert_awaited_once()
        self.assertEqual(order, ["disconnect", "release"])

    async def test_failed_bootstrap_still_closes_client(self) -> None:
        client = Mock()
        client.start = AsyncMock(side_effect=EOFError("input closed"))
        client.disconnect = AsyncMock()
        lease = Mock()
        lease.acquire.return_value = True
        with (
            patch.object(authorization.sys.stdin, "isatty", return_value=True),
            patch.multiple(authorization, API_ID=1, API_HASH="test", SESSION_NAME="test"),
            patch.object(authorization, "prepare_runtime_directories"),
            patch.object(authorization, "TelegramClient", return_value=client),
            patch.object(authorization, "SessionLease", return_value=lease),
        ):
            with self.assertRaises(EOFError):
                await authorization.authorize()
        client.disconnect.assert_awaited_once()
        lease.release.assert_called_once()


class StartupCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_settings_failure_closes_storage(self) -> None:
        storage = Mock()
        storage.close = AsyncMock()
        settings = Mock()
        settings.load = AsyncMock(side_effect=RuntimeError("settings failed"))
        with (
            patch.object(application, "validate_runtime_config"),
            patch.object(application, "prepare_runtime_directories"),
            patch.object(application, "setup_logging", return_value=Mock()),
            patch.object(application, "Storage", return_value=storage),
            patch.object(application, "SettingsService", return_value=settings),
            patch.object(application, "Bot") as bot_factory,
        ):
            with self.assertRaisesRegex(RuntimeError, "settings failed"):
                await application.main()
        storage.close.assert_awaited_once()
        bot_factory.assert_not_called()

    async def test_bot_creation_failure_closes_session_and_storage(self) -> None:
        storage = Mock()
        storage.close = AsyncMock()
        settings = Mock()
        settings.load = AsyncMock()
        session = Mock()
        session.close = AsyncMock()
        with (
            patch.object(application, "validate_runtime_config"),
            patch.object(application, "prepare_runtime_directories"),
            patch.object(application, "setup_logging", return_value=Mock()),
            patch.object(application, "Storage", return_value=storage),
            patch.object(application, "SettingsService", return_value=settings),
            patch.object(application, "AiohttpSession", return_value=session),
            patch.object(application, "Bot", side_effect=ValueError("invalid token")),
        ):
            with self.assertRaisesRegex(ValueError, "invalid token"):
                await application.main()
        session.close.assert_awaited_once()
        storage.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
