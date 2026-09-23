from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

# Compatibility re-export; new domain/persistence callers import battle_records directly.
from battle_records import BattleResult as BattleResult
from json_types import JsonValue as JsonValue


@dataclass(frozen=True)
class SessionSummary:
    session_id: int | None
    started_at: str | None
    status: str
    wins: int
    defeats: int
    xp: int
    dust: int
    runtime_seconds: int


class FarmerState(TypedDict):
    """Complete persisted singleton; nullable columns remain required keys."""

    singleton: int
    process_status: str
    game_state: str
    position_x: int | None
    position_y: int | None
    current_hp: int | None
    max_hp: int | None
    active_target: str | None
    moves: int
    last_action: str | None
    last_progress_at: str | None
    last_error: str | None
    session_id: int | None
    current_cycle: int
    cycles_count: int
    moves_in_cycle: int
    moves_per_cycle: int
    rest_until: str | None
    pause_requested: int


class FarmerStatePatch(TypedDict, total=False):
    """Writable fields only; omitted keys preserve their persisted values."""

    process_status: str
    game_state: str
    position_x: int | None
    position_y: int | None
    current_hp: int | None
    max_hp: int | None
    active_target: str | None
    moves: int
    last_action: str | None
    last_progress_at: str | None
    last_error: str | None
    session_id: int | None
    current_cycle: int
    cycles_count: int
    moves_in_cycle: int
    moves_per_cycle: int
    rest_until: str | None
    pause_requested: int


class TelegramSafetyStatus(TypedDict):
    telegram_cooldown_remaining: int
    telegram_cooldown_until: str | None
    telegram_cooldown_reason: str | None
    telegram_actions_1m: int
    telegram_actions_10m: int


class RuntimeStatus(FarmerState, TelegramSafetyStatus):
    """Complete control-panel state augmented with process-local observations."""

    task_running: bool
    rest_token: str | None
    location_name: str | None


class BattleTotals(TypedDict):
    battles: int
    wins: int
    defeats: int
    xp: int
    dust: int
    crystals: int


class DropTotals(TypedDict):
    items: int
    cards: int


class TargetTotals(TypedDict):
    target_name: str
    battles: int
    wins: int
    xp: int
    dust: int
    crystals: int


class DropSummary(TypedDict):
    item_name: str
    quantity: int
    is_card: int


class EventSummary(TypedDict):
    created_at: str
    level: str
    event_type: str
    message: str


class StatisticsDashboard(TypedDict):
    session: SessionSummary
    battle: BattleTotals
    drops: DropTotals
    targets: list[TargetTotals]
    state: FarmerState
    runtime_seconds: int


class TelegramActivityDay(TypedDict):
    day: str
    outgoing_total: int
    inline_callbacks: int
    map_requests: int
    peak_actions_1m: int
    peak_actions_10m: int
    incoming_new_messages: int
    incoming_message_edits: int
    incoming_semantic_states: int
    callback_successes: int
    callback_timeouts: int
    flood_waits: int
    flood_wait_seconds: int
    recovery_attempts: int
    silent_stalls: int
    manual_restriction_marks: int
    rpc_errors: int
