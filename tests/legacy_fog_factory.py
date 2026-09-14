from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, Mock

from farmer import Farmer
from game_mechanisms import MechanismBundle
from legacy_fog_mechanisms import (
    LegacyFoGMechanismRuntime,
    default_legacy_fog_bundle,
)
from notifications import Notifier
from settings_service import SettingsService
from storage import Storage


def legacy_bundle(
    storage: Storage,
    notifier: Notifier,
    settings: SettingsService,
) -> MechanismBundle:
    return default_legacy_fog_bundle(settings, storage, notifier)


def create_test_client() -> Mock:
    client = Mock()
    client.disconnect = AsyncMock()
    client.connect = AsyncMock()
    client.is_user_authorized = AsyncMock(return_value=True)
    client.get_input_entity = AsyncMock(return_value="offline-peer")
    client.send_message = AsyncMock()
    return client


def legacy_farmer(
    storage: Storage,
    notifier: Notifier,
    settings: SettingsService,
    **kwargs: Any,
) -> Farmer:
    kwargs.setdefault("client", create_test_client())
    return Farmer(
        storage,
        notifier,
        settings,
        mechanism_bundle=legacy_bundle(storage, notifier, settings),
        **kwargs,
    )


def legacy_runtime(farmer: Farmer) -> LegacyFoGMechanismRuntime:
    runtime = farmer.mechanisms
    if not isinstance(runtime, LegacyFoGMechanismRuntime):
        raise AssertionError("Test requires the legacy FoG mechanism runtime")
    return cast(LegacyFoGMechanismRuntime, runtime)
