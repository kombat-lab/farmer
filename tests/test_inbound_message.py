from __future__ import annotations

import asyncio
import subprocess
import sys
import unittest
from dataclasses import FrozenInstanceError, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from event_ingress import EventIngress
from fog_input import FoGInputPolicy
from game_message import GameMessage, ReadableGameMessage
from inbound_message import InboundMessage
from message_snapshot import ButtonSnapshot, MessageSnapshot


@dataclass
class MutableButton:
    text: str
    data: bytes | None = None


@dataclass
class MutableMessage:
    id: int
    raw_text: str
    buttons: list[list[MutableButton]]
    edit_date: datetime | None = None
    calls: list[tuple[int, int]] = field(default_factory=list)
    result: object = None
    error: BaseException | None = None

    async def click(self, row: int, column: int) -> object:
        self.calls.append((row, column))
        if self.error is not None:
            raise self.error
        return self.result


class InboundMessageTests(unittest.IsolatedAsyncioTestCase):
    async def make_envelope(self, source: MutableMessage) -> InboundMessage:
        ingress = EventIngress(FoGInputPolicy(character_name="Kombat"))
        result = await ingress.accept(MessageSnapshot.from_message(source))
        assert result.event is not None
        return InboundMessage(result.event, source)

    def test_snapshot_and_envelope_import_without_telethon(self) -> None:
        script = """
import importlib.abc
import sys

class BlockTransport(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] == 'telethon':
            raise AssertionError('Immutable input imported Telethon: ' + fullname)
        return None

sys.meta_path.insert(0, BlockTransport())
sys.path.insert(0, sys.argv[1])
import message_snapshot
import inbound_message
"""
        project = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(project)],
            capture_output=True, text=True, check=False, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    async def test_source_mutation_cannot_change_parser_input_or_replace_rpc_handle(self) -> None:
        stamp = datetime(2026, 9, 14, tzinfo=UTC)
        button = MutableButton("attack", b"opaque")
        callback_result = object()
        source = MutableMessage(1, "Раунд 8", [[button]], stamp, result=callback_result)
        envelope = await self.make_envelope(source)
        source.id = 999
        source.raw_text = "different turn"
        source.edit_date = stamp + timedelta(seconds=1)
        button.text = "different button"
        button.data = b"replacement"
        source.buttons.clear()
        self.assertEqual(envelope.id, 1)
        self.assertEqual(envelope.raw_text, "Раунд 8")
        self.assertEqual(envelope.buttons, ((ButtonSnapshot("attack", b"opaque"),),))
        self.assertEqual(envelope.edit_date, stamp)
        self.assertIs(envelope.snapshot, envelope.event.snapshot)
        result = await envelope.click(2, 3)
        self.assertIs(result, callback_result)
        self.assertEqual(source.calls, [(2, 3)])
        self.assertIs(envelope.rpc, source)

    async def test_envelope_is_immutable_and_rpc_is_absent_from_repr(self) -> None:
        source = MutableMessage(1, "prompt", [], result="callback secret")
        envelope = await self.make_envelope(source)
        with self.assertRaises(FrozenInstanceError):
            envelope.rpc = MutableMessage(2, "replacement", [])
        self.assertNotIn("callback secret", repr(envelope))

    async def test_rpc_errors_and_cancellation_propagate_without_policy(self) -> None:
        for error in (ValueError("rpc error"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                source = MutableMessage(1, "prompt", [], error=error)
                envelope = await self.make_envelope(source)
                with self.assertRaises(type(error)):
                    await envelope.click(0, 1)
                self.assertEqual(source.calls, [(0, 1)])

    async def test_envelope_satisfies_parser_and_existing_message_protocols(self) -> None:
        source = MutableMessage(1, "prompt", [[MutableButton("button")]])
        envelope = await self.make_envelope(source)
        readable: ReadableGameMessage = envelope
        actionable: GameMessage = envelope
        self.assertEqual(readable.raw_text, "prompt")
        await actionable.click(0, 0)
        self.assertEqual(source.calls, [(0, 0)])


if __name__ == '__main__':
    unittest.main()
