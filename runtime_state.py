from __future__ import annotations

from enum import Enum, auto


class BotState(Enum):
    STARTING = auto()
    MAP = auto()
    MOVING = auto()
    TARGET_SELECTION = auto()
    COMBAT = auto()
    RECOVERY = auto()
    PAUSED = auto()
    RESTING = auto()
    ACTIVITY_BREAK = auto()
    WAITING_FOR_HEALTH = auto()
    STOPPED = auto()


# These are the built-in phases, not an exhaustive persistence whitelist.
GAME_STATE_NAMES = frozenset(BotState.__members__) | {"ERROR"}
PROCESS_STATUS_NAMES = frozenset({"RUNNING", "PAUSED", "STOPPED", "ERROR"})

MAX_PHASE_NAME_BYTES = 64


def require_phase_name(value: object, name: str = "Mechanism phase") -> str:
    """Return an enum-style ASCII phase name accepted at runtime boundaries."""
    if type(value) is not str:
        raise ValueError(f"{name} must be a canonical phase name")
    encoded = value.encode("utf-8")
    if not encoded or len(encoded) > MAX_PHASE_NAME_BYTES:
        raise ValueError(
            f"{name} must contain between 1 and {MAX_PHASE_NAME_BYTES} ASCII bytes"
        )
    first, *remaining = encoded
    if not 65 <= first <= 90 or any(
        byte != 95 and not 65 <= byte <= 90 and not 48 <= byte <= 57
        for byte in remaining
    ):
        raise ValueError(
            f"{name} must match [A-Z][A-Z0-9_]* without whitespace or controls"
        )
    return value
