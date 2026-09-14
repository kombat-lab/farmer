from __future__ import annotations

import asyncio
import unittest

from event_ingress import EventIngress, IngressClosedError
from game_input import AdmissionDecision, AdmissionOutcome, InboundEvent, InputDescriptor
from message_snapshot import MessageSnapshot


class FactPolicy:
    def describe(self, snapshot: MessageSnapshot) -> InputDescriptor:
        return InputDescriptor(snapshot, snapshot.id, snapshot.id, True)

    def admit(
        self,
        previous: InputDescriptor | None,
        current_prompt: InputDescriptor | None,
        incoming: InputDescriptor,
    ) -> AdmissionDecision:
        return AdmissionDecision.ACCEPT


class EventIngressShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def accepted(self, ingress: EventIngress, message_id: int) -> InboundEvent:
        result = await ingress.accept(MessageSnapshot(message_id, f"fact {message_id}"))
        self.assertIs(result.outcome, AdmissionOutcome.ACCEPTED)
        assert result.event is not None
        return result.event

    async def test_close_rejects_every_blocked_producer_and_drains_accepted_order(self) -> None:
        ingress = EventIngress(FactPolicy(), capacity=2)
        accepted = [await self.accepted(ingress, index) for index in (1, 2)]
        producers = [
            asyncio.create_task(ingress.accept(MessageSnapshot(index, f"fact {index}")))
            for index in range(3, 8)
        ]
        await asyncio.sleep(0)
        self.assertTrue(all(not task.done() for task in producers))
        ingress.close()
        results = await asyncio.wait_for(asyncio.gather(*producers), timeout=1)
        self.assertTrue(all(result.outcome is AdmissionOutcome.CLOSED for result in results))
        self.assertTrue(all(result.event is None for result in results))
        self.assertEqual(ingress.qsize(), 2)
        self.assertTrue(all(not ingress.is_current(event.prompt_token) for event in accepted))
        joining = asyncio.create_task(ingress.join())
        await asyncio.sleep(0)
        self.assertFalse(joining.done())
        for expected in accepted:
            self.assertIs(await ingress.get(), expected)
            ingress.task_done()
        await asyncio.wait_for(joining, timeout=1)
        with self.assertRaises(IngressClosedError):
            await ingress.get()

    async def test_close_keeps_inflight_fact_unfinished_after_queue_is_exhausted(self) -> None:
        ingress = EventIngress(FactPolicy())
        first = await self.accepted(ingress, 1)
        second = await self.accepted(ingress, 2)
        self.assertIs(await ingress.get(), first)
        ingress.close()
        joining = asyncio.create_task(ingress.join())
        self.assertIs(await ingress.get(), second)
        ingress.task_done()
        with self.assertRaises(IngressClosedError):
            await ingress.get()
        await asyncio.sleep(0)
        self.assertTrue(ingress.empty())
        self.assertFalse(joining.done())
        ingress.task_done()
        await asyncio.wait_for(joining, timeout=1)

    async def test_close_wins_over_space_wakeup_before_producer_resumes(self) -> None:
        ingress = EventIngress(FactPolicy(), capacity=1)
        first = await self.accepted(ingress, 1)
        producer = asyncio.create_task(ingress.accept(MessageSnapshot(2, "unaccepted")))
        await asyncio.sleep(0)
        self.assertIs(await ingress.get(), first)
        # get() wakes the blocked producer, but it cannot append after this close().
        ingress.close()
        result = await asyncio.wait_for(producer, timeout=1)
        self.assertIs(result.outcome, AdmissionOutcome.CLOSED)
        self.assertTrue(ingress.empty())
        ingress.task_done()
        await asyncio.wait_for(ingress.join(), timeout=1)
        with self.assertRaises(IngressClosedError):
            await ingress.get()

    async def test_task_done_before_get_is_rejected_without_losing_pending_work(self) -> None:
        ingress = EventIngress(FactPolicy())
        event = await self.accepted(ingress, 1)
        ingress.close()
        with self.assertRaises(ValueError):
            ingress.task_done()
        joining = asyncio.create_task(ingress.join())
        await asyncio.sleep(0)
        self.assertFalse(joining.done())
        self.assertIs(await ingress.get(), event)
        ingress.task_done()
        await asyncio.wait_for(joining, timeout=1)
        with self.assertRaises(ValueError):
            ingress.task_done()

    async def test_duplicate_ack_cannot_consume_another_queued_fact(self) -> None:
        ingress = EventIngress(FactPolicy())
        first = await self.accepted(ingress, 1)
        second = await self.accepted(ingress, 2)
        self.assertIs(await ingress.get(), first)
        ingress.task_done()
        with self.assertRaises(ValueError):
            ingress.task_done()
        ingress.close()
        joining = asyncio.create_task(ingress.join())
        await asyncio.sleep(0)
        self.assertFalse(joining.done())
        self.assertIs(await ingress.get(), second)
        ingress.task_done()
        await asyncio.wait_for(joining, timeout=1)

    async def test_join_rechecks_new_admission_after_a_previous_drained_wakeup(self) -> None:
        ingress = EventIngress(FactPolicy())
        await self.accepted(ingress, 1)
        await ingress.get()
        joining = asyncio.create_task(ingress.join())
        await asyncio.sleep(0)
        ingress.task_done()
        # Admission is synchronous while capacity is available: the existing
        # join waiter has been woken but has not resumed when this fact is accepted.
        second = await self.accepted(ingress, 2)
        ingress.close()
        await asyncio.sleep(0)
        self.assertFalse(joining.done())
        self.assertIs(await ingress.get(), second)
        ingress.task_done()
        await asyncio.wait_for(joining, timeout=1)

    async def test_join_rechecks_a_requeued_fact_after_a_drained_wakeup(self) -> None:
        ingress = EventIngress(FactPolicy())
        first = await self.accepted(ingress, 1)
        await ingress.get()
        joining = asyncio.create_task(ingress.join())
        await asyncio.sleep(0)
        ingress.task_done()
        self.assertTrue(ingress.requeue_latest())
        ingress.close()
        await asyncio.sleep(0)
        self.assertFalse(joining.done())
        self.assertIs(await ingress.get(), first)
        ingress.task_done()
        await asyncio.wait_for(joining, timeout=1)

    async def test_cancelled_join_does_not_ack_and_a_later_join_can_finish(self) -> None:
        ingress = EventIngress(FactPolicy())
        await self.accepted(ingress, 1)
        await ingress.get()
        ingress.close()
        joining = asyncio.create_task(ingress.join())
        await asyncio.sleep(0)
        joining.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await joining
        retry = asyncio.create_task(ingress.join())
        await asyncio.sleep(0)
        self.assertFalse(retry.done())
        ingress.task_done()
        await asyncio.wait_for(retry, timeout=1)

    async def test_cancelled_waiting_consumer_does_not_lose_future_accepted_fact(self) -> None:
        ingress = EventIngress(FactPolicy())
        consumer = asyncio.create_task(ingress.get())
        await asyncio.sleep(0)
        consumer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await consumer
        first = await self.accepted(ingress, 1)
        ingress.close()
        self.assertIs(await ingress.get(), first)
        ingress.task_done()
        await asyncio.wait_for(ingress.join(), timeout=1)

    async def test_repeated_close_preserves_counters_and_rejects_requeue(self) -> None:
        ingress = EventIngress(FactPolicy())
        first = await self.accepted(ingress, 1)
        generation = ingress.generation
        ingress.close()
        ingress.close()
        result = await ingress.accept(MessageSnapshot(2, "rejected"))
        self.assertIs(result.outcome, AdmissionOutcome.CLOSED)
        self.assertEqual(ingress.generation, generation)
        self.assertFalse(ingress.requeue_latest())
        self.assertEqual(ingress.qsize(), 1)
        self.assertIs(await ingress.get(), first)
        ingress.close()
        ingress.task_done()
        ingress.close()
        await asyncio.wait_for(ingress.join(), timeout=1)
        with self.assertRaises(IngressClosedError):
            await ingress.get()

    async def test_empty_close_wakes_consumer_but_needs_no_artificial_ack(self) -> None:
        ingress = EventIngress(FactPolicy())
        consumer = asyncio.create_task(ingress.get())
        await asyncio.sleep(0)
        ingress.close()
        with self.assertRaises(IngressClosedError):
            await asyncio.wait_for(consumer, timeout=1)
        await asyncio.wait_for(ingress.join(), timeout=1)
        with self.assertRaises(ValueError):
            ingress.task_done()


if __name__ == "__main__":
    unittest.main()
