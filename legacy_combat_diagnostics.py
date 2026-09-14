from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NotRequired, Protocol, TypedDict, cast

from battle_outbox import BattleEvent, BattleOutboxEnvelope, InvalidBattleOutboxEntry
from battle_records import (
    BattleOutcome,
    BattleResult,
    IdempotencyConflict,
    RecordBattleResult,
    SourceEventId,
)
from combat_knowledge_namespace import LEGACY_COMBAT_KNOWLEDGE_NAMESPACE
from combat_learning import BattleLearningSummary, battle_learning_summary, resolved_decision
from combat_strategy import COMBAT_MODEL_VERSION, CombatDecisionTrace
from json_types import JsonValue


class CombatDecisionRow(TypedDict):
    id: int
    battle_id: int
    sequence_number: int
    created_at: str
    telegram_message_id: int
    target_name: str
    round_number: int | None
    chosen_skill: str
    chosen_target: str
    reason: str
    urgent: int
    result: BattleResult
    # Trace payload schemas vary by combat-model version in persisted data.
    trace: NotRequired[JsonValue]


logger = logging.getLogger("fog_farmer")
LEGACY_TRACE_SCHEMA_VERSION = 1
LEGACY_DIAGNOSTICS_NAMESPACE = LEGACY_COMBAT_KNOWLEDGE_NAMESPACE + ":diagnostics-1"
LEGACY_DIAGNOSTICS_EVENT_TYPE = "battle-diagnostics"

# These historical tables belong to this adapter. A future ruleset needs its own
# diagnostic repository/schema; the mandatory ledger does not inspect these rows.
_DIAGNOSTIC_SCHEMA_V2 = """
        CREATE TABLE IF NOT EXISTS combat_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            battle_id INTEGER NOT NULL,
            sequence_number INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            telegram_message_id INTEGER NOT NULL,
            target_name TEXT NOT NULL,
            round_number INTEGER,
            chosen_skill TEXT NOT NULL,
            chosen_target TEXT NOT NULL,
            reason TEXT NOT NULL,
            urgent INTEGER NOT NULL DEFAULT 0,
            trace_json TEXT NOT NULL,
            UNIQUE(battle_id, sequence_number),
            FOREIGN KEY(battle_id) REFERENCES battles(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_combat_decisions_target
            ON combat_decisions(target_name, chosen_skill);

        CREATE TABLE IF NOT EXISTS combat_battle_analysis (
            battle_id INTEGER PRIMARY KEY,
            target_name TEXT NOT NULL,
            result TEXT NOT NULL,
            happened_at TEXT NOT NULL,
            profile_max_hp INTEGER NOT NULL DEFAULT 0,
            model_version INTEGER NOT NULL DEFAULT 0,
            rounds INTEGER NOT NULL DEFAULT 0,
            total_actions INTEGER NOT NULL DEFAULT 0,
            offensive_actions INTEGER NOT NULL DEFAULT 0,
            self_heals INTEGER NOT NULL DEFAULT 0,
            renewals INTEGER NOT NULL DEFAULT 0,
            minimum_hp INTEGER,
            minimum_hp_percent REAL,
            last_decision_hp INTEGER,
            minimum_mana INTEGER,
            last_decision_mana INTEGER,
            effective_self_healing INTEGER NOT NULL DEFAULT 0,
            lost_healing_potential INTEGER NOT NULL DEFAULT 0,
            dangerous_turns INTEGER NOT NULL DEFAULT 0,
            shadow_decisions INTEGER NOT NULL DEFAULT 0,
            shadow_confident INTEGER NOT NULL DEFAULT 0,
            shadow_agreements INTEGER NOT NULL DEFAULT 0,
            policy_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(battle_id) REFERENCES battles(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_combat_analysis_profile_target
            ON combat_battle_analysis(profile_max_hp, target_name);
        CREATE INDEX IF NOT EXISTS idx_combat_analysis_happened_at
            ON combat_battle_analysis(happened_at);

"""


_DIAGNOSTIC_SCHEMA_V1 = _DIAGNOSTIC_SCHEMA_V2.replace(
    "            created_at TEXT NOT NULL,\n"
    "            FOREIGN KEY(battle_id) REFERENCES battles(id) ON DELETE CASCADE\n"
    "        );\n"
    "        CREATE INDEX IF NOT EXISTS idx_combat_analysis_profile_target",
    "            created_at TEXT NOT NULL\n"
    "        );\n"
    "        CREATE INDEX IF NOT EXISTS idx_combat_analysis_profile_target",
    1,
)
_DIAGNOSTIC_SCHEMA = _DIAGNOSTIC_SCHEMA_V2


