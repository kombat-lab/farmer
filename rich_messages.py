from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from settings_service import SettingsService

from collections.abc import Iterable
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import InputRichMessage, ReplyKeyboardMarkup

from config import (
    FAST_ATTACK_DELAY,
    FAST_MOVE_DELAY,
    FAST_SKILL_DELAY,
    FAST_TARGET_DELAY,
)


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
        f'<tr><td>{_e(left)}</td><td align="right">{_e(right)}</td></tr>' for left, right in rows
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
        "ACTIVITY_BREAK": "Длительный перерыв",
        "STOPPED": "Остановлен",
        "ERROR": "Ошибка",
    }
    icon = (
        "🟡"
        if game_state == "PAUSED"
        else "😴"
        if game_state in {"RESTING", "ACTIVITY_BREAK"}
        else "🟢"
        if running
        else "🔴"
    )
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


def stats_rich(data: dict, drop_items: list[dict] | None = None) -> str:
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
        ("🔄 Цикл", f"{state.get('current_cycle', 1)}/{state.get('cycles_count', 1)}"),
    ]
    target_rows = [
        (row["target_name"], f"{row['wins']} побед · {row['xp']} XP · {row['dust']} пыли")
        for row in data.get("targets", [])
    ]
    body = rich_table(rows)
    if target_rows:
        targets_table = rich_table(
            target_rows,
            headers=("Моб", "Результат"),
        )
        body += f"<details open><summary>🎯 По мобам</summary>{targets_table}</details>"
    if drop_items:
        regular = [row for row in drop_items if not row["is_card"]]
        cards = [row for row in drop_items if row["is_card"]]
        drop_rows = [(row["item_name"], row["quantity"]) for row in regular]
        drop_rows.extend((f"🃏 {row['item_name']}", row["quantity"]) for row in cards)
        drop_table = rich_table(
            drop_rows,
            headers=("Дроп", "Количество"),
        )
        body += f"<details><summary>🎁 Полученный дроп</summary>{drop_table}</details>"
    else:
        body += "<details><summary>🎁 Полученный дроп</summary><p>Предметов пока нет.</p></details>"
    return rich_document("📈 Статистика сессии", body)


def events_rich(events: list[dict]) -> str:
    if not events:
        return rich_document("📋 Журнал", "<p>Событий пока нет.</p>")

    rows = []
    for event in events:
        stamp = str(event["created_at"]).replace("T", " ")[:19]
        rows.append((stamp, f"{event['level']} · {event['message']}"))

    return rich_document(
        "📋 Журнал",
        rich_table(rows, headers=("Время", "Событие")),
    )


def settings_rich(settings: SettingsService) -> str:
    s = settings.values
    profile_name = "Быстрый" if s.activity_profile == "fast" else "Обычный"
    activity_break = (
        "отключён"
        if s.activity_profile == "fast"
        else "после 25–40 ходов или 25–45 мин.; отдых 4–8 мин."
    )
    targets = "<ul>" + "".join(f"<li>{_e(target)}</li>" for target in s.enabled_targets) + "</ul>"
    effective_delays = (
        (
            FAST_MOVE_DELAY,
            FAST_ATTACK_DELAY,
            FAST_TARGET_DELAY,
            FAST_SKILL_DELAY,
        )
        if s.activity_profile == "fast"
        else (
            (s.move_delay_min, s.move_delay_max),
            (s.attack_delay_min, s.attack_delay_max),
            (s.target_delay_min, s.target_delay_max),
            (s.skill_delay_min, s.skill_delay_max),
        )
    )
    move_delay, attack_delay, target_delay, skill_delay = effective_delays
    delays = rich_table(
        [
            ("Перемещение", f"{move_delay[0]:g}–{move_delay[1]:g} сек."),
            ("Открытие нападения", f"{attack_delay[0]:g}–{attack_delay[1]:g} сек."),
            ("Выбор цели", f"{target_delay[0]:g}–{target_delay[1]:g} сек."),
            ("Использование навыка", f"{skill_delay[0]:g}–{skill_delay[1]:g} сек."),
            (
                "Короткая пауза",
                "отключена"
                if s.activity_profile == "fast"
                else f"{s.long_pause_min:g}–{s.long_pause_max:g} сек. · "
                f"шанс {s.long_pause_chance * 100:g}%",
            ),
            ("Между циклами", f"{s.cycle_rest_min / 60:g}–{s.cycle_rest_max / 60:g} мин."),
            ("Длительный перерыв", activity_break),
        ],
        headers=("Задержка", "Диапазон"),
    )
    body = rich_table(
        [
            ("Количество циклов", s.cycles_count),
            ("Ходов в цикле", s.moves_per_cycle),
            ("Профиль активности", profile_name),
            ("Порог лечения", s.heal_threshold),
            ("HP перед новым боем", f"{s.battle_start_hp_percent}%"),
            ("Благословение", "включено" if s.blessing_enabled else "выключено"),
        ]
    )
    body += f"<details><summary>🎯 Активные цели</summary>{targets}</details>"
    body += f"<details><summary>⏱ Задержки</summary>{delays}</details>"
    return rich_document("⚙️ Настройки", body)


def notification_rich(
    title: str, rows: list[tuple[object, object]] | None = None, text: str | None = None
) -> str:
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
