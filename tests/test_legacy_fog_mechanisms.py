from __future__ import annotations

import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from discovery import DiscoveryEventKind
from farmer import Farmer
from game_input import InboundEvent
from message_snapshot import MessageSnapshot
from notifications import Notifier
from settings_service import SettingsService
from storage import Storage
from tests.combat_runtime_harness import Message
from tests.legacy_fog_factory import legacy_farmer, legacy_runtime


@dataclass(frozen=True, slots=True)
class DiscoveryProbe:
    event: InboundEvent
    kind: DiscoveryEventKind = DiscoveryEventKind.STATE

    @property
    def snapshot(self) -> MessageSnapshot:
        return self.event.snapshot


class LegacyFoGMechanismTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.storage = Storage(Path(":memory:"))
        self.settings = SettingsService(self.storage)
        await self.settings.load()
        self.notifier = MagicMock(spec=Notifier)
        self.notifier.send = AsyncMock()
        self.client = MagicMock()
        self.client.disconnect = AsyncMock()
        with patch("tests.legacy_fog_factory.create_test_client", return_value=self.client):
            self.farmer: Farmer = legacy_farmer(
                self.storage,
                self.notifier,
                self.settings,
            )
        self.runtime = legacy_runtime(self.farmer)

    async def asyncTearDown(self) -> None:
        await self.farmer.stop("test cleanup")
        await self.storage.close()

    async def test_partial_initialization_rolls_back_once_and_close_is_idempotent(
        self,
    ) -> None:
        discovery_initialize = AsyncMock(return_value=0)
        combat_initialize = AsyncMock(side_effect=RuntimeError("combat init failed"))
        combat_persist = AsyncMock()
        with (
            patch.object(self.runtime.discovery, "initialize", discovery_initialize),
            patch.object(self.runtime.combat, "initialize", combat_initialize),
            patch.object(self.runtime.combat, "persist", combat_persist),
        ):
            with self.assertRaisesRegex(RuntimeError, "combat init failed"):
                await self.runtime.initialize()
            await self.runtime.aclose()
            await self.runtime.aclose()

        discovery_initialize.assert_awaited_once()
        combat_initialize.assert_awaited_once()
        combat_persist.assert_awaited_once()

    async def test_discovery_route_has_precedence_for_overlapping_event(self) -> None:
        message = Message(41, "overlapping future game state")
        await self.farmer.enqueue_message(message)
        event = self.farmer.input_event(message)
        assert event is not None
        observation = DiscoveryProbe(event)
        discovery_handle = AsyncMock(return_value=True)
        combat_observe = MagicMock(
            side_effect=AssertionError("combat route must not inspect claimed discovery event")
        )

        with (
            patch.object(
                self.runtime.discovery,
                "observe_message",
                return_value=observation,
            ),
            patch.object(
                self.runtime.discovery,
                "handle_message",
                discovery_handle,
            ),
            patch.object(
                self.runtime.combat.legacy_controller,
                "observe_message",
                combat_observe,
            ),
        ):
            handled = await self.runtime.handle(event)

        self.assertTrue(handled)
        discovery_handle.assert_awaited_once_with(observation)
        combat_observe.assert_not_called()