class LegacyDiagnosticsSchemaError(RuntimeError):
    """Existing table ownership/version could not be proven; leave it untouched."""


def _table_fingerprint(connection: sqlite3.Connection, table: str) -> str:
    columns = [tuple(row) for row in connection.execute(f"PRAGMA table_info({table})")]
    foreign_keys = [tuple(row) for row in connection.execute(f"PRAGMA foreign_key_list({table})")]
    unique_keys = []
    for row in connection.execute(f"PRAGMA index_list({table})"):
        if row[2]:
            unique_keys.append(
                tuple(column[2] for column in connection.execute(f"PRAGMA index_info({row[1]})"))
            )
    payload = (columns, foreign_keys, sorted(unique_keys))
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _expected_schema_fingerprints(schema: str) -> dict[str, str]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(schema)
        return {
            table: _table_fingerprint(connection, table)
            for table in ("combat_decisions", "combat_battle_analysis")
        }
    finally:
        connection.close()


def _schema_fingerprint(tables: Mapping[str, str]) -> str:
    return hashlib.sha256(json.dumps(tables, sort_keys=True).encode()).hexdigest()


LEGACY_DIAGNOSTICS_SCHEMA_VERSION = 2
_EXPECTED_TABLES_V1 = _expected_schema_fingerprints(_DIAGNOSTIC_SCHEMA_V1)
_EXPECTED_TABLES = _expected_schema_fingerprints(_DIAGNOSTIC_SCHEMA_V2)
_SCHEMA_FINGERPRINT_V1 = _schema_fingerprint(_EXPECTED_TABLES_V1)
_SCHEMA_FINGERPRINT = _schema_fingerprint(_EXPECTED_TABLES)

_ANALYSIS_COLUMNS = (
    "battle_id,target_name,result,happened_at,profile_max_hp,model_version,"
    "rounds,total_actions,offensive_actions,self_heals,renewals,minimum_hp,"
    "minimum_hp_percent,last_decision_hp,minimum_mana,last_decision_mana,"
    "effective_self_healing,lost_healing_potential,dangerous_turns,"
    "shadow_decisions,shadow_confident,shadow_agreements,policy_key,created_at"
)
_ANALYSIS_SELECT = ",".join(
    f"analysis.{column}" for column in _ANALYSIS_COLUMNS.split(",")
)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _execute_schema(connection: sqlite3.Connection, schema: str) -> None:
    for statement in schema.split(";"):
        if statement.strip():
            connection.execute(statement)


def _require_table_fingerprints(
    connection: sqlite3.Connection, expected: Mapping[str, str]
) -> None:
    for table, fingerprint in expected.items():
        if not _table_exists(connection, table):
            raise LegacyDiagnosticsSchemaError(f"Missing owned legacy table: {table}")
        if _table_fingerprint(connection, table) != fingerprint:
            raise LegacyDiagnosticsSchemaError(f"Unrecognized legacy table schema: {table}")


def _validate_analysis_references(connection: sqlite3.Connection) -> None:
    violation = connection.execute(
        "PRAGMA foreign_key_check(combat_battle_analysis)"
    ).fetchone()
    if violation is not None:
        raise LegacyDiagnosticsSchemaError(
            "Legacy analysis migration left a foreign-key violation: "
            f"{tuple(violation)!r}"
        )
    orphan = connection.execute(
        """
        SELECT analysis.battle_id
        FROM combat_battle_analysis AS analysis
        LEFT JOIN battles ON battles.id=analysis.battle_id
        WHERE battles.id IS NULL
        LIMIT 1
        """
    ).fetchone()
    if orphan is not None:
        raise LegacyDiagnosticsSchemaError(
            f"Legacy analysis migration left an orphan: {orphan[0]!r}"
        )


