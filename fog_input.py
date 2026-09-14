from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from blessing import BLESSING_BUTTON
from game_input import AdmissionDecision, InputDescriptor
from message_snapshot import MessageSnapshot
from models import MessageKind
from parser import classify_message, is_passive_health_notification, parse_map

_COUNTDOWN_RE = re.compile(r"(?im)^\s*⏳\s*Осталось:\s*\d+\s*сек\.?\s*$")
MAP_ACTION_SCOPE = "map"
_MAP_BOUNDARY_KINDS = frozenset(
    {
        MessageKind.TARGET_SELECTION,
        MessageKind.COMBAT_TARGET_SELECTION,
        MessageKind.COMBAT_STARTED,
        MessageKind.PLAYER_TURN,
        MessageKind.BATTLE_FINISHED,
        MessageKind.TARGET_GONE,
    }
)


def semantic_fog_text(text: str) -> str:
    return _COUNTDOWN_RE.sub("", text).strip()


class FoGInputPolicy:
    """FoG prompt identity, independent of queues, RPCs, and runtime ownership."""

    def __init__(
        self,
        *,
        character_name: str,
        enabled_targets: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        self.character_name = character_name
        self._enabled_targets = enabled_targets

    def describe(self, snapshot: MessageSnapshot) -> InputDescriptor:
        semantic_text = semantic_fog_text(snapshot.raw_text)
        targets = tuple(self._enabled_targets()) if self._enabled_targets is not None else ()
        is_map = parse_map(snapshot.raw_text, targets, self.character_name) is not None
        kind = classify_message(snapshot.raw_text, targets, self.character_name, is_map=is_map)
        is_blessing_menu = not is_map and any(
            BLESSING_BUTTON.casefold() in button.text.casefold()
            for row in snapshot.buttons for button in row
        )
        return InputDescriptor(
            snapshot=snapshot,
            fact_key=(snapshot.id, semantic_text),
            state_key=(
                snapshot.id,
                semantic_text,
                tuple(
                    tuple((button.text, button.callback_data) for button in row)
                    for row in snapshot.buttons
                ),
            ),
            is_prompt=not is_passive_health_notification(snapshot.raw_text),
            action_scope=MAP_ACTION_SCOPE if is_map else None,
            advances_action_scopes=(
                (MAP_ACTION_SCOPE,) if kind in _MAP_BOUNDARY_KINDS or is_blessing_menu else ()
            ),
        )

    def admit(
        self,
        previous: InputDescriptor | None,
        current_prompt: InputDescriptor | None,
        incoming: InputDescriptor,
    ) -> AdmissionDecision:
        if previous is None or previous.state_key != incoming.state_key:
            return AdmissionDecision.ACCEPT
        changed_prompt = current_prompt is None or current_prompt.fact_key != incoming.fact_key
        previous_edit = previous.snapshot.edit_date
        incoming_edit = incoming.snapshot.edit_date
        newer_edit = incoming_edit is not None and (
            previous_edit is None
            or incoming.snapshot.revision_timestamp > previous.snapshot.revision_timestamp
        )
        if incoming.action_scope == MAP_ACTION_SCOPE and changed_prompt and newer_edit:
            return AdmissionDecision.ACCEPT
        return AdmissionDecision.DUPLICATE
