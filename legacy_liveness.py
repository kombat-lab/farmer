from __future__ import annotations

from liveness import LivenessPhase
from models import BotState

_SUSPENDED_STATES = frozenset(
    {
        BotState.PAUSED,
        BotState.RESTING,
        BotState.ACTIVITY_BREAK,
        BotState.WAITING_FOR_HEALTH,
        BotState.RECOVERY,
    }
)


def liveness_is_suspended(state: BotState) -> bool:
    return state in _SUSPENDED_STATES


def liveness_phase(state: BotState) -> LivenessPhase:
    if state is BotState.MOVING:
        return LivenessPhase.DISCOVERY_ACTION
    if state is BotState.TARGET_SELECTION:
        return LivenessPhase.TARGET_SELECTION
    if state is BotState.COMBAT:
        return LivenessPhase.COMBAT
    if state is BotState.RECOVERY:
        return LivenessPhase.RECOVERY
    return LivenessPhase.GENERAL
