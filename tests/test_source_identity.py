from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

from battle_records import SourceEventId
from bounded_values import INT64_MAX
from message_snapshot import (
    ButtonSnapshot,
    MessageSnapshot,
    canonical_source_scope,
    derive_source_event_id,
)


class SourceIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = "telegram:bot:fogmmobot"
        self.snapshot = MessageSnapshot(
            42,
            "Бой завершён\nПобеда",
            ((ButtonSnapshot("Продолжить", b"battle:next"),),),
            datetime(2026, 9, 14, 12, 30, 5, 123456, tzinfo=UTC),
        )

    def test_identity_is_stable_for_the_same_immutable_revision(self) -> None:
        first = derive_source_event_id(self.scope, self.snapshot)
        copied = MessageSnapshot(
            self.snapshot.id,
            self.snapshot.raw_text,
            self.snapshot.buttons,
            self.snapshot.edit_date,
        )
        self.assertEqual(first, derive_source_event_id(self.scope, copied))
        self.assertTrue(first.value.startswith("src1:"))
        self.assertLessEqual(len(first.value.encode("utf-8")), 255)

    def test_equivalent_aware_timezones_have_one_canonical_identity(self) -> None:
        assert self.snapshot.edit_date is not None
        moscow = replace(
            self.snapshot,
            edit_date=self.snapshot.edit_date.astimezone(timezone(timedelta(hours=3))),
        )
        self.assertEqual(
            derive_source_event_id(self.scope, self.snapshot),
            derive_source_event_id(self.scope, moscow),
        )

    def test_every_source_revision_component_changes_identity(self) -> None:
        assert self.snapshot.edit_date is not None
        variants = (
            ("telegram:bot:other", self.snapshot),
            (self.scope, replace(self.snapshot, id=43)),
            (
                self.scope,
                replace(
                    self.snapshot,
                    edit_date=self.snapshot.edit_date + timedelta(microseconds=1),
                ),
            ),
            (self.scope, replace(self.snapshot, raw_text=self.snapshot.raw_text + "!")),
            (
                self.scope,
                replace(
                    self.snapshot,
                    buttons=((ButtonSnapshot("Продолжить", b"battle:other"),),),
                ),
            ),
            (
                self.scope,
                replace(
                    self.snapshot,
                    buttons=((ButtonSnapshot("Дальше", b"battle:next"),),),
                ),
            ),
        )
        original = derive_source_event_id(self.scope, self.snapshot)
        for scope, snapshot in variants:
            with self.subTest(scope=scope, snapshot=snapshot):
                self.assertNotEqual(original, derive_source_event_id(scope, snapshot))

    def test_identical_transport_facts_intentionally_collapse(self) -> None:
        first = derive_source_event_id(self.scope, self.snapshot)
        redelivery = derive_source_event_id(self.scope, replace(self.snapshot))
        self.assertEqual(first, redelivery)

    def test_scope_snapshot_and_message_id_validation_is_strict(self) -> None:
        for scope in ("", " leading", "trailing ", "line\nbreak", "x" * 256):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                canonical_source_scope(scope)
        for value in (True, 0, -1, INT64_MAX + 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                MessageSnapshot(value)
        with self.assertRaises(ValueError):
            derive_source_event_id(self.scope, object())  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            SourceEventId("src1:\u200bhidden")


if __name__ == "__main__":
    unittest.main()
