from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from storage import SCHEMA_VERSION, Storage


class StorageRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.storage = Storage(Path(":memory:"))

    async def asyncTearDown(self) -> None:
        await self.storage.close()

    async def test_daily_window_contains_complete_moscow_days(self) -> None:
        for stamp in (
            "2026-09-09T20:00:00+00:00",  # Previous Moscow day; exclude.
            "2026-09-09T21:00:00+00:00",  # Moscow midnight; include.
            "2026-09-10T00:00:00+00:00",
        ):
            await self.storage.increment_telegram_activity(stamp, {"outgoing_total": 1})
        with patch("storage.datetime") as clock:
            clock.now.return_value = datetime(2026, 9, 10, 12, tzinfo=UTC)
            days = await self.storage.get_telegram_activity_daily(days=1)
        self.assertEqual([(day["day"], day["outgoing_total"]) for day in days], [
            ("2026-09-10", 2),
        ])

    async def test_moscow_day_changes_before_utc_day(self) -> None:
        await self.storage.increment_telegram_activity(
            "2026-09-10T20:00:00+00:00", {"outgoing_total": 1}
        )
        with patch("storage.datetime") as clock:
            clock.now.return_value = datetime(2026, 9, 10, 22, tzinfo=UTC)
            self.assertEqual(await self.storage.get_telegram_activity_daily(days=1), [])

    async def test_fourteen_day_window_keeps_first_local_midnight(self) -> None:
        await self.storage.increment_telegram_activity(
            "2026-08-27T21:00:00+00:00", {"outgoing_total": 4}
        )
        with patch("storage.datetime") as clock:
            clock.now.return_value = datetime(2026, 9, 10, 12, tzinfo=UTC)
            days = await self.storage.get_telegram_activity_daily(days=14)
        self.assertEqual([(day["day"], day["outgoing_total"]) for day in days], [
            ("2026-08-28", 4),
        ])

    async def test_new_session_dashboard_has_numeric_zero_totals(self) -> None:
        await self.storage.start_session(cycles_count=1, moves_per_cycle=100)
        dashboard = await self.storage.get_statistics_dashboard()
        self.assertEqual(dashboard["battle"]["wins"], 0)
        self.assertEqual(dashboard["battle"]["defeats"], 0)


class StorageSchemaVersionTests(unittest.TestCase):
    def test_schema_version_is_checked_while_write_transaction_is_held(self) -> None:
        reads_in_transaction: list[bool] = []
        storage = object.__new__(Storage)
        storage.connection = sqlite3.connect(":memory:")
        storage.connection.row_factory = sqlite3.Row

        def capture_version_read(statement: str) -> None:
            if statement.strip().upper() == "PRAGMA USER_VERSION":
                reads_in_transaction.append(storage.connection.in_transaction)

        storage.connection.set_trace_callback(capture_version_read)
        try:
            storage._create_schema()
            self.assertEqual(reads_in_transaction, [True])
        finally:
            storage.connection.close()

    def test_future_schema_is_rejected_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "future.sqlite3"
            connection = sqlite3.connect(path)
            original_journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            connection.execute("CREATE TABLE future_data(value TEXT NOT NULL)")
            connection.execute("INSERT INTO future_data VALUES ('preserve')")
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
            connection.commit()
            connection.close()

            with self.assertRaisesRegex(RuntimeError, "новее поддерживаемой"):
                Storage(path)

            connection = sqlite3.connect(path)
            try:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0],
                    SCHEMA_VERSION + 1,
                )
                self.assertEqual(
                    connection.execute("PRAGMA journal_mode").fetchone()[0],
                    original_journal_mode,
                )
                self.assertEqual(
                    connection.execute("SELECT value FROM future_data").fetchone()[0],
                    "preserve",
                )
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertEqual(tables, {"future_data"})
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
