from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Iterable

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import InputRichMessage, ReplyKeyboardMarkup


def _e(value: object) -> str:
    return escape(str(value), quote=True)


def rich_document(title: str, body: str, *, subtitle: str | None = None) -> str:
    subtitle_html = f"<p>{_e(subtitle)}</p>" if subtitle else ""
    return f"<h2>{_e(title)}</h2>{subtitle_html}{body}"


def rich_table(
    rows: Iterable[tuple[object, object]],
    *,
    headers: tuple[str, str] = ("Показатель", "Значение"),
    caption: str | None = None,
) -> str:
    caption_html = f"<caption>{_e(caption)}</caption>" if caption else ""
    body = "".join(
        f"<tr><td>{_e(left)}</td><td align=\"right\">{_e(right)}</td></tr>"
        for left, right in rows
    )
    return (
        "<table bordered striped>"
        f"{caption_html}"
        f"<tr><th>{_e(headers[0])}</th><th>{_e(headers[1])}</th></tr>"
        f"{body}</table>"
    )


def status_rich(state: dict) -> str:
    running = bool(state.get("task_running"))
    game_state = str(state.get("game_state") or "STOPPED")
    names = {
        "STARTING": "Запуск",
        "MAP": "Карта",
        "MOVING": "Перемещение",
        "TARGET_SELECTION": "Выбор цели",
        "COMBAT": "Бой",
        "RECOVERY": "Восстановление HP",
        "PAUSED": "Пауза",
        "RESTING": "Передышка",
        "STOPPED": "Остановлен",
        "ERROR": "Ошибка",
    }
    icon = "🟡" if game_state == "PAUSED" else "😴" if game_state == "RESTING" else "🟢" if running else "🔴"
    position = (
        f"({state.get('position_x')}, {state.get('position_y')})"
        if state.get("position_x") is not None
        else "неизвестна"
    )
    rows = [
        ("Статус", "Работает" if running else "Остановлен"),
        ("Режим", names.get(game_state, game_state)),
        ("Позиция", position),
        ("HP", f"{state.get('current_hp') or '—'}/{state.get('max_hp') or '—'}"),
        ("Цель", state.get("active_target") or "нет"),
        ("Цикл", f"{state.get('current_cycle', 1)}/{state.get('cycles_count', 1)}"),
        ("Ход", f"{state.get('moves_in_cycle', 0)}/{state.get('moves_per_cycle', 80)}"),
        ("Всего ходов", state.get("moves", 0)),
    ]
    details = rich_table(
        [
            ("Последнее действие", state.get("last_action") or "нет"),
            ("Последняя ошибка", state.get("last_error") or "нет"),
            ("Последний прогресс", state.get("last_progress_at") or "нет"),
        ],
        headers=("Служебное поле", "Данные"),
    )
    return rich_document(
        f"{icon} Состояние фармера",
        rich_table(rows) + f"<details><summary>Диагностика</summary>{details}</details>",
    )


