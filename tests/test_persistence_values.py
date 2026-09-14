from __future__ import annotations

import json
import sqlite3
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from battle_outbox import BattleOutboxEnvelope
from battle_records import (
    BattleOutcome,
    ItemDrop,
    RecordBattleResult,
    RewardBundle,
    SourceEventId,
)
from bounded_values import INT64_MAX, INT64_MIN
from storage import Storage
from tests.test_battle_outbox import event


class PersistedValueTests(unittest.TestCase):
    def test_domain_persisted_numbers_reject_bool_and_out_of_int64_range(self) -> None:
        outcome = BattleOutcome(
            source_event_id=SourceEventId("test:persistence:1"),
            source_message_id=1,
            session_id=None,
            target_name="Моль",
            result="VICTORY",
        )
        for value in (True, 2**63, 2**100, -1, 0):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    replace(outcome, source_message_id=value)
                with self.assertRaises(ValueError):
                    replace(outcome, session_id=value)
                with self.assertRaises(ValueError):
                    ItemDrop("item", quantity=value)
        for value in (True, -1, INT64_MAX + 1):
            for field in ("xp", "dust", "crystals"):
                with self.subTest(value=value, field=field), self.assertRaises(ValueError):
                    RewardBundle(**{field: value})
        self.assertEqual(RewardBundle(xp=INT64_MAX).xp, INT64_MAX)
        self.assertEqual(
            replace(outcome, position=(INT64_MIN, INT64_MAX)).position, (INT64_MIN, INT64_MAX)
        )
        for coordinates in ((INT64_MIN - 1, 0), (0, INT64_MAX + 1)):
            with self.assertRaises(ValueError):
                replace(outcome, position=coordinates)
        for version in (True, 0, INT64_MAX + 1):
            with self.assertRaises(ValueError):
                replace(event(), schema_version=version)

    def test_record_result_validates_output_contract_and_detaches_names(self) -> None:
        result = RecordBattleResult(True, 1)
        for fields in (
            {"inserted": 1},
            {"battle_id": True},
            {"battle_id": 0},
            {"battle_id": INT64_MAX + 1},
            {"cards": "card"},
            {"cards": (" ",)},
            {"cards": (None,)},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                replace(result, **fields)
        names = ["  Карта Моль "]
        actual = RecordBattleResult(True, INT64_MAX, names)
        names.append("other")
        self.assertEqual(actual.cards, ("Карта Моль",))

    def test_outbox_envelope_has_valid_ids_event_and_canonical_aware_times(self) -> None:
        envelope = BattleOutboxEnvelope(1, 2, "2026-09-14T12:00:00+03:00", event())
        self.assertEqual(envelope.created_at, "2026-09-14T09:00:00+00:00")
        for fields in (
            {"id": True},
            {"id": 0},
            {"battle_id": INT64_MAX + 1},
            {"created_at": "2026-09-14T12:00:00"},
            {"created_at": None},
            {"event": {}},
            {"attempts": True},
            {"attempts": -1},
            {"next_attempt_at": "2026-09-14"},
            {"last_error": []},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                replace(envelope, **fields)


class PersistedNumericBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = Storage(Path(":memory:"))

    async def asyncTearDown(self) -> None:
        await self.store.close()

    async def test_knowledge_uses_trimmed_namespace_and_canonical_typed_json(self) -> None:
        await self.store.save_combat_knowledge(500, {"z": 2, "a": [1]}, namespace="  rules  ")
        self.assertEqual(
            await self.store.load_combat_knowledge(namespace=" rules "), {500: {"a": [1], "z": 2}}
        )
        row = self.store.connection.execute(
            "SELECT namespace,knowledge_json FROM combat_knowledge"
        ).fetchone()
        self.assertEqual(tuple(row), ("rules", '{"a":[1],"z":2}'))
        for hp in (True, 0, -1, 1.5, INT64_MAX + 1):
            with self.subTest(hp=hp), self.assertRaises(ValueError):
                await self.store.save_combat_knowledge(hp, {}, namespace="rules")
        for payload in ({"n": float("nan")}, {"n": object()}, {1: 2}, [], None):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                await self.store.save_combat_knowledge(500, payload, namespace="rules")
        self.assertFalse(self.store.connection.in_transaction)

    async def test_malformed_persisted_knowledge_is_quarantined_per_row(self) -> None:
        await self.store.save_combat_knowledge(500, {}, namespace="rules")
        corrupt_rows = (
            ("rules", 600, "2026-09-14T00:00:00+00:00", "[]"),
            ("rules", 700, "2026-09-14T00:00:00+00:00", '{"n":NaN}'),
            ("rules", 800, "2026-09-14T00:00:00+00:00", "{broken"),
            ("rules", "bad", "2026-09-14T00:00:00+00:00", "{}"),
        )
        self.store.connection.executemany(
            "INSERT INTO combat_knowledge("
            "namespace,profile_max_hp,updated_at,knowledge_json) VALUES (?,?,?,?)",
            corrupt_rows,
        )
        self.store.connection.commit()

        self.assertEqual(
            await self.store.load_combat_knowledge(namespace="rules"), {500: {}}
        )
        remaining = self.store.connection.execute(
            "SELECT profile_max_hp,knowledge_json FROM combat_knowledge "
            "WHERE namespace=? ORDER BY profile_max_hp",
            ("rules",),
        ).fetchall()
        self.assertEqual([tuple(row) for row in remaining], [(500, "{}")])
        events = self.store.connection.execute(
            "SELECT level,message,payload_json FROM events "
            "WHERE event_type='COMBAT_KNOWLEDGE_QUARANTINED' ORDER BY id"
        ).fetchall()
        self.assertEqual(len(events), len(corrupt_rows))
        payloads = [json.loads(row["payload_json"]) for row in events]
        self.assertTrue(all(row["level"] == "WARNING" for row in events))
        self.assertTrue(all("удалён" in row["message"] for row in events))
        self.assertTrue(all(payload["namespace"] == "rules" for payload in payloads))
        self.assertEqual(
            {payload["profile_key_type"] for payload in payloads}, {"int", "str"}
        )
        self.assertTrue(all(payload["error_type"] for payload in payloads))
        self.assertTrue(all(payload["reason"] for payload in payloads))

        self.assertEqual(
            await self.store.load_combat_knowledge(namespace="rules"), {500: {}}
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM events "
                "WHERE event_type='COMBAT_KNOWLEDGE_QUARANTINED'"
            ).fetchone()[0],
            len(corrupt_rows),
        )

    async def test_knowledge_quarantine_is_atomic_when_delete_fails(self) -> None:
        self.store.connection.execute(
            "INSERT INTO combat_knowledge("
            "namespace,profile_max_hp,updated_at,knowledge_json) VALUES (?,?,?,?)",
            ("rules", 600, "2026-09-14T00:00:00+00:00", "[]"),
        )
        self.store.connection.execute(
            """
            CREATE TRIGGER reject_knowledge_quarantine
            BEFORE DELETE ON combat_knowledge
            BEGIN
                SELECT RAISE(FAIL, 'quarantine rejected');
            END
            """
        )
        self.store.connection.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            await self.store.load_combat_knowledge(namespace="rules")
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM combat_knowledge WHERE namespace='rules'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM events "
                "WHERE event_type='COMBAT_KNOWLEDGE_QUARANTINED'"
            ).fetchone()[0],
            0,
        )
        self.assertFalse(self.store.connection.in_transaction)

        self.store.connection.execute("DROP TRIGGER reject_knowledge_quarantine")
        self.store.connection.commit()
        self.assertEqual(await self.store.load_combat_knowledge(namespace="rules"), {})
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM events "
                "WHERE event_type='COMBAT_KNOWLEDGE_QUARANTINED'"
            ).fetchone()[0],
            1,
        )

    async def test_int64_overflow_is_rejected_before_binding_or_counter_corruption(self) -> None:
        for value in (True, INT64_MAX + 1):
            with self.assertRaises(ValueError):
                await self.store.start_session(cycles_count=value, moves_per_cycle=80)
            with self.assertRaises(ValueError):
                await self.store.update_state(position_x=value)
            with self.assertRaises(ValueError):
                await self.store.remember_map_obstacle("map", (value, 0))
            with self.assertRaises(ValueError):
                await self.store.ack_battle_event(value, namespace="consumer")
        bucket = datetime.now(UTC).isoformat()
        await self.store.increment_telegram_activity(bucket, {"outgoing_total": INT64_MAX})
        with self.assertRaises(ValueError):
            await self.store.increment_telegram_activity(bucket, {"outgoing_total": 1})
        self.assertEqual(
            self.store.connection.execute(
                "SELECT outgoing_total FROM telegram_activity_hourly"
            ).fetchone()[0],
            INT64_MAX,
        )
        session = await self.store.start_session(cycles_count=1, moves_per_cycle=80)
        self.store.connection.execute("UPDATE sessions SET xp=?", (INT64_MAX,))
        self.store.connection.commit()
        outcome = BattleOutcome(
            source_event_id=SourceEventId("test:persistence:overflow"),
            source_message_id=1,
            session_id=session,
            target_name="Моль",
            result="VICTORY",
            rewards=RewardBundle(xp=1),
        )
        with self.assertRaises(ValueError):
            await self.store.record_battle_outcome(outcome)
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM battles").fetchone()[0], 0
        )
        self.assertFalse(self.store.connection.in_transaction)


if __name__ == "__main__":
    unittest.main()
