from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from models import ActionType
from parser import normalize


BLESSING_REFRESH_INTERVAL = 29 * 60
BLESSING_RETRY_INTERVAL = 5 * 60
NON_COMBAT_SKILLS_BUTTON = "Небоевые навыки"
BLESSING_BUTTON = "Благословение"
BLESSING_STATUS_MARKER = "благословение: +5 ко всем характеристикам на 30 мин"


ClickButton = Callable[..., Awaitable[bool]]
FindButton = Callable[..., object | None]
Log = Callable[[str], None]
MarkProgress = Callable[[str], None]


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

    async def try_open_from_map(
        self,
        message,
        *,
        click_button: ClickButton,
        log: Log,
        mark_progress: MarkProgress,
    ) -> bool:
        if not self.refresh_due():
            return False

        clicked = await click_button(
            message,
            contains=(NON_COMBAT_SKILLS_BUTTON,),
            action_type=ActionType.OPEN_ATTACK,
            description=NON_COMBAT_SKILLS_BUTTON,
        )
        if not clicked:
            self.next_attempt_at = time.monotonic() + BLESSING_RETRY_INTERVAL
            log(
                "Не удалось открыть небоевые навыки. "
                "Повторю попытку через 5 минут."
            )
            return False

        self.refresh_in_progress = True
        self.next_attempt_at = time.monotonic() + BLESSING_RETRY_INTERVAL
        mark_progress("открыто меню небоевых навыков")
        return True

    async def handle_menu(
        self,
        message,
        *,
        find_button: FindButton,
        click_button: ClickButton,
        mark_progress: MarkProgress,
    ) -> bool:
        if not self.refresh_in_progress:
            return False

        if find_button(message, contains=(BLESSING_BUTTON,)) is None:
            return False

        clicked = await click_button(
            message,
            contains=(BLESSING_BUTTON,),
            action_type=ActionType.USE_SKILL,
            description=BLESSING_BUTTON,
        )
        if clicked:
            mark_progress("использовано Благословение")
        else:
            self.refresh_in_progress = False
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