def _migrate_diagnostic_analysis_v1_to_v2(
    connection: sqlite3.Connection,
) -> None:
    if _table_exists(connection, "combat_battle_analysis_v2"):
        raise LegacyDiagnosticsSchemaError(
            "Unexpected combat_battle_analysis_v2 table before migration"
        )
    connection.execute(
        """
        CREATE TABLE combat_battle_analysis_v2 (
            battle_id INTEGER PRIMARY KEY,
            target_name TEXT NOT NULL,
            result TEXT NOT NULL,
            happened_at TEXT NOT NULL,
            profile_max_hp INTEGER NOT NULL DEFAULT 0,
            model_version INTEGER NOT NULL DEFAULT 0,
            rounds INTEGER NOT NULL DEFAULT 0,
            total_actions INTEGER NOT NULL DEFAULT 0,
            offensive_actions INTEGER NOT NULL DEFAULT 0,
            self_heals INTEGER NOT NULL DEFAULT 0,
            renewals INTEGER NOT NULL DEFAULT 0,
            minimum_hp INTEGER,
            minimum_hp_percent REAL,
            last_decision_hp INTEGER,
            minimum_mana INTEGER,
            last_decision_mana INTEGER,
            effective_self_healing INTEGER NOT NULL DEFAULT 0,
            lost_healing_potential INTEGER NOT NULL DEFAULT 0,
            dangerous_turns INTEGER NOT NULL DEFAULT 0,
            shadow_decisions INTEGER NOT NULL DEFAULT 0,
            shadow_confident INTEGER NOT NULL DEFAULT 0,
            shadow_agreements INTEGER NOT NULL DEFAULT 0,
            policy_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(battle_id) REFERENCES battles(id) ON DELETE CASCADE
        )
        """
    )
    expected_rows = connection.execute(
        "SELECT COUNT(*) FROM combat_battle_analysis AS analysis "
        "JOIN battles ON battles.id=analysis.battle_id"
    ).fetchone()[0]
    connection.execute(
        f"INSERT INTO combat_battle_analysis_v2({_ANALYSIS_COLUMNS}) "
        f"SELECT {_ANALYSIS_SELECT} FROM combat_battle_analysis AS analysis "
        "JOIN battles ON battles.id=analysis.battle_id"
    )
    copied_rows = connection.execute(
        "SELECT COUNT(*) FROM combat_battle_analysis_v2"
    ).fetchone()[0]
    if copied_rows != expected_rows:
        raise RuntimeError("Legacy analysis migration did not preserve valid rows")
    connection.execute("DROP TABLE combat_battle_analysis")
    connection.execute(
        "ALTER TABLE combat_battle_analysis_v2 RENAME TO combat_battle_analysis"
    )
    connection.execute(
        "CREATE INDEX idx_combat_analysis_profile_target "
        "ON combat_battle_analysis(profile_max_hp, target_name)"
    )
    connection.execute(
        "CREATE INDEX idx_combat_analysis_happened_at "
        "ON combat_battle_analysis(happened_at)"
    )
    _require_table_fingerprints(connection, _EXPECTED_TABLES)
    _validate_analysis_references(connection)


class BattleDiagnosticsStore(Protocol):
    async def get_battle_outcome(self, battle_id: int) -> BattleOutcome | None: ...

    async def record_battle_outcome(
        self,
        outcome: BattleOutcome,
        *,
        events: tuple[BattleEvent, ...] = (),
        legacy_source_event_ids: tuple[SourceEventId, ...] = (),
    ) -> RecordBattleResult: ...

    async def pending_battle_event_entries(
        self,
        *,
        namespace: str,
        limit: int = 100,
        after_id: int | None = None,
        include_deferred: bool = False,
    ) -> tuple[BattleOutboxEnvelope | InvalidBattleOutboxEntry, ...]: ...

    async def defer_invalid_battle_event(
        self, entry: InvalidBattleOutboxEntry, *, retry_after_seconds: float = 60,
    ) -> bool: ...

    async def ack_battle_event(self, event_id: int, *, namespace: str) -> bool: ...

    async def fail_battle_event(
        self,
        event_id: int,
        *,
        namespace: str,
        error: str,
        retry_after_seconds: float = 60,
    ) -> bool: ...

    def diagnostics_reader(self) -> AbstractAsyncContextManager[sqlite3.Connection]: ...

    def diagnostics_transaction(self) -> AbstractAsyncContextManager[sqlite3.Connection]: ...


@dataclass(frozen=True, slots=True)
class LegacyDecisionRow:
    sequence_number: int
    created_at: str
    telegram_message_id: int
    target_name: str
    round_number: int | None
    chosen_skill: str
    chosen_target: str
    reason: str
    urgent: int
    trace_json: str

    def parameters(self, battle_id: int) -> tuple[object, ...]:
        return (
            battle_id,
            self.sequence_number,
            self.created_at,
            self.telegram_message_id,
            self.target_name,
            self.round_number,
            self.chosen_skill,
            self.chosen_target,
            self.reason,
            self.urgent,
            self.trace_json,
        )


