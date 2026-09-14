from __future__ import annotations

import asyncio
import unittest
from dataclasses import FrozenInstanceError, dataclass
from datetime import UTC, datetime, timedelta

from event_ingress import EventIngress, IngressClosedError
from fog_input import MAP_ACTION_SCOPE, FoGInputPolicy
from game_input import (
    AdmissionDecision,
    AdmissionOutcome,
    AdmissionResult,
    InboundEvent,
    InputDescriptor,
)
from message_snapshot import ButtonSnapshot, MessageSnapshot


@dataclass
class SourceButton:
    text: str
    data: object = None


@dataclass
class SourceMessage:
    id: int
    raw_text: str | None
    buttons: list[list[SourceButton]] | None
    edit_date: datetime | None = None


def snapshot(
    message_id: int,
    text: str,
    *,
    button: str | None = None,
    edit_date: datetime | None = None,
) -> MessageSnapshot:
    buttons = ((ButtonSnapshot(button),),) if button is not None else ()
    return MessageSnapshot(message_id, text, buttons, edit_date)


def map_snapshot(
    message_id: int,
    x: int,
    *,
    button: str | None = None,
    edit_date: datetime | None = None,
) -> MessageSnapshot:
    return snapshot(
        message_id,
        f"Позиция: ({x}, 0)\nМонстры на клетке: 0",
        button=button,
        edit_date=edit_date,
    )


class SnapshotTests(unittest.TestCase):
    def test_capture_copies_mutable_text_and_keyboard_without_rpc(self) -> None:
        button = SourceButton("attack")
        keyboard = [[button]]
        source = SourceMessage(1, "turn", keyboard)
        captured = MessageSnapshot.from_message(source)
        source.raw_text = "different turn"
        button.text = "heal"
        keyboard.append([SourceButton("new row")])
        self.assertEqual(captured.raw_text, "turn")
        self.assertEqual(captured.buttons, ((ButtonSnapshot("attack"),),))
        self.assertFalse(hasattr(captured, "click"))
        with self.assertRaises(FrozenInstanceError):
            captured.raw_text = "mutated"
        with self.assertRaises(FrozenInstanceError):
            captured.buttons[0][0].text = "mutated"

    def test_capture_retains_opaque_bytes_independently_of_source(self) -> None:
        original = bytes((0, 255, 128, 42))
        button = SourceButton("one button", original)
        captured = MessageSnapshot.from_message(SourceMessage(1, "turn", [[button]]))
        button.data = b"replacement"
        frozen_button = captured.buttons[0][0]
        self.assertEqual(frozen_button.callback_data, original)
        self.assertIsInstance(frozen_button.callback_data, bytes)
        with self.assertRaises(FrozenInstanceError):
            frozen_button.callback_data = b"mutated"
        self.assertEqual(MessageSnapshot.from_message(captured), captured)

    def test_capture_does_not_accept_mutable_or_nonbyte_callback_data(self) -> None:
        for data in (bytearray(b"mutable"), "text", 12, None):
            with self.subTest(data=data):
                button = SourceButton("one button", data)
                captured = MessageSnapshot.from_message(SourceMessage(1, "turn", [[button]]))
                self.assertIsNone(captured.buttons[0][0].callback_data)

    def test_capture_normalizes_absent_optional_data(self) -> None:
        captured = MessageSnapshot.from_message(SourceMessage(1, None, None))
        self.assertEqual(captured.raw_text, "")
        self.assertEqual(captured.buttons, ())
        self.assertEqual(captured.revision_timestamp, 0)


    def test_direct_construction_copies_rows_and_rejects_invalid_identity(self) -> None:
        rows = [[ButtonSnapshot("attack")]]
        captured = MessageSnapshot(1, "turn", rows)
        rows.append([ButtonSnapshot("heal")])
        self.assertEqual(captured.buttons, ((ButtonSnapshot("attack"),),))
        for invalid_id in (0, -1, True, 1.5, "1"):
            with self.subTest(message_id=invalid_id), self.assertRaises(ValueError):
                MessageSnapshot(invalid_id)
        with self.assertRaises(ValueError):
            MessageSnapshot(1, edit_date=datetime(2026, 9, 14))
        with self.assertRaises(ValueError):
            MessageSnapshot(1, buttons=(("not-a-snapshot",),))

    def test_button_snapshot_rejects_mutable_or_untyped_fields(self) -> None:
        with self.assertRaises(ValueError):
            ButtonSnapshot(12)
        with self.assertRaises(ValueError):
            ButtonSnapshot("attack", bytearray(b"mutable"))


