from __future__ import annotations

from collections.abc import Mapping

from battle_records import BattleOutcome, BattleResult, SourceEventId
from legacy_battle_rewards import reward_bundle_from_raw
from legacy_combat_diagnostics import LegacyCombatDiagnostics
from storage import Storage


async def record_legacy_battle(
    storage: Storage,
    *,
    telegram_message_id: int,
    session_id: int | None,
    target_name: str,
    result: BattleResult,
    xp: int = 0,
    dust: int = 0,
    crystals: int = 0,
    items: tuple[str, ...] = (),
    combat_decisions: tuple[Mapping[str, object], ...] = (),
) -> tuple[bool, list[str]]:
    outcome = BattleOutcome(
        source_event_id=SourceEventId(f"test:legacy-message:{telegram_message_id}"),
        source_message_id=telegram_message_id,
        session_id=session_id,
        target_name=target_name,
        result=result,
        rewards=reward_bundle_from_raw(xp=xp, dust=dust, crystals=crystals, items=items),
    )
    result_record = await LegacyCombatDiagnostics(storage).record_payloads(
        outcome,
        combat_decisions=combat_decisions,
    )
    return result_record.inserted, list(result_record.cards)
