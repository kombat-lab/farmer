from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from farmer import Farmer
from notifications import Notifier
from settings_service import SettingsService
from storage import Storage
from tests.legacy_fog_factory import legacy_farmer

CHARACTER = "Kombat"
TARGETS = ["Черная мушка"]


class FakeButton:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeMessage:
    def __init__(
        self,
        text: str,
        buttons: list[list[str]],
        *,
        message_id: int = 1,
        edit_date: datetime | None = None,
    ) -> None:
        self.id = message_id
        self.edit_date = edit_date
        self.raw_text = text
        self.buttons = [[FakeButton(button) for button in row] for row in buttons]


def make_offline_farmer(storage: Storage, client: object | None = None) -> Farmer:
    if client is None:
        client = MagicMock()
        client.disconnect = AsyncMock()
        client.is_connected.return_value = False
    with patch("tests.legacy_fog_factory.create_test_client", return_value=client):
        return legacy_farmer(storage, MagicMock(spec=Notifier), SettingsService(storage))