class LegacyCombatDiagnostics:
    """Compose the mandatory ledger with isolated, versioned legacy projections.

    Store ports provide only synchronized SQLite access and the ledger operation.
    This adapter never imports Storage and cannot participate in an import cycle.
    Versionless rows in its historical tables are the supported legacy format;
    foreign namespaces and newer trace/model versions are never interpreted.
    """

    def __init__(self, store: BattleDiagnosticsStore) -> None:
        self.store = store
        self._schema_ready = False
        self._drain_lock = asyncio.Lock()

    async def initialize(self) -> None:
        await self._ensure_schema()
        await self.drain_pending()

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self.store.diagnostics_transaction() as connection:
            has_metadata = _table_exists(
                connection, "legacy_combat_diagnostics_meta"
            )
            metadata: tuple[object, object] | None = None
            if has_metadata:
                row = connection.execute(
                    "SELECT schema_version,schema_fingerprint "
                    "FROM legacy_combat_diagnostics_meta WHERE singleton=1"
                ).fetchone()
                if row is None:
                    raise LegacyDiagnosticsSchemaError(
                        "Unsupported legacy diagnostic schema metadata"
                    )
                metadata = (row[0], row[1])
                if metadata == (1, _SCHEMA_FINGERPRINT_V1):
                    _require_table_fingerprints(connection, _EXPECTED_TABLES_V1)
                    _migrate_diagnostic_analysis_v1_to_v2(connection)
                elif metadata == (
                    LEGACY_DIAGNOSTICS_SCHEMA_VERSION,
                    _SCHEMA_FINGERPRINT,
                ):
                    _require_table_fingerprints(connection, _EXPECTED_TABLES)
                else:
                    raise LegacyDiagnosticsSchemaError(
                        "Unsupported legacy diagnostic schema metadata"
                    )
            else:
                if _table_exists(connection, "combat_decisions"):
                    decisions_fingerprint = _table_fingerprint(
                        connection, "combat_decisions"
                    )
                    allowed_decisions = {
                        _EXPECTED_TABLES_V1["combat_decisions"],
                        _EXPECTED_TABLES["combat_decisions"],
                    }
                    if decisions_fingerprint not in allowed_decisions:
                        raise LegacyDiagnosticsSchemaError(
                            "Unrecognized legacy table schema: combat_decisions"
                        )
                if _table_exists(connection, "combat_battle_analysis"):
                    analysis_fingerprint = _table_fingerprint(
                        connection, "combat_battle_analysis"
                    )
                    if analysis_fingerprint == _EXPECTED_TABLES_V1[
                        "combat_battle_analysis"
                    ]:
                        _execute_schema(connection, _DIAGNOSTIC_SCHEMA_V1)
                        _require_table_fingerprints(
                            connection, _EXPECTED_TABLES_V1
                        )
                        _migrate_diagnostic_analysis_v1_to_v2(connection)
                    elif analysis_fingerprint == _EXPECTED_TABLES[
                        "combat_battle_analysis"
                    ]:
                        _execute_schema(connection, _DIAGNOSTIC_SCHEMA_V2)
                    else:
                        raise LegacyDiagnosticsSchemaError(
                            "Unrecognized legacy table schema: "
                            "combat_battle_analysis"
                        )
                else:
                    _execute_schema(connection, _DIAGNOSTIC_SCHEMA_V2)

            _require_table_fingerprints(connection, _EXPECTED_TABLES)
            _validate_analysis_references(connection)
            if metadata is None:
                connection.execute(
                    """
                    CREATE TABLE legacy_combat_diagnostics_meta (
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        schema_version INTEGER NOT NULL,
                        schema_fingerprint TEXT NOT NULL
                    )
                    """
                )
                published = connection.execute(
                    "INSERT INTO legacy_combat_diagnostics_meta VALUES (1,?,?)",
                    (LEGACY_DIAGNOSTICS_SCHEMA_VERSION, _SCHEMA_FINGERPRINT),
                )
            else:
                published = connection.execute(
                    "UPDATE legacy_combat_diagnostics_meta "
                    "SET schema_version=?,schema_fingerprint=? "
                    "WHERE singleton=1 AND schema_version=? AND schema_fingerprint=?",
                    (
                        LEGACY_DIAGNOSTICS_SCHEMA_VERSION,
                        _SCHEMA_FINGERPRINT,
                        metadata[0],
                        metadata[1],
                    ),
                )
            if published.rowcount != 1:
                raise RuntimeError(
                    "SQLite did not publish legacy diagnostics schema metadata"
                )
        self._schema_ready = True

    @staticmethod
    def _supports_payload(payload: Mapping[str, object]) -> bool:
        namespace = payload.get("ruleset_namespace", LEGACY_COMBAT_KNOWLEDGE_NAMESPACE)
        schema = payload.get("trace_schema_version", LEGACY_TRACE_SCHEMA_VERSION)
        model = payload.get("model_version", 0)
        return (
            namespace == LEGACY_COMBAT_KNOWLEDGE_NAMESPACE
            and type(schema) is int
            and schema == LEGACY_TRACE_SCHEMA_VERSION
            and type(model) is int
            and 0 <= model <= COMBAT_MODEL_VERSION
        )

    @classmethod
    def _decode_trace(cls, raw: str) -> dict[str, object] | None:
        try:
            payload: object = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        trace = cast(dict[str, object], payload)
        return trace if cls._supports_payload(trace) else None

    @staticmethod
    def _prepare_analysis(traces: list[dict[str, object]]) -> BattleLearningSummary | None:
        if not traces:
            return None
        try:
            return battle_learning_summary(traces)
        except Exception:
            logger.exception("Не удалось рассчитать необязательную legacy-аналитику боя")
            return None

    def _event_for_payloads(
        self, outcome: BattleOutcome, payloads: tuple[Mapping[str, object], ...]
    ) -> BattleEvent | None:
        if not payloads:
            return None
        if any(not self._supports_payload(trace) for trace in payloads):
            logger.warning("Пропущена несовместимая схема legacy-диагностики боя")
            return None
        payload_json = json.dumps(
            {"traces": [dict(trace) for trace in payloads]},
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        return BattleEvent(
            namespace=LEGACY_DIAGNOSTICS_NAMESPACE,
            idempotency_key="traces-v1:" + payload_hash,
            event_type=LEGACY_DIAGNOSTICS_EVENT_TYPE,
            schema_version=LEGACY_TRACE_SCHEMA_VERSION,
            payload_json=payload_json,
        )

    async def record_battle(
        self,
        outcome: BattleOutcome,
        *,
        decisions: tuple[CombatDecisionTrace, ...] = (),
        events: tuple[BattleEvent, ...] = (),
        legacy_source_event_ids: tuple[SourceEventId, ...] = (),
    ) -> RecordBattleResult:
        try:
            payloads = tuple(trace.as_payload() for trace in decisions)
        except Exception:
            logger.exception("Не удалось сериализовать необязательные решения боя")
            payloads = ()
        return await self.record_payloads(
            outcome,
            combat_decisions=payloads,
            events=events,
            legacy_source_event_ids=legacy_source_event_ids,
        )

    async def record_payloads(
        self,
        outcome: BattleOutcome,
        *,
        combat_decisions: tuple[Mapping[str, object], ...] = (),
        events: tuple[BattleEvent, ...] = (),
        legacy_source_event_ids: tuple[SourceEventId, ...] = (),
    ) -> RecordBattleResult:
        """Persist diagnostics and opaque application intents with the required ledger."""
        events = tuple(events)
        if any(not isinstance(value, BattleEvent) for value in events):
            raise ValueError("Expected immutable BattleEvent values")
        try:
            event = self._event_for_payloads(outcome, combat_decisions)
        except Exception:
            logger.exception("Не удалось подготовить необязательную диагностику боя")
            event = None
        recorded = await self.store.record_battle_outcome(
            outcome,
            events=events + ((event,) if event is not None else ()),
            legacy_source_event_ids=legacy_source_event_ids,
        )
        try:
            await self.initialize()
        except Exception:
            logger.exception("Не удалось обработать очередь legacy-диагностики")
        return recorded

    async def _decode_event(
        self,
        envelope: BattleOutboxEnvelope,
    ) -> tuple[BattleOutcome, tuple[Mapping[str, object], ...]]:
        if (
            envelope.event.event_type != LEGACY_DIAGNOSTICS_EVENT_TYPE
            or envelope.event.schema_version != LEGACY_TRACE_SCHEMA_VERSION
        ):
            raise ValueError("Unsupported diagnostic event type/schema")
        traces = envelope.event.decoded_payload().get("traces")
        if (
            not isinstance(traces, list)
            or not traces
            or any(not isinstance(trace, dict) for trace in traces)
        ):
            raise ValueError("Diagnostic event must contain decision trace objects")
        outcome = await self.store.get_battle_outcome(envelope.battle_id)
        if outcome is None:
            raise ValueError("Diagnostic event has no durable battle")
        return outcome, tuple(cast(Mapping[str, object], trace) for trace in traces)

    async def drain_pending(
        self,
        *,
        batch_size: int = 100,
        include_deferred: bool = False,
    ) -> int:
        """Consume at least once; durable projection and trace keys make retries safe."""
        await self._ensure_schema()
        async with self._drain_lock:
            acknowledged = 0
            after_id: int | None = None
            while True:
                pending = await self.store.pending_battle_event_entries(
                    namespace=LEGACY_DIAGNOSTICS_NAMESPACE,
                    limit=batch_size,
                    after_id=after_id,
                    include_deferred=include_deferred,
                )
                if not pending:
                    return acknowledged
                for envelope in pending:
                    after_id = envelope.id
                    if isinstance(envelope, InvalidBattleOutboxEntry):
                        logger.error("Corrupt diagnostic event %s: %s", envelope.id,
                                     envelope.decode_error)
                        try:
                            if not await self.store.defer_invalid_battle_event(envelope):
                                logger.error("Failed to quarantine event %s", envelope.id)
                        except Exception:
                            logger.exception("Failed to quarantine event %s", envelope.id)
                        continue
                    try:
                        outcome, traces = await self._decode_event(envelope)
                        if not await self._persist(outcome, envelope.battle_id, traces):
                            raise RuntimeError("Diagnostic projection was not committed")
                        acknowledged += int(
                            await self.store.ack_battle_event(
                                envelope.id,
                                namespace=LEGACY_DIAGNOSTICS_NAMESPACE,
                            )
                        )
                    except Exception as exc:
                        logger.exception("Не удалось обработать событие боя %s", envelope.id)
                        try:
                            await self.store.fail_battle_event(
                                envelope.id,
                                namespace=LEGACY_DIAGNOSTICS_NAMESPACE,
                                error=str(exc),
                                retry_after_seconds=min(3600, 60 * 2 ** min(envelope.attempts, 6)),
                            )
                        except Exception:
                            logger.exception("Не удалось записать ошибку события %s", envelope.id)

    async def _persist(
        self,
        outcome: BattleOutcome,
        battle_id: int,
        payloads: tuple[Mapping[str, object], ...],
    ) -> bool:
        if not payloads:
            return False
        if any(not self._supports_payload(trace) for trace in payloads):
            logger.warning("Пропущена несовместимая схема legacy-диагностики боя %s", battle_id)
            return False
        happened_at = outcome.happened_at.astimezone(UTC).isoformat()
        traces: list[dict[str, object]] = []
        prepared: list[LegacyDecisionRow] = []
        for sequence_number, raw in enumerate(payloads, start=1):
            trace = dict(raw)
            trace["ruleset_namespace"] = LEGACY_COMBAT_KNOWLEDGE_NAMESPACE
            trace["trace_schema_version"] = LEGACY_TRACE_SCHEMA_VERSION
            decision = resolved_decision(trace)
            raw_round = trace.get("round_number")
            prepared.append(
                LegacyDecisionRow(
                    sequence_number=sequence_number,
                    created_at=str(trace.get("created_at") or happened_at),
                    telegram_message_id=int(str(trace.get("telegram_message_id") or 0)),
                    target_name=str(trace.get("target_name") or outcome.target_name),
                    round_number=raw_round if type(raw_round) is int else None,
                    chosen_skill=str(decision.get("skill_name") or "неизвестно"),
                    chosen_target=str(decision.get("target") or "unknown"),
                    reason=str(decision.get("reason") or ""),
                    urgent=int(bool(decision.get("urgent"))),
                    trace_json=json.dumps(
                        trace, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ),
                )
            )
            traces.append(trace)
        summary = self._prepare_analysis(traces)
        await self._ensure_schema()
        try:
            async with self.store.diagnostics_transaction() as connection:
                if not connection.execute(
                    "SELECT 1 FROM battles WHERE id=?", (battle_id,)
                ).fetchone():
                    return False
                connection.executemany(
                    """
                    INSERT INTO combat_decisions(
                        battle_id,sequence_number,created_at,telegram_message_id,
                        target_name,round_number,chosen_skill,chosen_target,reason,urgent,trace_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(battle_id,sequence_number) DO NOTHING
                    """,
                    [row.parameters(battle_id) for row in prepared],
                )
                for expected in prepared:
                    actual = connection.execute(
                        "SELECT battle_id,sequence_number,created_at,telegram_message_id,"
                        "target_name,round_number,chosen_skill,chosen_target,"
                        "reason,urgent,trace_json "
                        "FROM combat_decisions WHERE battle_id=? AND sequence_number=?",
                        (battle_id, expected.sequence_number),
                    ).fetchone()
                    if actual is None:
                        raise RuntimeError("SQLite did not save a diagnostic decision")
                    actual_values = list(actual)
                    actual_values[-1] = json.dumps(
                        json.loads(str(actual_values[-1])),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if tuple(actual_values) != expected.parameters(battle_id):
                        raise IdempotencyConflict("Conflicting diagnostic decision sequence")
                actual_count = connection.execute(
                    "SELECT COUNT(*) FROM combat_decisions WHERE battle_id=?", (battle_id,),
                ).fetchone()[0]
                if actual_count != len(prepared):
                    raise IdempotencyConflict("Conflicting complete diagnostic trace count")
        except IdempotencyConflict:
            raise
        except Exception:
            logger.exception("Не удалось сохранить решения боя %s", battle_id)
            return False
        if summary is None:
            return False
        try:
            async with self.store.diagnostics_transaction() as connection:
                if not connection.execute(
                    "SELECT 1 FROM battles WHERE id=?", (battle_id,)
                ).fetchone():
                    return False
                self._write_analysis(
                    connection,
                    battle_id=battle_id,
                    target_name=outcome.target_name,
                    result=outcome.result,
                    happened_at=happened_at,
                    summary=summary,
                )
        except IdempotencyConflict:
            raise
        except Exception:
            logger.exception("Не удалось сохранить аналитику боя %s", battle_id)
            return False
        return True

    @staticmethod
    def _write_analysis(
        connection: sqlite3.Connection,
        *,
        battle_id: int,
        target_name: str,
        result: str,
        happened_at: str,
        summary: BattleLearningSummary,
    ) -> bool:
        parameters = (
            battle_id,
            target_name,
            result,
            happened_at,
            summary.profile_max_hp,
            summary.model_version,
            summary.rounds,
            summary.total_actions,
            summary.offensive_actions,
            summary.self_heals,
            summary.renewals,
            summary.minimum_hp,
            summary.minimum_hp_percent,
            summary.last_decision_hp,
            summary.minimum_mana,
            summary.last_decision_mana,
            summary.effective_self_healing,
            summary.lost_healing_potential,
            summary.dangerous_turns,
            summary.shadow_decisions,
            summary.shadow_confident,
            summary.shadow_agreements,
            summary.policy_key,
            datetime.now(UTC).isoformat(),
        )
        cursor = connection.execute(
            """
            INSERT INTO combat_battle_analysis(
                battle_id,target_name,result,happened_at,profile_max_hp,
                model_version,rounds,total_actions,offensive_actions,self_heals,
                renewals,minimum_hp,minimum_hp_percent,last_decision_hp,
                minimum_mana,last_decision_mana,effective_self_healing,
                lost_healing_potential,dangerous_turns,shadow_decisions,
                shadow_confident,shadow_agreements,policy_key,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(battle_id) DO NOTHING
            """,
            parameters,
        )
        actual = connection.execute(
            "SELECT battle_id,target_name,result,happened_at,profile_max_hp,"
            "model_version,rounds,total_actions,offensive_actions,self_heals,"
            "renewals,minimum_hp,minimum_hp_percent,last_decision_hp,"
            "minimum_mana,last_decision_mana,effective_self_healing,"
            "lost_healing_potential,dangerous_turns,shadow_decisions,"
            "shadow_confident,shadow_agreements,policy_key "
            "FROM combat_battle_analysis WHERE battle_id=?", (battle_id,),
        ).fetchone()
        if actual is None:
            raise RuntimeError("SQLite did not save diagnostic analysis")
        if tuple(actual) != parameters[:-1]:
            raise IdempotencyConflict("Conflicting diagnostic analysis content")
        return cursor.rowcount > 0

    async def backfill(self) -> int:
        await self._ensure_schema()
        drained = await self.drain_pending(include_deferred=True)
        async with self.store.diagnostics_reader() as connection:
            battles = connection.execute(
                """SELECT b.id,b.target_name,b.result,b.happened_at FROM battles b
                LEFT JOIN combat_battle_analysis a ON a.battle_id=b.id
                WHERE a.battle_id IS NULL AND EXISTS(
                    SELECT 1 FROM combat_decisions cd WHERE cd.battle_id=b.id
                ) ORDER BY b.id"""
            ).fetchall()
        written = drained
        for battle in battles:
            async with self.store.diagnostics_reader() as connection:
                rows = connection.execute(
                    "SELECT trace_json FROM combat_decisions WHERE battle_id=? "
                    "ORDER BY sequence_number",
                    (int(battle["id"]),),
                ).fetchall()
            traces: list[dict[str, object]] = []
            for row in rows:
                trace = self._decode_trace(str(row["trace_json"]))
                if trace is None:
                    # A mixed battle is not a partial legacy battle: do not project it.
                    traces.clear()
                    break
                traces.append(trace)
            summary = self._prepare_analysis(traces)
            if summary is None:
                continue
            try:
                async with self.store.diagnostics_transaction() as connection:
                    if not connection.execute(
                        "SELECT 1 FROM battles WHERE id=?", (int(battle["id"]),)
                    ).fetchone():
                        continue
                    inserted = self._write_analysis(
                        connection,
                        battle_id=int(battle["id"]),
                        target_name=str(battle["target_name"]),
                        result=str(battle["result"]),
                        happened_at=str(battle["happened_at"]),
                        summary=summary,
                    )
                written += int(inserted)
            except Exception:
                logger.exception("Не удалось восстановить legacy-аналитику боя %s", battle["id"])
        return written

    async def cleanup(self) -> int:
        """Prune only supported legacy traces with an existing compact projection."""
        await self._ensure_schema()
        async with self.store.diagnostics_reader() as connection:
            rows = connection.execute(
                "SELECT cd.id,cd.trace_json FROM combat_decisions cd "
                "JOIN combat_battle_analysis a ON a.battle_id=cd.battle_id"
            ).fetchall()
        outdated: list[tuple[int, str]] = []
        for row in rows:
            raw = str(row["trace_json"])
            trace = self._decode_trace(raw)
            if (
                trace is not None
                and cast(int, trace.get("model_version", 0)) < COMBAT_MODEL_VERSION
            ):
                outdated.append((int(row["id"]), raw))
        async with self.store.diagnostics_transaction() as connection:
            if not outdated:
                return 0
            cursor = connection.executemany(
                "DELETE FROM combat_decisions WHERE id=? AND trace_json=?", outdated
            )
            return max(0, cursor.rowcount)

    async def get_decisions(self, target_name: str | None = None) -> list[CombatDecisionRow]:
        await self._ensure_schema()
        query = "SELECT cd.*,b.result FROM combat_decisions cd JOIN battles b ON b.id=cd.battle_id"
        params: tuple[str, ...] = ()
        if target_name is not None:
            query += " WHERE cd.target_name=?"
            params = (target_name,)
        query += " ORDER BY cd.id"
        async with self.store.diagnostics_reader() as connection:
            rows = connection.execute(query, params).fetchall()
        result: list[CombatDecisionRow] = []
        for row in rows:
            trace = self._decode_trace(str(row["trace_json"]))
            if trace is None:
                continue
            item = dict(row)
            item.pop("trace_json")
            item["trace"] = trace
            result.append(cast(CombatDecisionRow, item))
        return result

    async def learning_stats(
        self,
        *,
        target_name: str | None = None,
        profile_max_hp: int | None = None,
    ) -> list[dict[str, object]]:
        await self._ensure_schema()
        conditions: list[str] = []
        params: list[object] = []
        if target_name is not None:
            conditions.append("a.target_name=?")
            params.append(target_name)
        if profile_max_hp is not None:
            conditions.append("a.profile_max_hp=?")
            params.append(profile_max_hp)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""
            SELECT a.*
            FROM combat_battle_analysis a
            {where}
            ORDER BY a.happened_at,a.battle_id
        """
        async with self.store.diagnostics_reader() as connection:
            return [dict(row) for row in connection.execute(query, tuple(params)).fetchall()]

    async def learning_overview(
        self,
        *,
        target_name: str | None = None,
        profile_max_hp: int | None = None,
    ) -> list[dict[str, object]]:
        await self._ensure_schema()
        conditions: list[str] = []
        params: list[object] = []
        if target_name is not None:
            conditions.append("a.target_name=?")
            params.append(target_name)
        if profile_max_hp is not None:
            conditions.append("a.profile_max_hp=?")
            params.append(profile_max_hp)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""
            SELECT a.profile_max_hp,a.target_name,a.policy_key,
                   COUNT(*) AS battles,
                   SUM(CASE WHEN a.result='VICTORY' THEN 1 ELSE 0 END) AS victories,
                   SUM(CASE WHEN a.result='DEFEAT' THEN 1 ELSE 0 END) AS defeats,
                   AVG(a.rounds) AS average_rounds,
                   MIN(CASE WHEN a.result='VICTORY' THEN a.rounds END)
                       AS best_victory_rounds,
                   AVG(a.minimum_hp_percent) AS average_minimum_hp_percent,
                   MIN(a.minimum_hp_percent) AS minimum_hp_percent,
                   CASE WHEN SUM(a.total_actions)>0
                       THEN CAST(SUM(a.offensive_actions) AS REAL)
                            / SUM(a.total_actions)
                       ELSE 0 END AS offensive_ratio,
                   SUM(a.self_heals) AS self_heals,
                   SUM(a.renewals) AS renewals,
                   SUM(a.lost_healing_potential) AS lost_healing_potential,
                   SUM(a.dangerous_turns) AS dangerous_turns,
                   SUM(a.shadow_confident) AS shadow_confident,
                   SUM(a.shadow_agreements) AS shadow_agreements,
                   CASE WHEN SUM(a.shadow_confident)>0
                       THEN CAST(SUM(a.shadow_agreements) AS REAL)
                            / SUM(a.shadow_confident)
                       ELSE NULL END AS shadow_agreement_rate
            FROM combat_battle_analysis a
            {where}
            GROUP BY a.profile_max_hp,a.target_name,a.policy_key
            ORDER BY victories DESC,average_rounds ASC,battles DESC
        """
        async with self.store.diagnostics_reader() as connection:
            return [dict(row) for row in connection.execute(query, tuple(params)).fetchall()]
