from __future__ import annotations

import asyncio
import logging
import re
from contextlib import suppress
from typing import Iterable

from telethon import TelegramClient, events
from telethon.errors import RPCError

from config import API_HASH, API_ID, GAME_BOT, SESSION_NAME
from notifications import Notifier
from storage import Storage

logger = logging.getLogger("fog_farmer")

INVITE_MARKER = "приглашение в группу"
GROUP_MARKER = "ваша роль: участник"

ACCEPT_BUTTON = "Принять"
NON_COMBAT_SKILLS_BUTTON = "Небоевые навыки"
BLESSING_BUTTON = "Благословение"
LEAVE_GROUP_BUTTON = "Выйти из группы"

BLESSING_STATUS_MARKERS = (
    "статус: ✨ благословение",
    "благословение: +5",
    "благословение действует",
)

MAP_COMMAND = "Карта"


class AutoBuff:
    """Принимает приглашение, применяет Благословение и выходит из группы."""

    def __init__(self, storage: Storage, notifier: Notifier) -> None:
        self.storage = storage
        self.notifier = notifier

        self.client: TelegramClient | None = None
        self.game_bot = None
        self.task: asyncio.Task | None = None

        self.lock = asyncio.Lock()
        self.processing_lock = asyncio.Lock()

        self.enabled = False
        self.last_invite_id: int | None = None
        self.last_player: str | None = None

        self.success_count = 0
        self.error_count = 0

    def is_running(self) -> bool:
        return (
            self.enabled
            and self.task is not None
            and not self.task.done()
        )

    async def start(self) -> tuple[bool, str]:
        async with self.lock:
            if self.is_running():
                return False, "Автобаф уже включён."

            self.enabled = True
            self.task = asyncio.create_task(
                self._run(),
                name="auto-buff",
            )

            try:
                await asyncio.wait_for(
                    self._wait_until_connected(),
                    timeout=20,
                )
            except Exception as error:
                self.enabled = False

                if self.task is not None and not self.task.done():
                    self.task.cancel()

                    with suppress(asyncio.CancelledError):
                        await self.task

                self.task = None

                return (
                    False,
                    "Не удалось запустить автобаф: "
                    f"{type(error).__name__}: {error}",
                )

            return (
                True,
                "Автобаф включён. Ожидаю приглашения в группу.",
            )

    async def stop(self) -> tuple[bool, str]:
        async with self.lock:
            if not self.is_running() and self.task is None:
                self.enabled = False
                return False, "Автобаф уже выключен."

            self.enabled = False

            task = self.task
            self.task = None

            if task is not None and not task.done():
                task.cancel()

                with suppress(asyncio.CancelledError):
                    await task

            elif self.client is not None:
                await self.client.disconnect()

            self.client = None
            self.game_bot = None

            await self.storage.add_event(
                "AUTO_BUFF_STOPPED",
                "Автобаф выключен",
            )

            return True, "Автобаф выключен."

    async def _wait_until_connected(self) -> None:
        while self.enabled:
            if (
                self.client is not None
                and self.client.is_connected()
                and self.game_bot is not None
            ):
                return

            if self.task is not None and self.task.done():
                error = self.task.exception()

                if error is not None:
                    raise error

                raise RuntimeError("Задача автобафа завершилась.")

            await asyncio.sleep(0.1)

        raise RuntimeError("Запуск автобафа отменён.")

    async def _run(self) -> None:
        client = TelegramClient(
            SESSION_NAME,
            API_ID,
            API_HASH,
        )

        self.client = client

        try:
            await client.start()

            self.game_bot = await client.get_entity(GAME_BOT)

            client.add_event_handler(
                self._on_game_message,
                events.NewMessage(chats=self.game_bot),
            )

            client.add_event_handler(
                self._on_game_message,
                events.MessageEdited(chats=self.game_bot),
            )

            await self.storage.add_event(
                "AUTO_BUFF_STARTED",
                "Автобаф включён и ожидает приглашения",
            )

            await client.run_until_disconnected()

        except asyncio.CancelledError:
            raise

        except Exception as error:
            logger.exception("Автобаф аварийно завершён")

            self.error_count += 1

            await self.storage.add_event(
                "AUTO_BUFF_CRASHED",
                f"{type(error).__name__}: {error}",
                level="CRITICAL",
            )

            await self.notifier.send(
                "Автобаф аварийно завершён\n"
                f"{type(error).__name__}: {error}"
            )

            raise

        finally:
            if client.is_connected():
                await client.disconnect()

            self.enabled = False

    async def _on_game_message(self, event) -> None:
        if not self.enabled:
            return

        message = event.message
        text = (message.raw_text or "").casefold()

        if INVITE_MARKER not in text:
            return

        if message.id == self.last_invite_id:
            return

        self.last_invite_id = message.id

        asyncio.create_task(
            self._process_invitation(message),
            name=f"auto-buff-invite-{message.id}",
        )

    async def _process_invitation(self, invite_message) -> None:
        async with self.processing_lock:
            player = self._extract_player(
                invite_message.raw_text or ""
            )

            self.last_player = player

            try:
                logger.info(
                    "Автобаф: получено приглашение от %s",
                    player,
                )

                await self._click(
                    invite_message,
                    exact=ACCEPT_BUTTON,
                )

                await self.storage.add_event(
                    "AUTO_BUFF_INVITE_ACCEPTED",
                    f"Принято приглашение от {player}",
                )

                group_message = await self._wait_for_message(
                    lambda message: (
                        GROUP_MARKER
                        in (message.raw_text or "").casefold()
                    ),
                    timeout=15,
                )

                logger.info(
                    "Автобаф: группа открыта, сообщение=%s",
                    group_message.id,
                )

                skills_message = await self._open_non_combat_skills()

                logger.info(
                    "Автобаф: меню небоевых навыков открыто, сообщение=%s",
                    skills_message.id,
                )

                confirmation_snapshot = (
                    await self._blessing_confirmation_snapshot()
                )

                await self._click_blessing(skills_message)

                logger.info(
                    "Автобаф: кнопка Благословение нажата"
                )

                await self._wait_for_blessing_confirmation(
                    confirmation_snapshot,
                    timeout=20,
                )

                logger.info(
                    "Автобаф: применение Благословения подтверждено"
                )

                fresh_group_message = await self._fresh(
                    group_message
                )

                if (
                    fresh_group_message is None
                    or not self._has_button(
                        fresh_group_message,
                        contains=LEAVE_GROUP_BUTTON,
                    )
                ):
                    fresh_group_message = (
                        await self._find_recent_with_button(
                            LEAVE_GROUP_BUTTON
                        )
                    )

                if fresh_group_message is None:
                    raise RuntimeError(
                        "Не найдена кнопка «Выйти из группы»."
                    )

                await self._click(
                    fresh_group_message,
                    contains=LEAVE_GROUP_BUTTON,
                )

                self.success_count += 1

                await self.storage.add_event(
                    "AUTO_BUFF_COMPLETED",
                    f"Игрок {player} получил Благословение; "
                    "группа покинута",
                )

                await self.notifier.send(
                    "✨ Автобаф выполнен\n"
                    f"Игрок: {player}\n"
                    f"Всего успешно: {self.success_count}"
                )

            except asyncio.CancelledError:
                raise

            except Exception as error:
                self.error_count += 1

                logger.exception(
                    "Ошибка автобафа для %s",
                    player,
                )

                await self.storage.add_event(
                    "AUTO_BUFF_FAILED",
                    f"{player}: "
                    f"{type(error).__name__}: {error}",
                    level="ERROR",
                )

                await self.notifier.send(
                    "⚠️ Ошибка автобафа\n"
                    f"Игрок: {player}\n"
                    f"{type(error).__name__}: {error}\n\n"
                    "Группа не покинута."
                )

    async def _open_non_combat_skills(self):
        map_message = await self._find_recent_with_button(
            NON_COMBAT_SKILLS_BUTTON
        )

        if map_message is None:
            assert self.client is not None

            before_map_id = await self._latest_message_id()

            await self.client.send_message(
                self.game_bot,
                MAP_COMMAND,
            )

            map_message = await self._wait_for_message(
                lambda message: self._has_button(
                    message,
                    contains=NON_COMBAT_SKILLS_BUTTON,
                ),
                timeout=12,
                min_id=before_map_id + 1,
            )

        before_skills_id = await self._latest_message_id()

        await self._click(
            map_message,
            contains=NON_COMBAT_SKILLS_BUTTON,
        )

        skills_message = await self._wait_for_message(
            lambda message: self._has_button(
                message,
                contains=BLESSING_BUTTON,
            ),
            timeout=12,
            min_id=before_skills_id + 1,
        )

        return skills_message

    async def _click_blessing(self, skills_message) -> None:
        """
        Нажимает первую кнопку меню небоевых навыков.

        По структуре игрового сообщения первая кнопка:
        [0][0] — Благословение.
        """

        fresh_message = await self._fresh(skills_message)

        if fresh_message is None:
            raise RuntimeError(
                "Сообщение с небоевыми навыками исчезло."
            )

        buttons = fresh_message.buttons or []

        if not buttons:
            raise RuntimeError(
                "В сообщении небоевых навыков нет кнопок."
            )

        if not buttons[0]:
            raise RuntimeError(
                "Первая строка кнопок небоевых навыков пуста."
            )

        first_button_text = (
            buttons[0][0].text or ""
        ).strip()

        if BLESSING_BUTTON.casefold() not in (
            first_button_text.casefold()
        ):
            available_buttons = [
                text
                for _, _, text
                in self._button_texts(fresh_message)
            ]

            raise RuntimeError(
                "Первая кнопка не является Благословением. "
                f"Первая кнопка: {first_button_text!r}. "
                f"Доступные кнопки: {available_buttons}"
            )

        last_error: Exception | None = None

        for attempt in range(1, 4):
            try:
                fresh_message = await self._fresh(
                    skills_message
                )

                if fresh_message is None:
                    raise RuntimeError(
                        "Сообщение меню больше недоступно."
                    )

                buttons = fresh_message.buttons or []

                if not buttons or not buttons[0]:
                    raise RuntimeError(
                        "Кнопки меню небоевых навыков исчезли."
                    )

                first_button_text = (
                    buttons[0][0].text or ""
                ).strip()

                if BLESSING_BUTTON.casefold() not in (
                    first_button_text.casefold()
                ):
                    raise RuntimeError(
                        "Первая кнопка больше не является "
                        "Благословением."
                    )

                logger.info(
                    "Автобаф: нажимаю первую кнопку "
                    "«%s», message_id=%s, попытка=%s",
                    first_button_text,
                    fresh_message.id,
                    attempt,
                )

                await fresh_message.click(0, 0)

                return

            except Exception as error:
                last_error = error

                logger.warning(
                    "Автобаф: ошибка нажатия Благословения, "
                    "попытка %s/3: %s: %s",
                    attempt,
                    type(error).__name__,
                    error,
                )

                if attempt < 3:
                    await asyncio.sleep(1)

        assert last_error is not None

        raise RuntimeError(
            "Не удалось нажать кнопку «Благословение» "
            "после трёх попыток: "
            f"{type(last_error).__name__}: {last_error}"
        ) from last_error

    async def _blessing_confirmation_snapshot(
        self,
    ) -> dict[int, str]:
        assert self.client is not None

        messages = await self.client.get_messages(
            self.game_bot,
            limit=30,
        )

        return {
            message.id: message.raw_text or ""
            for message in messages
            if self._is_blessing_confirmation(message)
        }

    async def _wait_for_blessing_confirmation(
        self,
        snapshot: dict[int, str],
        *,
        timeout: float,
    ):
        assert self.client is not None

        deadline = (
            asyncio.get_running_loop().time()
            + timeout
        )

        while (
            self.enabled
            and asyncio.get_running_loop().time()
            < deadline
        ):
            messages = await self.client.get_messages(
                self.game_bot,
                limit=30,
            )

            for message in messages:
                if not self._is_blessing_confirmation(
                    message
                ):
                    continue

                current_text = message.raw_text or ""
                previous_text = snapshot.get(message.id)

                if (
                    message.id not in snapshot
                    or previous_text != current_text
                ):
                    return message

            await asyncio.sleep(0.35)

        raise TimeoutError(
            "После нажатия «Благословение» "
            "не получено подтверждение бафа."
        )

    async def _wait_for_message(
        self,
        predicate,
        timeout: float,
        *,
        min_id: int = 0,
    ):
        assert self.client is not None

        deadline = (
            asyncio.get_running_loop().time()
            + timeout
        )

        newest_id = 0

        while (
            self.enabled
            and asyncio.get_running_loop().time()
            < deadline
        ):
            messages = await self.client.get_messages(
                self.game_bot,
                limit=20,
            )

            for message in messages:
                newest_id = max(
                    newest_id,
                    message.id,
                )

                if message.id < min_id:
                    continue

                if predicate(message):
                    return message

            await asyncio.sleep(0.4)

        raise TimeoutError(
            "Ожидаемое сообщение не появилось. "
            f"min_id={min_id}, "
            f"последнее id={newest_id}"
        )

    async def _latest_message_id(self) -> int:
        assert self.client is not None

        messages = await self.client.get_messages(
            self.game_bot,
            limit=1,
        )

        if not messages:
            return 0

        return messages[0].id

    async def _find_recent_with_button(
        self,
        text: str,
    ):
        assert self.client is not None

        messages = await self.client.get_messages(
            self.game_bot,
            limit=30,
        )

        for message in messages:
            if self._has_button(
                message,
                contains=text,
            ):
                return message

        return None

    async def _fresh(self, message):
        assert self.client is not None

        fresh_message = await self.client.get_messages(
            self.game_bot,
            ids=message.id,
        )

        if (
            fresh_message is None
            or not fresh_message.id
        ):
            return None

        return fresh_message

    async def _click(
        self,
        message,
        *,
        exact: str | None = None,
        contains: str | None = None,
    ) -> None:
        fresh_message = await self._fresh(message)

        if fresh_message is None:
            raise RuntimeError(
                "Сообщение с кнопкой исчезло."
            )

        position = self._find_button(
            fresh_message,
            exact=exact,
            contains=contains,
        )

        if position is None:
            requested = (
                exact
                or contains
                or "неизвестная кнопка"
            )

            available_buttons = [
                text
                for _, _, text
                in self._button_texts(fresh_message)
            ]

            raise RuntimeError(
                f"Кнопка «{requested}» не найдена. "
                f"Доступные кнопки: {available_buttons}"
            )

        row, column = position

        try:
            await fresh_message.click(
                row,
                column,
            )

        except RPCError as error:
            raise RuntimeError(
                "Telegram не выполнил нажатие: "
                f"{error}"
            ) from error

    @staticmethod
    def _button_texts(
        message,
    ) -> Iterable[tuple[int, int, str]]:
        for row_index, row in enumerate(
            message.buttons or []
        ):
            for column_index, button in enumerate(row):
                yield (
                    row_index,
                    column_index,
                    (button.text or "").strip(),
                )

    @classmethod
    def _find_button(
        cls,
        message,
        *,
        exact: str | None = None,
        contains: str | None = None,
    ) -> tuple[int, int] | None:
        exact_cf = (
            exact.casefold()
            if exact
            else None
        )

        contains_cf = (
            contains.casefold()
            if contains
            else None
        )

        for row, column, text in cls._button_texts(
            message
        ):
            normalized = text.casefold()

            if (
                exact_cf is not None
                and normalized == exact_cf
            ):
                return row, column

            if (
                contains_cf is not None
                and contains_cf in normalized
            ):
                return row, column

        return None

    @classmethod
    def _has_button(
        cls,
        message,
        *,
        contains: str,
    ) -> bool:
        return (
            cls._find_button(
                message,
                contains=contains,
            )
            is not None
        )

    @staticmethod
    def _is_blessing_confirmation(
        message,
    ) -> bool:
        text = (
            message.raw_text or ""
        ).casefold()

        return any(
            marker in text
            for marker in BLESSING_STATUS_MARKERS
        )

    @staticmethod
    def _extract_player(text: str) -> str:
        match = re.search(
            r"•\s*От:\s*(?:\S+\s*)?(.+)",
            text,
            re.IGNORECASE,
        )

        if not match:
            return "неизвестный игрок"

        return match.group(1).strip()

    async def status(self) -> dict[str, object]:
        return {
            "running": self.is_running(),
            "last_player": self.last_player,
            "success_count": self.success_count,
            "error_count": self.error_count,
        }
