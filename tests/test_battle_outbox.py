from __future__ import annotations

import sqlite3
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from battle_outbox import BattleEvent
from battle_records import (
    BattleOutcome,
    IdempotencyConflict,
    ItemDrop,
    RewardBundle,
    SourceEventId,
)
from legacy_combat_diagnostics import (
    LEGACY_DIAGNOSTICS_NAMESPACE,
    LEGACY_DIAGNOSTICS_SCHEMA_VERSION,
    LegacyCombatDiagnostics,
    LegacyDiagnosticsSchemaError,
)
from storage import SCHEMA_VERSION, Storage
from tests.test_legacy_combat_diagnostics import legacy_trace


def event(*, key: str = "one", namespace: str = "consumer:v1", value: int = 1) -> BattleEvent:
    return BattleEvent.from_payload(
        namespace=namespace,
        idempotency_key=key,
        event_type="battle-recorded",
        schema_version=1,
        payload={"value": value},
    )


class BattleEventValueTests(unittest.TestCase):
    def test_payload_is_canonical_and_detached_from_input_and_decoded_copies(self) -> None:
        source = {"z": [1], "a": {"b": 2}}
        value = BattleEvent.from_payload(
            namespace=" consumer ",
            idempotency_key="key",
            event_type="recorded",
            schema_version=1,
            payload=source,
        )
        source["z"].append(3)
        copy = value.decoded_payload()
        copy["z"].append(4)
        self.assertEqual(value.payload_json, '{"a":{"b":2},"z":[1]}')
        self.assertEqual(value.namespace, "consumer")
        with self.assertRaises(FrozenInstanceError):
            value.payload_json = "{}"

    def test_invalid_identity_version_and_non_json_values_are_rejected(self) -> None:
        for fields in (
            {"namespace": " "},
            {"idempotency_key": ""},
            {"event_type": ""},
            {"schema_version": True},
            {"schema_version": 0},
            {"payload_json": "[]"},
            {"payload_json": '{"n":NaN}'},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                replace(event(), **fields)
        for payload in ({1: "bad key"}, {"n": float("inf")}, {"n": object()}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                BattleEvent.from_payload(
                    namespace="n",
                    idempotency_key="k",
                    event_type="t",
                    schema_version=1,
                    payload=payload,
                )


class BattleOutboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "test.sqlite3"
        self.store = Storage(self.path)
        self.outcome = BattleOutcome(
            source_event_id=SourceEventId("test:outbox:100"),
            source_message_id=100,
            session_id=None,
            target_name="Моль",
            result="VICTORY",
            rewards=RewardBundle(
                xp=12, dust=2, crystals=3, items=(ItemDrop("Карта Моль", is_card=True),)
            ),
            position=(1, 2),
        )
        self.diagnostics = LegacyCombatDiagnostics(self.store)

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self.directory.cleanup()

    async def pending_diagnostics(self):
        return await self.store.pending_battle_events(
            namespace=LEGACY_DIAGNOSTICS_NAMESPACE,
            include_deferred=True,
        )

    async def test_duplicate_replay_adds_missing_events_without_repeating_rewards(self) -> None:
        first = await self.store.record_battle_outcome(self.outcome, events=(event(),))
        replay = replace(
            self.outcome,
            session_id=999,
            position=None,
            happened_at=self.outcome.happened_at + timedelta(days=1),
        )
        duplicate = await self.store.record_battle_outcome(
            replay, events=(event(), event(key="two"))
        )
        self.assertFalse(duplicate.inserted)
        self.assertEqual(duplicate.battle_id, first.battle_id)
        self.assertEqual(duplicate.cards, ())
        self.assertEqual(len(await self.store.pending_battle_events(namespace="consumer:v1")), 2)
        row = self.store.connection.execute("SELECT * FROM battles").fetchone()
        self.assertEqual((row["xp"], row["position_x"], row["position_y"]), (12, 1, 2))

    async def test_changed_mandatory_content_is_an_explicit_conflict(self) -> None:
        await self.store.record_battle_outcome(self.outcome)
        for fields in (
            {"target_name": "Другой"},
            {"result": "DEFEAT"},
            {"rewards": RewardBundle(xp=13)},
            {"rewards": replace(self.outcome.rewards, items=())},
        ):
            with self.subTest(fields=fields), self.assertRaises(IdempotencyConflict):
                await self.store.record_battle_outcome(replace(self.outcome, **fields))
        self.assertFalse(self.store.connection.in_transaction)
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM battles").fetchone()[0], 1
        )

    async def test_event_identity_is_scoped_to_battle_and_namespace(self) -> None:
        await self.store.record_battle_outcome(
            self.outcome, events=(event(), event(namespace="other"))
        )
        await self.store.record_battle_outcome(
            replace(
                self.outcome,
                source_event_id=SourceEventId("test:outbox:101"),
                source_message_id=101,
            ),
            events=(event(),),
        )
        self.assertEqual(len(await self.store.pending_battle_events(namespace="consumer:v1")), 2)
        self.assertEqual(len(await self.store.pending_battle_events(namespace="other")), 1)

    async def test_conflicting_event_rolls_back_entire_new_batch(self) -> None:
        await self.store.record_battle_outcome(self.outcome, events=(event(),))
        for changed in (
            event(value=2),
            replace(event(), event_type="other"),
            replace(event(), schema_version=2),
        ):
            with self.subTest(changed=changed), self.assertRaises(IdempotencyConflict):
                await self.store.record_battle_outcome(
                    self.outcome,
                    events=(event(key="new"), changed),
                )
        self.assertEqual(len(await self.store.pending_battle_events(namespace="consumer:v1")), 1)
        self.assertFalse(self.store.connection.in_transaction)

    async def test_outbox_infrastructure_failure_rolls_back_ledger_and_rewards(self) -> None:
        self.store.connection.executescript("""
            CREATE TRIGGER fail_outbox AFTER INSERT ON battle_outbox
            BEGIN SELECT RAISE(FAIL, 'outbox infrastructure failed'); END;
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            await self.store.record_battle_outcome(self.outcome, events=(event(),))
        await self.store.add_event("TEST", "unrelated successful write")
        for table in ("battles", "drops", "battle_currencies", "battle_outbox"):
            self.assertEqual(
                self.store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0
            )
        self.assertFalse(self.store.connection.in_transaction)

    async def test_pending_in_any_namespace_prevents_retention_until_ack(self) -> None:
        outcome = replace(self.outcome, happened_at=datetime(2020, 1, 1, tzinfo=UTC))
        await self.store.record_battle_outcome(outcome, events=(event(namespace="future"),))
        self.assertEqual((await self.store.cleanup_old_data())["battles"], 0)
        pending = await self.store.pending_battle_events(namespace="future")
        self.assertFalse(await self.store.ack_battle_event(pending[0].id, namespace="wrong"))
        self.assertTrue(await self.store.ack_battle_event(pending[0].id, namespace="future"))
        self.assertFalse(await self.store.ack_battle_event(pending[0].id, namespace="future"))
        self.assertEqual((await self.store.cleanup_old_data())["battles"], 1)

    async def test_retry_metadata_defers_without_losing_payload(self) -> None:
        await self.store.record_battle_outcome(self.outcome, events=(event(),))
        pending = await self.store.pending_battle_events(namespace="consumer:v1")
        await self.store.fail_battle_event(pending[0].id, namespace="consumer:v1", error="retry")
        self.assertEqual(await self.store.pending_battle_events(namespace="consumer:v1"), ())
        deferred = await self.store.pending_battle_events(
            namespace="consumer:v1", include_deferred=True
        )
        self.assertEqual(deferred[0].attempts, 1)
        self.assertEqual(deferred[0].last_error, "retry")
        self.assertEqual(deferred[0].event, pending[0].event)
        self.assertIsNotNone(deferred[0].next_attempt_at)

    async def test_pending_rejects_lossy_numeric_metadata_from_storage(self) -> None:
        await self.store.record_battle_outcome(self.outcome, events=(event(),))
        for column, valid_value in (("attempts", 0), ("schema_version", 1)):
            with self.subTest(column=column):
                self.store.connection.execute(
                    f"UPDATE battle_outbox SET {column}=1.5"
                )
                self.store.connection.commit()
                with self.assertRaisesRegex(ValueError, column):
                    await self.store.pending_battle_events(namespace="consumer:v1")
                self.assertFalse(self.store.connection.in_transaction)
                self.store.connection.execute(
                    f"UPDATE battle_outbox SET {column}=?", (valid_value,)
                )
                self.store.connection.commit()

    async def test_restart_replays_durable_traces_and_empty_duplicate_keeps_original(self) -> None:
        with patch.object(self.diagnostics, "initialize", return_value=None):
            first = await self.diagnostics.record_payloads(
                self.outcome,
                combat_decisions=(legacy_trace(),),
            )
        self.assertEqual(len(await self.pending_diagnostics()), 1)
        await self.store.close()
        self.store = Storage(self.path)
        self.diagnostics = LegacyCombatDiagnostics(self.store)
        replay = replace(self.outcome, session_id=999, happened_at=datetime.now(UTC), position=None)
        duplicate = await self.diagnostics.record_battle(replay, decisions=())
        self.assertFalse(duplicate.inserted)
        self.assertEqual(duplicate.battle_id, first.battle_id)
        self.assertEqual(duplicate.cards, ())
        self.assertEqual(await self.pending_diagnostics(), ())
        self.assertEqual(len(await self.diagnostics.get_decisions()), 1)
        self.assertEqual(len(await self.diagnostics.learning_stats()), 1)

    async def test_replay_after_projection_before_ack_is_idempotent(self) -> None:
        with patch.object(self.store, "ack_battle_event", side_effect=RuntimeError("ack failed")):
            with self.assertLogs("fog_farmer", level="ERROR"):
                result = await self.diagnostics.record_payloads(
                    self.outcome,
                    combat_decisions=(legacy_trace(),),
                )
        self.assertTrue(result.inserted)
        self.assertEqual(len(await self.pending_diagnostics()), 1)
        self.assertEqual(await self.diagnostics.drain_pending(include_deferred=True), 1)
        self.assertEqual(len(await self.diagnostics.get_decisions()), 1)
        self.assertEqual(len(await self.diagnostics.learning_stats()), 1)
        self.assertEqual(await self.pending_diagnostics(), ())

    async def test_malformed_trace_serialization_does_not_block_required_ledger(self) -> None:
        with self.assertLogs("fog_farmer", level="ERROR"):
            recorded = await self.diagnostics.record_payloads(
                self.outcome,
                combat_decisions=(legacy_trace(unserializable=object()),),
            )
        self.assertTrue(recorded.inserted)
        self.assertEqual(recorded.cards, ("Карта Моль",))
        self.assertEqual(await self.pending_diagnostics(), ())

    async def test_poison_event_gets_retry_metadata_without_starving_later_event(self) -> None:
        future = BattleEvent.from_payload(
            namespace=LEGACY_DIAGNOSTICS_NAMESPACE,
            idempotency_key="future",
            event_type="future",
            schema_version=2,
            payload={},
        )
        await self.store.record_battle_outcome(self.outcome, events=(future,))
        with patch.object(self.diagnostics, "initialize", return_value=None):
            await self.diagnostics.record_payloads(self.outcome, combat_decisions=(legacy_trace(),))
        with self.assertLogs("fog_farmer", level="ERROR"):
            self.assertEqual(await self.diagnostics.drain_pending(batch_size=1), 1)
        pending = await self.pending_diagnostics()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].event, future)
        self.assertEqual(pending[0].attempts, 1)
        self.assertIn("Unsupported", pending[0].last_error)

    async def test_different_trace_for_same_sequence_is_retained_as_conflict(self) -> None:
        await self.diagnostics.record_payloads(self.outcome, combat_decisions=(legacy_trace(),))
        changed = legacy_trace(decision={"skill_name": "лечение", "target": "self"})
        with self.assertLogs("fog_farmer", level="ERROR"):
            duplicate = await self.diagnostics.record_payloads(
                self.outcome, combat_decisions=(changed,)
            )
        self.assertFalse(duplicate.inserted)
        pending = await self.pending_diagnostics()
        self.assertEqual(len(pending), 1)
        self.assertIn("Conflicting diagnostic", pending[0].last_error)
        self.assertEqual(len(await self.diagnostics.learning_stats()), 1)

    async def test_future_or_unknown_schema_is_not_overwritten(self) -> None:
        self.store.connection.execute("CREATE TABLE combat_decisions (alien TEXT)")
        with self.assertRaises(LegacyDiagnosticsSchemaError):
            await self.diagnostics.initialize()
        self.store.connection.execute("DROP TABLE combat_decisions")
        await self.diagnostics.initialize()
        future_version = LEGACY_DIAGNOSTICS_SCHEMA_VERSION + 1
        self.store.connection.execute(
            "UPDATE legacy_combat_diagnostics_meta SET schema_version=?",
            (future_version,),
        )
        self.store.connection.commit()
        with self.assertRaises(LegacyDiagnosticsSchemaError):
            await LegacyCombatDiagnostics(self.store).initialize()
        self.assertEqual(
            self.store.connection.execute(
                "SELECT schema_version FROM legacy_combat_diagnostics_meta"
            ).fetchone()[0],
            future_version,
        )

    async def test_external_event_is_atomic_and_remains_pending_for_its_own_consumer(self) -> None:
        notification = event(namespace="application:cards:v1")
        result = await self.diagnostics.record_battle(self.outcome, events=(notification,))
        self.assertTrue(result.inserted)
        pending = await self.store.pending_battle_events(namespace="application:cards:v1")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].battle_id, result.battle_id)
        await self.diagnostics.initialize()
        self.assertEqual(
            len(await self.store.pending_battle_events(namespace="application:cards:v1")), 1
        )

    async def test_outcome_getter_round_trips_all_facts_and_original_context(self) -> None:
        session_id = await self.store.start_session(cycles_count=1, moves_per_cycle=80)
        original = replace(
            self.outcome,
            session_id=session_id,
            happened_at=datetime(2026, 9, 14, 12, 30, tzinfo=UTC),
        )
        recorded = await self.store.record_battle_outcome(original)
        self.assertEqual(await self.store.get_battle_outcome(recorded.battle_id), original)
        replay = replace(original, session_id=None, position=None, happened_at=datetime.now(UTC))
        await self.store.record_battle_outcome(replay)
        self.assertEqual(await self.store.get_battle_outcome(recorded.battle_id), original)
        self.assertIsNone(await self.store.get_battle_outcome(recorded.battle_id + 1))
        for invalid_id in (True, 0, 2**63):
            with self.assertRaises(ValueError):
                await self.store.get_battle_outcome(invalid_id)
        self.store.connection.execute("UPDATE drops SET is_card=2")
        self.store.connection.commit()
        with self.assertRaises(ValueError):
            await self.store.get_battle_outcome(recorded.battle_id)
        self.assertFalse(self.store.connection.in_transaction)

    async def test_existing_v2_database_gets_outbox_without_losing_required_state(self) -> None:
        await self.store.record_battle_outcome(self.outcome)
        self.store.connection.execute("DROP TABLE battle_outbox")
        self.store.connection.execute("PRAGMA user_version=2")
        await self.store.close()
        self.store = Storage(self.path)
        self.assertEqual(
            self.store.connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION
        )
        self.assertEqual(self.store.connection.execute("SELECT xp FROM battles").fetchone()[0], 12)
        self.assertEqual(await self.store.pending_battle_events(namespace="consumer:v1"), ())


if __name__ == "__main__":
    unittest.main()
