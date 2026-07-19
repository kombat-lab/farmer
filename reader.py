from __future__ import annotations

import asyncio
from datetime import datetime

from telethon import TelegramClient, events
from telethon.errors import RPCError

from config import (
    API_HASH,
    API_ID,
    CHARACTER_NAME,
    GAME_BOT,
    SESSION_NAME,
    TARGET_MONSTERS,
)
from models import MessageKind
from parser import (
    classify_message,
    extract_player_hp,
    parse_map,
)


def format_date(value) -> str:
    if value is None:
        return "не указано"

    return value.astimezone().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def get_button_texts(message) -> list[str]:
    if not message.buttons:
        return []

    return [
        getattr(button, "text", "")
        for row in message.buttons
        for button in row
        if getattr(button, "text", "")
    ]


def print_buttons(message) -> None:
    if not message.buttons:
        print("Кнопки: отсутствуют")
        return

    print("Кнопки:")

    for row_index, row in enumerate(message.buttons):
        labels = [
            f"[{row_index},{column_index}] "
            f"{getattr(button, 'text', '')!r}"
            for column_index, button in enumerate(row)
        ]
        print("  " + " | ".join(labels))


def print_parsed_state(text: str) -> None:
    kind = classify_message(
        text,
        TARGET_MONSTERS,
        CHARACTER_NAME,
    )
    hp = extract_player_hp(
        text,
        CHARACTER_NAME,
    )

    print()
    print("Результат парсинга:")
    print(f"  Тип сообщения: {kind.name}")

    if hp:
        print(f"  HP персонажа: {hp[0]}/{hp[1]}")

    if kind is not MessageKind.MAP:
        return

    map_info = parse_map(
        text,
        TARGET_MONSTERS,
        CHARACTER_NAME,
    )
    if map_info is None:
        return

    print(f"  Позиция: {map_info.position}")
    print(f"  Монстров заявлено: {map_info.monster_count}")
    print(f"  Монстры в тексте: {list(map_info.monsters)}")
    print(
        f"  Список сокращён: "
        f"{'ДА' if map_info.has_hidden_monsters else 'НЕТ'}"
    )
    print(
        f"  Найденная цель: "
        f"{map_info.found_target or 'нет'}"
    )
    print(
        f"  Переход завершён: "
        f"{'ДА' if map_info.movement_finished else 'НЕТ'}"
    )


def print_message(message, event_type: str) -> None:
    text = message.raw_text or ""

    print()
    print("=" * 90)
    print(f"Событие: {event_type}")
    print(
        f"Локальное время: "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    print(f"Дата сообщения: {format_date(message.date)}")
    print(f"ID сообщения: {message.id}")
    print(f"Исходящее: {message.out}")
    print(
        f"Отредактировано: "
        f"{'да' if message.edit_date else 'нет'}"
    )

    if message.edit_date:
        print(
            f"Дата редактирования: "
            f"{format_date(message.edit_date)}"
        )

    print("-" * 90)
    print(text or "[сообщение не содержит текста]")
    print("-" * 90)
    print_buttons(message)
    print("=" * 90)
    print_parsed_state(text)


def validate_config() -> None:
    if not isinstance(API_ID, int) or API_ID <= 0:
        raise ValueError(
            "API_ID должен быть положительным числом."
        )

    if not isinstance(API_HASH, str) or not API_HASH.strip():
        raise ValueError("API_HASH не заполнен.")

    if not isinstance(GAME_BOT, str) or not GAME_BOT.startswith("@"):
        raise ValueError(
            "GAME_BOT должен начинаться с @."
        )


async def main() -> None:
    validate_config()

    client = TelegramClient(
        SESSION_NAME,
        API_ID,
        API_HASH,
    )

    try:
        await client.start()

        me = await client.get_me()
        game_bot = await client.get_entity(GAME_BOT)

        print("=" * 90)
        print("Подключение к Telegram выполнено")
        print(f"Аккаунт: {me.first_name or 'без имени'}")
        print(f"Игровой бот: {GAME_BOT}")
        print(f"ID игрового бота: {game_bot.id}")
        print("Режим: только чтение")
        print("Для остановки нажми Ctrl+C.")
        print("=" * 90)

        messages = await client.get_messages(
            game_bot,
            limit=10,
        )

        for message in reversed(messages):
            print_message(message, "ИСТОРИЯ")

        @client.on(events.NewMessage(chats=game_bot))
        async def new_message_handler(event) -> None:
            print_message(
                event.message,
                "НОВОЕ СООБЩЕНИЕ",
            )

        @client.on(events.MessageEdited(chats=game_bot))
        async def edited_message_handler(event) -> None:
            print_message(
                event.message,
                "ИЗМЕНЕНИЕ СООБЩЕНИЯ",
            )

        print("\nОжидание новых событий...\n")
        await client.run_until_disconnected()

    except ValueError as error:
        print(f"\nОшибка настройки: {error}")
    except RPCError as error:
        print(
            f"\nОшибка Telegram API: "
            f"{type(error).__name__}: {error}"
        )
    except Exception as error:
        print(
            f"\nНеожиданная ошибка: "
            f"{type(error).__name__}: {error}"
        )
    finally:
        if client.is_connected():
            await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nПрограмма остановлена пользователем.")