class InputDescriptorTests(unittest.TestCase):
    def test_scope_collection_is_copied_to_an_immutable_tuple(self) -> None:
        scopes = ["map"]
        descriptor = InputDescriptor(
            snapshot(1, "boundary"), "fact", "state", True, advances_action_scopes=scopes
        )
        scopes.append("changed")
        self.assertEqual(descriptor.advances_action_scopes, ("map",))

    def test_invalid_named_or_advanced_scopes_are_rejected(self) -> None:
        for scope in ("", "  ", 1, False):
            with self.subTest(scope=scope):
                with self.assertRaises(ValueError):
                    InputDescriptor(snapshot(1, "state"), "fact", "state", True, scope)
                with self.assertRaises(ValueError):
                    InputDescriptor(
                        snapshot(1, "boundary"), "fact", "state", True,
                        advances_action_scopes=(scope,),
                    )


    def test_descriptor_and_result_runtime_invariants(self) -> None:
        message = snapshot(1, "fact")
        with self.assertRaises(ValueError):
            InputDescriptor(message, [], "state", True)
        with self.assertRaises(ValueError):
            InputDescriptor(message, "fact", "state", 1)
        with self.assertRaises(ValueError):
            InputDescriptor(
                message,
                "fact",
                "state",
                True,
                advances_action_scopes="map",
            )
        with self.assertRaises(ValueError):
            AdmissionResult(AdmissionOutcome.ACCEPTED)


class EventIngressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.ingress = EventIngress(FoGInputPolicy(character_name="Kombat"))

    async def accept(self, message: MessageSnapshot) -> InboundEvent:
        result = await self.ingress.accept(message)
        self.assertEqual(result.outcome, AdmissionOutcome.ACCEPTED)
        assert result.event is not None
        return result.event

    async def test_map_roundtrip_has_new_token_and_action_epoch(self) -> None:
        first = await self.accept(map_snapshot(1, 8, button="move"))
        middle = await self.accept(map_snapshot(1, 7, button="move"))
        returned = await self.accept(map_snapshot(1, 8, button="move"))
        self.assertEqual(first.snapshot, returned.snapshot)
        self.assertFalse(self.ingress.is_current(first.prompt_token))
        self.assertFalse(self.ingress.is_current(middle.prompt_token))
        self.assertTrue(self.ingress.is_current(returned.prompt_token))
        self.assertNotEqual(first.action_key, returned.action_key)
        self.assertEqual(self.ingress.action_epoch(MAP_ACTION_SCOPE), 3)
        observed = [await self.ingress.get() for _ in range(3)]
        self.assertEqual(observed, [first, middle, returned])

    async def test_identical_map_from_a_new_message_is_a_new_observation(self) -> None:
        first = await self.accept(map_snapshot(1, 8))
        await self.accept(map_snapshot(2, 7))
        returned = await self.accept(map_snapshot(3, 8))
        self.assertTrue(self.ingress.is_current(returned.prompt_token))
        self.assertNotEqual(first.action_key, returned.action_key)
        self.assertEqual(self.ingress.generation, 3)

    async def test_new_edit_can_reactivate_previous_message_after_another_map(self) -> None:
        stamp = datetime(2026, 9, 14, tzinfo=UTC)
        first = await self.accept(map_snapshot(10, 8, edit_date=stamp))
        middle = await self.accept(map_snapshot(11, 7))
        duplicate = await self.ingress.accept(map_snapshot(10, 8, edit_date=stamp))
        self.assertEqual(duplicate.outcome, AdmissionOutcome.DUPLICATE)
        self.assertIs(self.ingress.latest_prompt, middle)
        returned = await self.accept(map_snapshot(10, 8, edit_date=stamp + timedelta(seconds=1)))
        self.assertFalse(self.ingress.is_current(first.prompt_token))
        self.assertTrue(self.ingress.is_current(returned.prompt_token))
        self.assertEqual(self.ingress.action_epoch(MAP_ACTION_SCOPE), 3)

    async def test_older_revision_cannot_replace_registry_or_prompt(self) -> None:
        stamp = datetime(2026, 9, 14, tzinfo=UTC)
        current = await self.accept(map_snapshot(1, 8, edit_date=stamp))
        stale = await self.ingress.accept(map_snapshot(1, 7, edit_date=stamp-timedelta(seconds=1)))
        self.assertEqual(stale.outcome, AdmissionOutcome.STALE_REVISION)
        self.assertIs(self.ingress.latest_prompt, current)
        self.assertIs(self.ingress.latest_for(1), current.descriptor)
        self.assertEqual(self.ingress.qsize(), 1)

    async def test_original_delivery_after_an_edit_is_stale(self) -> None:
        current = await self.accept(
            map_snapshot(1, 8, edit_date=datetime(2026, 9, 14, tzinfo=UTC))
        )
        stale = await self.ingress.accept(map_snapshot(1, 7))
        self.assertEqual(stale.outcome, AdmissionOutcome.STALE_REVISION)
        self.assertTrue(self.ingress.is_current(current.prompt_token))

    async def test_equal_timestamps_do_not_hide_changed_content(self) -> None:
        stamp = datetime(2026, 9, 14, tzinfo=UTC)
        first = await self.accept(map_snapshot(1, 8, edit_date=stamp))
        current = await self.accept(map_snapshot(1, 7, edit_date=stamp))
        self.assertFalse(self.ingress.is_current(first.prompt_token))
        self.assertTrue(self.ingress.is_current(current.prompt_token))

    async def test_passive_health_keeps_actionable_prompt(self) -> None:
        current = await self.accept(map_snapshot(1, 8, button="move"))
        health = await self.accept(snapshot(2, "Ваше здоровье восстановилось до 700/780"))
        self.assertIsNone(health.prompt_token)
        self.assertIsNone(health.action_key)
        self.assertIs(self.ingress.latest_prompt, current)
        self.assertTrue(self.ingress.is_current(current.prompt_token))
        self.assertEqual(self.ingress.generation, 1)
        self.assertEqual(self.ingress.qsize(), 2)

    async def test_countdown_edit_updates_watermark_without_new_action(self) -> None:
        stamp = datetime(2026, 9, 14, tzinfo=UTC)
        first = await self.accept(
            snapshot(1, "Раунд 8\n⏳ Осталось: 20 сек.", button="attack", edit_date=stamp)
        )
        edited = snapshot(
            1, "Раунд 8\n⏳ Осталось: 19 сек.",
            button="attack", edit_date=stamp + timedelta(seconds=2),
        )
        result = await self.ingress.accept(edited)
        self.assertEqual(result.outcome, AdmissionOutcome.DUPLICATE)
        latest = self.ingress.latest_for(1)
        assert latest is not None
        self.assertEqual(latest.snapshot.edit_date, edited.edit_date)
        self.assertTrue(self.ingress.is_current(first.prompt_token))
        self.assertEqual(self.ingress.generation, 1)
        self.assertEqual(self.ingress.qsize(), 1)
        stale = await self.ingress.accept(
            snapshot(1, "different turn", edit_date=stamp + timedelta(seconds=1))
        )
        self.assertEqual(stale.outcome, AdmissionOutcome.STALE_REVISION)

    async def test_keyboard_change_replaces_token_but_preserves_action_identity(self) -> None:
        first = await self.accept(snapshot(1, "Раунд 8", button="attack"))
        changed = await self.accept(snapshot(1, "Раунд 8", button="heal"))
        returned = await self.accept(snapshot(1, "Раунд 8", button="attack"))
        self.assertFalse(self.ingress.is_current(first.prompt_token))
        self.assertFalse(self.ingress.is_current(changed.prompt_token))
        self.assertTrue(self.ingress.is_current(returned.prompt_token))
        self.assertEqual(first.action_key, changed.action_key)
        self.assertEqual(first.action_key, returned.action_key)
        self.assertEqual(self.ingress.action_epoch(MAP_ACTION_SCOPE), 0)

    async def test_callback_payload_refreshes_rpc_identity_without_replaying_action(self) -> None:
        first = await self.accept(
            MessageSnapshot(1, "Раунд 8", ((ButtonSnapshot("attack", b"first"),),))
        )
        changed_payload = MessageSnapshot(
            1, "Раунд 8", ((ButtonSnapshot("attack", b"second"),),)
        )
        result = await self.ingress.accept(changed_payload)
        self.assertEqual(result.outcome, AdmissionOutcome.ACCEPTED)
        assert result.event is not None
        self.assertFalse(self.ingress.is_current(first.prompt_token))
        self.assertTrue(self.ingress.is_current(result.event.prompt_token))
        self.assertEqual(first.action_key, result.event.action_key)
        self.assertEqual(self.ingress.qsize(), 2)

    async def test_map_keyboard_change_does_not_advance_action_epoch(self) -> None:
        first = await self.accept(map_snapshot(1, 8, button="a"))
        changed = await self.accept(map_snapshot(1, 8, button="b"))
        returned = await self.accept(map_snapshot(1, 8, button="a"))
        self.assertEqual(first.action_key, changed.action_key)
        self.assertEqual(first.action_key, returned.action_key)
        self.assertEqual(self.ingress.action_epoch(MAP_ACTION_SCOPE), 1)

    async def test_movement_ack_and_keyboard_edit_do_not_advance_map_action_scope(self) -> None:
        first = await self.accept(map_snapshot(1, 8, button="a"))
        ack = await self.accept(snapshot(2, "Шаг начат"))
        self.assertEqual(self.ingress.action_epoch(MAP_ACTION_SCOPE), 1)
        changed = await self.accept(map_snapshot(1, 8, button="b"))
        self.assertEqual(first.action_key, changed.action_key)
        self.assertEqual(self.ingress.action_epoch(MAP_ACTION_SCOPE), 1)
        self.assertFalse(self.ingress.is_current(ack.prompt_token))
        self.assertTrue(self.ingress.is_current(changed.prompt_token))

    async def test_countdown_after_movement_ack_does_not_reactivate_old_map(self) -> None:
        stamp = datetime(2026, 9, 14, tzinfo=UTC)
        text = "Позиция: (8, 0)\nМонстры на клетке: 0\n⏳ Осталось: 20 сек."
        first = await self.accept(snapshot(1, text, button="move", edit_date=stamp))
        ack = await self.accept(snapshot(2, "Шаг начат"))
        edited = snapshot(
            1, text.replace("20 сек.", "19 сек."), button="move",
            edit_date=stamp + timedelta(seconds=1),
        )
        result = await self.ingress.accept(edited)
        self.assertEqual(result.outcome, AdmissionOutcome.DUPLICATE)
        self.assertIs(self.ingress.latest_prompt, ack)
        self.assertFalse(self.ingress.is_current(first.prompt_token))
        self.assertEqual(self.ingress.action_epoch(MAP_ACTION_SCOPE), 1)

    async def test_target_menu_boundary_allows_return_to_same_map_with_new_epoch(self) -> None:
        stamp = datetime(2026, 9, 14, tzinfo=UTC)
        first = await self.accept(map_snapshot(1, 8, button="attack", edit_date=stamp))
        await self.accept(snapshot(2, "Выбери цель для нападения", button="back"))
        self.assertEqual(self.ingress.action_epoch(MAP_ACTION_SCOPE), 2)
        duplicate_old_delivery = await self.ingress.accept(first.snapshot)
        self.assertEqual(duplicate_old_delivery.outcome, AdmissionOutcome.DUPLICATE)
        returned = await self.accept(
            map_snapshot(1, 8, button="attack", edit_date=stamp + timedelta(seconds=1))
        )
        self.assertNotEqual(first.action_key, returned.action_key)
        self.assertEqual(self.ingress.action_epoch(MAP_ACTION_SCOPE), 2)
        self.assertTrue(self.ingress.is_current(returned.prompt_token))

    async def test_blessing_menu_boundary_allows_return_to_same_map(self) -> None:
        stamp = datetime(2026, 9, 14, tzinfo=UTC)
        first = await self.accept(map_snapshot(1, 8, button="Небоевые навыки", edit_date=stamp))
        await self.accept(snapshot(2, "Небоевые навыки", button="✨ Благословение"))
        returned = await self.accept(
            map_snapshot(1, 8, button="Небоевые навыки", edit_date=stamp + timedelta(seconds=1))
        )
        self.assertNotEqual(first.action_key, returned.action_key)
        self.assertEqual(self.ingress.action_epoch(MAP_ACTION_SCOPE), 2)
        self.assertTrue(self.ingress.is_current(returned.prompt_token))

    async def test_battle_and_target_boundaries_advance_only_on_accepted_facts(self) -> None:
        boundary_texts = (
            "Выбери цель для нападения",
            "Выберите цель для лечения",
            "Вы напали: Бронзовик",
            "Выберите навык:",
            "Бой завершён. Победа",
            "Монстр не найден на текущей клетке",
        )
        for text in boundary_texts:
            with self.subTest(text=text):
                self.ingress = EventIngress(FoGInputPolicy(character_name="Kombat"))
                await self.accept(map_snapshot(1, 8))
                await self.accept(snapshot(2, text))
                self.assertEqual(self.ingress.action_epoch(MAP_ACTION_SCOPE), 2)
                result = await self.ingress.accept(snapshot(2, text))
                self.assertEqual(result.outcome, AdmissionOutcome.DUPLICATE)
                self.assertEqual(self.ingress.action_epoch(MAP_ACTION_SCOPE), 2)

    async def test_combat_return_keeps_historical_action_identity(self) -> None:
        first = await self.accept(snapshot(1, "Раунд 8", button="attack"))
        await self.accept(snapshot(1, "Раунд 9", button="attack"))
        returned = await self.accept(snapshot(1, "Раунд 8", button="attack"))
        self.assertEqual(first.action_key, returned.action_key)
        self.assertFalse(self.ingress.is_current(first.prompt_token))

    async def test_prompt_tokens_cannot_be_reused_in_another_session(self) -> None:
        message = map_snapshot(1, 8)
        first = await self.accept(message)
        other = EventIngress(FoGInputPolicy(character_name="Kombat"))
        result = await other.accept(message)
        assert result.event is not None
        self.assertFalse(other.is_current(first.prompt_token))
        self.assertTrue(other.is_current(result.event.prompt_token))

    async def test_backpressure_preserves_facts_and_invalidates_old_action_early(self) -> None:
        self.ingress = EventIngress(FoGInputPolicy(character_name="Kombat"), capacity=1)
        victory = await self.accept(snapshot(1, "Победа!"))
        producer = asyncio.create_task(self.ingress.accept(map_snapshot(2, 8)))
        await asyncio.sleep(0)
        self.assertFalse(producer.done())
        self.assertFalse(self.ingress.is_current(victory.prompt_token))
        self.assertEqual(self.ingress.qsize(), 1)
        self.assertIs(await self.ingress.get(), victory)
        self.ingress.task_done()
        result = await asyncio.wait_for(producer, timeout=1)
        self.assertTrue(result.accepted)
        self.assertIs(await self.ingress.get(), result.event)
        self.ingress.task_done()
        await self.ingress.join()

    async def test_multiple_blocked_producers_keep_arrival_order(self) -> None:
        self.ingress = EventIngress(FoGInputPolicy(character_name="Kombat"), capacity=1)
        await self.accept(snapshot(1, "first"))
        producers = [
            asyncio.create_task(self.ingress.accept(snapshot(index, f"fact {index}")))
            for index in range(2, 6)
        ]
        await asyncio.sleep(0)
        ids = []
        for _ in range(5):
            event = await asyncio.wait_for(self.ingress.get(), timeout=1)
            ids.append(event.snapshot.id)
            self.ingress.task_done()
        results = await asyncio.gather(*producers)
        self.assertTrue(all(result.accepted for result in results))
        self.assertEqual(ids, list(range(1, 6)))
        await self.ingress.join()

    async def test_join_waits_for_processing_not_only_queue_consumption(self) -> None:
        await self.accept(snapshot(1, "fact"))
        await self.ingress.get()
        joining = asyncio.create_task(self.ingress.join())
        await asyncio.sleep(0)
        self.assertFalse(joining.done())
        self.ingress.task_done()
        await asyncio.wait_for(joining, timeout=1)
        with self.assertRaises(ValueError):
            self.ingress.task_done()

    async def test_close_wakes_backpressured_producer_without_losing_queued_fact(self) -> None:
        self.ingress = EventIngress(FoGInputPolicy(character_name="Kombat"), capacity=1)
        first = await self.accept(snapshot(1, "fact"))
        blocked = asyncio.create_task(self.ingress.accept(snapshot(2, "next")))
        await asyncio.sleep(0)
        self.ingress.close()
        result = await asyncio.wait_for(blocked, timeout=1)
        self.assertEqual(result.outcome, AdmissionOutcome.CLOSED)
        self.assertFalse(self.ingress.is_current(first.prompt_token))
        self.assertIs(await self.ingress.get(), first)
        self.ingress.task_done()
        await self.ingress.join()
        with self.assertRaises(IngressClosedError):
            await self.ingress.get()

    async def test_close_wakes_waiting_consumer_and_rejects_new_input(self) -> None:
        consumer = asyncio.create_task(self.ingress.get())
        await asyncio.sleep(0)
        self.ingress.close()
        with self.assertRaises(IngressClosedError):
            await asyncio.wait_for(consumer, timeout=1)
        result = await self.ingress.accept(snapshot(1, "fact"))
        self.assertEqual(result.outcome, AdmissionOutcome.CLOSED)
        self.assertEqual(self.ingress.registry_size, 0)

    async def test_registry_is_bounded_independently_of_fact_queue(self) -> None:
        self.ingress = EventIngress(FoGInputPolicy(character_name="Kombat"), registry_capacity=2)
        for index in range(1, 4):
            await self.accept(snapshot(index, f"fact {index}"))
        self.assertEqual(self.ingress.registry_size, 2)
        self.assertIsNone(self.ingress.latest_for(1))
        self.assertEqual(self.ingress.qsize(), 3)

    async def test_reprocessing_preserves_token_and_does_not_increment_generation(self) -> None:
        current = await self.accept(map_snapshot(1, 8))
        await self.ingress.get()
        self.ingress.task_done()
        self.assertTrue(self.ingress.requeue_latest())
        self.assertIs(await self.ingress.get(), current)
        self.ingress.task_done()
        self.assertEqual(self.ingress.generation, 1)
        self.assertEqual(self.ingress.action_epoch(MAP_ACTION_SCOPE), 1)
        self.ingress.close()
        self.assertFalse(self.ingress.requeue_latest())

    async def test_cancelled_backpressured_input_closes_without_reviving_old_prompt(self) -> None:
        self.ingress = EventIngress(FoGInputPolicy(character_name="Kombat"), capacity=1)
        first = await self.accept(map_snapshot(1, 8))
        blocked = asyncio.create_task(self.ingress.accept(map_snapshot(2, 7)))
        await asyncio.sleep(0)
        self.assertFalse(blocked.done())
        observed = self.ingress.latest_prompt
        assert observed is not None
        self.assertFalse(self.ingress.is_current(first.prompt_token))
        blocked.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await blocked
        self.assertTrue(self.ingress.closed)
        self.assertFalse(self.ingress.is_current(first.prompt_token))
        self.assertFalse(self.ingress.is_current(observed.prompt_token))
        self.assertFalse(self.ingress.requeue_latest())
        retried = await self.ingress.accept(map_snapshot(2, 7))
        self.assertEqual(retried.outcome, AdmissionOutcome.CLOSED)
        self.assertIs(await self.ingress.get(), first)
        self.ingress.task_done()
        await self.ingress.join()
        with self.assertRaises(IngressClosedError):
            await self.ingress.get()

    async def test_cancelled_producer_wakes_other_backpressured_producers(self) -> None:
        self.ingress = EventIngress(FoGInputPolicy(character_name="Kombat"), capacity=1)
        await self.accept(snapshot(1, "first"))
        publishing = asyncio.create_task(self.ingress.accept(snapshot(2, "second")))
        waiting = asyncio.create_task(self.ingress.accept(snapshot(3, "third")))
        await asyncio.sleep(0)
        publishing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await publishing
        result = await asyncio.wait_for(waiting, timeout=1)
        self.assertEqual(result.outcome, AdmissionOutcome.CLOSED)
        self.assertEqual(self.ingress.qsize(), 1)

    async def test_passive_burst_cannot_evict_current_prompt(self) -> None:
        self.ingress = EventIngress(FoGInputPolicy(character_name="Kombat"), registry_capacity=2)
        current = await self.accept(map_snapshot(1, 8))
        for message_id in range(2, 8):
            await self.accept(snapshot(message_id, "Ваше здоровье восстановилось до 700/780"))
            self.assertTrue(self.ingress.is_current(current.prompt_token))
            self.assertEqual(self.ingress.registry_size, 2)
        self.assertIs(self.ingress.latest_for(1), current.descriptor)
        self.assertIsNone(self.ingress.latest_for(2))
        self.assertEqual(self.ingress.qsize(), 7)
        replacement = await self.accept(map_snapshot(8, 7))
        self.assertTrue(self.ingress.is_current(replacement.prompt_token))
        self.assertFalse(self.ingress.is_current(current.prompt_token))
        self.assertEqual(self.ingress.registry_size, 2)

    async def test_capacity_one_retains_prompt_and_queues_passive_fact(self) -> None:
        self.ingress = EventIngress(FoGInputPolicy(character_name="Kombat"), registry_capacity=1)
        current = await self.accept(map_snapshot(1, 8))
        await self.accept(snapshot(2, "Ваше здоровье восстановилось до 700/780"))
        self.assertTrue(self.ingress.is_current(current.prompt_token))
        self.assertEqual(self.ingress.registry_size, 1)
        self.assertEqual(self.ingress.qsize(), 2)
        replacement = await self.accept(map_snapshot(3, 7))
        self.assertTrue(self.ingress.is_current(replacement.prompt_token))
        self.assertIsNone(self.ingress.latest_for(1))
        self.assertEqual(self.ingress.registry_size, 1)

    async def test_passive_scope_boundary_invalidates_and_cannot_requeue_prompt(self) -> None:
        class BoundaryPolicy:
            def describe(self, message: MessageSnapshot) -> InputDescriptor:
                if message.raw_text == "boundary":
                    return InputDescriptor(
                        message,
                        "boundary",
                        "boundary",
                        False,
                        advances_action_scopes=("scope",),
                    )
                return InputDescriptor(
                    message,
                    message.raw_text,
                    message.raw_text,
                    True,
                    "scope",
                )

            def admit(
                self,
                previous: InputDescriptor | None,
                current_prompt: InputDescriptor | None,
                incoming: InputDescriptor,
            ) -> AdmissionDecision:
                return AdmissionDecision.ACCEPT

        ingress = EventIngress(BoundaryPolicy())
        first = await ingress.accept(snapshot(1, "action"))
        assert first.event is not None
        await ingress.accept(snapshot(2, "boundary"))
        self.assertEqual(ingress.action_epoch("scope"), 2)
        self.assertFalse(ingress.is_current(first.event.prompt_token))
        self.assertIsNone(ingress.latest_prompt)
        self.assertFalse(ingress.requeue_latest())

    async def test_malformed_policy_results_fail_before_publication(self) -> None:
        class WrongDescriptorPolicy:
            def describe(self, message: MessageSnapshot) -> object:
                return object()

            def admit(self, previous: object, current: object, incoming: object) -> object:
                return AdmissionDecision.ACCEPT

        ingress = EventIngress(WrongDescriptorPolicy())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            await ingress.accept(snapshot(1, "fact"))
        self.assertEqual(ingress.qsize(), 0)
        self.assertEqual(ingress.registry_size, 0)

        class WrongDecisionPolicy:
            def describe(self, message: MessageSnapshot) -> InputDescriptor:
                return InputDescriptor(message, "fact", "state", True)

            def admit(
                self,
                previous: InputDescriptor | None,
                current: InputDescriptor | None,
                incoming: InputDescriptor,
            ) -> object:
                return True

        ingress = EventIngress(WrongDecisionPolicy())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            await ingress.accept(snapshot(1, "fact"))
        self.assertEqual(ingress.qsize(), 0)
        self.assertEqual(ingress.registry_size, 0)

    def test_invalid_capacities_are_rejected(self) -> None:
        policy = FoGInputPolicy(character_name="Kombat")
        for capacity, registry_capacity in [
            (0, 1), (1, 0), (-1, 1), (1, -1), (True, 1), (1, 1.5)
        ]:
            with self.subTest(capacity=capacity, registry_capacity=registry_capacity):
                with self.assertRaises(ValueError):
                    EventIngress(policy, capacity=capacity, registry_capacity=registry_capacity)


class IndependentPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_accepts_non_fog_semantics(self) -> None:
        class CounterPolicy:
            def describe(self, message: MessageSnapshot) -> InputDescriptor:
                return InputDescriptor(
                    message, message.raw_text, message.raw_text, True, "counter"
                )

            def admit(
                self,
                previous: InputDescriptor | None,
                current_prompt: InputDescriptor | None,
                incoming: InputDescriptor,
            ) -> AdmissionDecision:
                return AdmissionDecision.ACCEPT

        ingress = EventIngress(CounterPolicy())
        first = await ingress.accept(snapshot(1, "100"))
        second = await ingress.accept(snapshot(1, "101"))
        assert first.event is not None and second.event is not None
        self.assertEqual(ingress.action_epoch("counter"), 2)
        self.assertFalse(ingress.is_current(first.event.prompt_token))
        self.assertTrue(ingress.is_current(second.event.prompt_token))


    async def test_scope_boundaries_are_policy_data_without_game_specific_strings(self) -> None:
        class CounterPolicy:
            def describe(self, message: MessageSnapshot) -> InputDescriptor:
                if message.raw_text == "boundary":
                    return InputDescriptor(
                        message, message.raw_text, message.raw_text, True,
                        advances_action_scopes=("counter", "counter"),
                    )
                scope = "counter" if message.raw_text.isdecimal() else None
                return InputDescriptor(message, message.raw_text, message.raw_text, True, scope)

            def admit(
                self,
                previous: InputDescriptor | None,
                current_prompt: InputDescriptor | None,
                incoming: InputDescriptor,
            ) -> AdmissionDecision:
                return AdmissionDecision.ACCEPT

        ingress = EventIngress(CounterPolicy())
        first = await ingress.accept(snapshot(1, "100"))
        await ingress.accept(snapshot(2, "transient"))
        repeated = await ingress.accept(snapshot(1, "100"))
        assert first.event is not None and repeated.event is not None
        self.assertEqual(first.event.action_key, repeated.event.action_key)
        await ingress.accept(snapshot(3, "boundary"))
        self.assertEqual(ingress.action_epoch("counter"), 2)
        returned = await ingress.accept(snapshot(1, "100"))
        assert returned.event is not None
        self.assertNotEqual(first.event.action_key, returned.event.action_key)
        self.assertEqual(ingress.action_epoch("counter"), 2)


if __name__ == "__main__":
    unittest.main()
