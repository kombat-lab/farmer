from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from game_input import ActionOutcome
from models import ActionType
from parser import normalize

BLESSING_REFRESH_INTERVAL = 29 * 60
BLESSING_RETRY_INTERVAL = 5 * 60
NON_COMBAT_SKILLS_BUTTON = "Небоевые навыки"
BLESSING_BUTTON = "Благословение"
BLESSING_STATUS_MARKER = "благословение: +5 ко всем характеристикам на 30 мин"


ClickButton = Callable[..., Awaitable[ActionOutcome]]
FindButton = Callable[..., object | None]
Log = Callable[[str], None]
MarkProgress = Callable[[str], None]

_DISPATCHED_OUTCOMES = frozenset(
    {
        ActionOutcome.SENT,
        ActionOutcome.DELIVERY_UNKNOWN,
        ActionOutcome.DUPLICATE,
    }
)


def _validated_outcome(value: object) -> ActionOutcome:
    if not isinstance(value, ActionOutcome):
        raise ValueError("Blessing callback must return ActionOutcome")
    return value


class BlessingManager:
    """Управляет периодическим обновлением небоевого бафа Благословение."""

    def __init__(self) -> None:
        self.refreshed_at: float | None = None
        self.next_attempt_at = 0.0
        self.refresh_in_progress = False

    def refresh_due(self) -> bool:
        now = time.monotonic()
        if self.refresh_in_progress or now < self.next_attempt_at:
            return False
        if self.refreshed_at is None:
            return True
        return now - self.refreshed_at >= BLESSING_REFRESH_INTERVAL

    def cancel(self) -> bool:
        was_in_progress = self.refresh_in_progress
        self.refresh_in_progress = False
        return was_in_progress

    async def try_open_from_map(
        self,
        *,
        click_button: ClickButton,
        log: Log,
        mark_progress: MarkProgress,
    ) -> bool:
        if not self.refresh_due():
            return False

        outcome = _validated_outcome(
            await click_button(
                contains=(NON_COMBAT_SKILLS_BUTTON,),
                action_type=ActionType.OPEN_ATTACK,
                description=NON_COMBAT_SKILLS_BUTTON,
            )
        )
        if outcome in _DISPATCHED_OUTCOMES:
            self.refresh_in_progress = True
            self.next_attempt_at = time.monotonic() + BLESSING_RETRY_INTERVAL
            mark_progress("отправлен запрос открыть меню небоевых навыков")
            return True
        if outcome in {ActionOutcome.DEFERRED, ActionOutcome.STALE}:
            # This map event must not dispatch a second callback. A future fresh
            # event or the cooldown reprocessor may safely try again.
            return True

        self.next_attempt_at = time.monotonic() + BLESSING_RETRY_INTERVAL
        log("Не удалось открыть небоевые навыки. Повторю попытку через 5 минут.")
        return False

    async def handle_menu(
        self,
        message: object,
        *,
        find_button: FindButton,
        click_button: ClickButton,
        mark_progress: MarkProgress,
    ) -> bool:
        if not self.refresh_in_progress:
            return False

        if find_button(message, contains=(BLESSING_BUTTON,)) is None:
            return False

        outcome = _validated_outcome(
            await click_button(
                contains=(BLESSING_BUTTON,),
                action_type=ActionType.USE_SKILL,
                description=BLESSING_BUTTON,
            )
        )
        if outcome in _DISPATCHED_OUTCOMES:
            mark_progress("отправлен запрос использовать Благословение")
        elif outcome is ActionOutcome.REJECTED:
            self.refresh_in_progress = False
            self.next_attempt_at = time.monotonic() + BLESSING_RETRY_INTERVAL
        # DEFERRED and STALE preserve the pending flow. They consume this menu
        # event without pretending that the callback failed or retrying it here.
        return True

    def confirm_from_text(
        self,
        text: str,
        *,
        log: Log,
        mark_progress: MarkProgress,
    ) -> bool:
        if not self.refresh_in_progress:
            return False
        if BLESSING_STATUS_MARKER not in normalize(text):
            return False

        self.refreshed_at = time.monotonic()
        self.next_attempt_at = self.refreshed_at + BLESSING_REFRESH_INTERVAL
        self.refresh_in_progress = False
        log("Благословение подтверждено. Следующее обновление через 29 минут.")
        mark_progress("Благословение обновлено")
        return True