def _duration(seconds: int) -> str:
    hours, remainder = divmod(max(0, int(seconds)), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def stats_rich(data: dict) -> str:
    battle = data["battle"]
    drops = data["drops"]
    state = data["state"]
    rows = [
        ("⏱ Время", _duration(data.get("runtime_seconds", 0))),
        ("⚔️ Боев", battle.get("battles", 0)),
        ("🏆 Побед", battle.get("wins", 0)),
        ("☠️ Поражений", battle.get("defeats", 0)),
        ("✨ Опыт", battle.get("xp", 0)),
        ("💠 Туманная пыль", battle.get("dust", 0)),
        ("🎁 Предметов", drops.get("items", 0)),
        ("🃏 Карт", drops.get("cards", 0)),
        ("👣 Перемещений", state.get("moves", 0)),
        ("🔄 Цикл", f"{state.get('current_cycle',1)}/{state.get('cycles_count',1)}"),
    ]
    target_rows = [
        (row["target_name"], f"{row['wins']} побед · {row['xp']} XP · {row['dust']} пыли")
        for row in data.get("targets", [])
    ]
    body = rich_table(rows)
    if target_rows:
        body += f"<details open><summary>🎯 По мобам</summary>{rich_table(target_rows, headers=('Моб','Результат'))}</details>"
    return rich_document("📈 Статистика сессии", body)

def drops_rich(drops: list[dict]) -> str:
    if not drops:
        return rich_document("🎁 Дроп текущей сессии", "<p>Предметов пока нет.</p>")

    regular = [row for row in drops if not row["is_card"]]
    cards = [row for row in drops if row["is_card"]]

    blocks: list[str] = []
    if regular:
        blocks.append(
            rich_table(
                [(row["item_name"], row["quantity"]) for row in regular],
                headers=("Предмет", "Количество"),
            )
        )
    else:
        blocks.append("<p>Обычного дропа нет.</p>")

    card_body = (
        rich_table(
            [(row["item_name"], row["quantity"]) for row in cards],
            headers=("Карта", "Количество"),
        )
        if cards
        else "<p>Карты мобов не выпадали.</p>"
    )
    blocks.append(f"<details open><summary>🃏 Карты мобов</summary>{card_body}</details>")
    return rich_document("🎁 Дроп текущей сессии", "".join(blocks))


def events_rich(events: list[dict]) -> str:
    if not events:
        return rich_document("⚠️ Последние события", "<p>Событий пока нет.</p>")

    rows = []
    for event in events:
        stamp = str(event["created_at"]).replace("T", " ")[:19]
        rows.append((stamp, f"{event['level']} · {event['message']}"))

    return rich_document(
        "⚠️ Последние события",
        rich_table(rows, headers=("Время", "Событие")),
    )


def settings_rich(settings) -> str:
    s = settings.values
    targets = "<ul>" + "".join(
        f"<li>{_e(target)}</li>" for target in s.enabled_targets
    ) + "</ul>"
    delays = rich_table(
        [
            ("Перемещение", f"{s.move_delay_min:g}–{s.move_delay_max:g} сек."),
            ("Открытие нападения", f"{s.attack_delay_min:g}–{s.attack_delay_max:g} сек."),
            ("Выбор цели", f"{s.target_delay_min:g}–{s.target_delay_max:g} сек."),
            ("Использование навыка", f"{s.skill_delay_min:g}–{s.skill_delay_max:g} сек."),
            ("Длинная пауза", f"{s.long_pause_min:g}–{s.long_pause_max:g} сек."),
            ("Шанс длинной паузы", f"{s.long_pause_chance * 100:g}%"),
            ("Между циклами", f"{s.cycle_rest_min / 60:g}–{s.cycle_rest_max / 60:g} мин."),
        ],
        headers=("Задержка", "Диапазон"),
    )
    body = rich_table(
        [
            ("Количество циклов", s.cycles_count),
            ("Ходов в цикле", s.moves_per_cycle),
            ("Максимум HP", s.max_hp),
            ("Максимум маны", s.max_mana),
            ("Сила лечения", s.heal_amount),
            ("Порог лечения", max(1, s.max_hp - s.heal_amount)),
        ]
    )
    body += f"<details open><summary>🎯 Активные цели</summary>{targets}</details>"
    body += f"<details><summary>⏱ Задержки</summary>{delays}</details>"
    return rich_document("⚙️ Настройки", body)


def notification_rich(title: str, rows: list[tuple[object, object]] | None = None, text: str | None = None) -> str:
    body = ""
    if text:
        body += f"<p>{_e(text)}</p>"
    if rows:
        body += rich_table(rows)
    return rich_document(title, body)


async def send_rich_with_fallback(
    bot: Bot,
    *,
    chat_id: int,
    html: str,
    fallback_text: str,
    reply_markup: ReplyKeyboardMarkup | None = None,
    disable_notification: bool | None = None,
) -> None:
    """Отправляет настоящий Rich Message; при несовместимости — обычный HTML."""
    try:
        await bot.send_rich_message(
            chat_id=chat_id,
            rich_message=InputRichMessage(
                html=html,
                skip_entity_detection=True,
            ),
            reply_markup=reply_markup,
            disable_notification=disable_notification,
        )
    except (TelegramBadRequest, TelegramNetworkError):
        await bot.send_message(
            chat_id=chat_id,
            text=fallback_text,
            reply_markup=reply_markup,
            disable_notification=disable_notification,
        )
