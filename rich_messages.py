from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from html import escape
from typing import TYPE_CHECKING

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import (
    ForceReply,
    InlineKeyboardMarkup,
    InputRichMessage,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

if TYPE_CHECKING:
    from settings_service import SettingsService

logger = logging.getLogger("fog_farmer")

ReplyMarkup = (
    InlineKeyboardMarkup | ReplyKeyboardMarkup | ReplyKeyboardRemove | ForceReply
)


def _e(value: object) -> str:
    return escape(str(value), quote=True)


def _attribute(value: object) -> str:
    return escape(str(value), quote=True)


def rich_document(title: str, body: str, *, subtitle: str | None = None) -> str:
    subtitle_html = f"<p>{_e(subtitle)}</p>" if subtitle else ""
    return f"<h2>{_e(title)}</h2>{subtitle_html}{body}"


def rich_table(
    rows: Iterable[tuple[object, object]],
    *,
    headers: tuple[str, str] | None = ("Показатель", "Значение"),
    caption: str | None = None,
    compact: bool = True,
) -> str:
    caption_html = f"<caption>{_e(caption)}</caption>" if caption else ""
    header_html = ""
    if headers is not None:
        header_html = f"<tr><th>{_e(headers[0])}</th><th>{_e(headers[1])}</th></tr>"
    body = "".join(
        f'<tr><td>{_e(left)}</td><td align="right">{_e(right)}</td></tr>'
        for left, right in rows
    )
    compact_attr = " compact" if compact else ""
    return (
        f"<table bordered striped{compact_attr}>"
        f"{caption_html}{header_html}{body}</table>"
    )


def rich_button(
    text: str,
    callback_data: str | None = None,
    *,
    style: str | None = None,
    disabled: bool = False,
) -> str:
    """Создаёт кнопку Bot API 10.3 в HTML RichMessage."""
    button_type = "disabled" if disabled else "callback_data"
    attributes = [f'type="{button_type}"']
    if callback_data is not None and not disabled:
        attributes.append(f'data="{_attribute(callback_data)}"')
    if style:
        attributes.append(f'style="{_attribute(style)}"')
    return f"<tg-button {' '.join(attributes)}>{_e(text)}</tg-button>"


def rich_button_row(*buttons: str, align: str = "center") -> str:
    if not buttons:
        return ""
    return f'<tg-button-row align="{_attribute(align)}">{"".join(buttons)}</tg-button-row>'


def rich_notice(text: str | None, *, error: bool = False) -> str:
    if not text:
        return ""
    icon = "⚠️" if error else "✅"
    return f"<aside><b>{icon} {_e(text)}</b></aside>"


def _navigation(active: str) -> str:
    items = (
        ("home", "Обзор", "ui:home"),
        ("stats", "Статистика", "ui:stats"),
        ("settings", "Настройки", "ui:settings"),
        ("events", "Журнал", "ui:events"),
    )
    return rich_button_row(
        *(
            rich_button(
                label,
                callback,
                style="primary" if key == active else "link",
                disabled=key == active,
            )
            for key, label, callback in items
        )
    )


def _state_name(game_state: str) -> str:
    return {
        "STARTING": "Запуск",
        "MAP": "Поиск на карте",
        "MOVING": "Перемещение",
        "TARGET_SELECTION": "Выбор цели",
        "COMBAT": "Бой",
        "RECOVERY": "Восстановление HP",
        "WAITING_FOR_HEALTH": "Восстановление HP",
        "PAUSED": "Пауза",
        "RESTING": "Передышка",
        "ACTIVITY_BREAK": "Длительный перерыв",
        "STOPPED": "Остановлен",
        "ERROR": "Ошибка",
    }.get(game_state, game_state)


def _state_icon(running: bool, game_state: str) -> str:
    if game_state == "ERROR":
        return "🔴"
    if game_state == "PAUSED":
        return "🟡"
    if game_state in {"RESTING", "ACTIVITY_BREAK", "RECOVERY", "WAITING_FOR_HEALTH"}:
        return "😴"
    return "🟢" if running else "⚫️"


def _control_buttons(running: bool, game_state: str) -> str:
    if not running:
        return rich_button_row(
            rich_button("▶️ Запустить", "ctl:start", style="success"),
            rich_button("↻ Обновить", "ui:home", style="primary"),
        )
    if game_state in {"PAUSED", "RESTING", "ACTIVITY_BREAK"}:
        return rich_button_row(
            rich_button("▶️ Продолжить", "ctl:resume", style="success"),
            rich_button("⏹ Стоп", "ctl:stop", style="danger"),
            rich_button("↻", "ui:home", style="primary"),
        )
    return rich_button_row(
        rich_button("⏸ Пауза", "ctl:pause", style="primary"),
        rich_button("⏹ Стоп", "ctl:stop", style="danger"),
        rich_button("↻", "ui:home"),
    )


def dashboard_rich(state: dict, data: dict, *, notice: str | None = None) -> str:
    running = bool(state.get("task_running"))
    game_state = str(state.get("game_state") or "STOPPED")
    icon = _state_icon(running, game_state)
    position = (
        f"({state.get('position_x')}, {state.get('position_y')})"
        if state.get("position_x") is not None
        else "неизвестна"
    )
    current_hp = state.get("current_hp") or "—"
    max_hp = state.get("max_hp") or "—"
    state_rows: list[tuple[object, object]] = [
        ("Состояние", _state_name(game_state)),
        ("Локация", state.get("location_name") or "не определена"),
        ("Позиция", position),
        ("Здоровье", f"{current_hp}/{max_hp}"),
        ("Цель", state.get("active_target") or "нет"),
        (
            "Прогресс",
            f"цикл {state.get('current_cycle', 1)}/{state.get('cycles_count', 1)} · "
            f"ход {state.get('moves_in_cycle', 0)}/{state.get('moves_per_cycle', 0)}",
        ),
    ]
    cooldown_remaining = int(state.get("telegram_cooldown_remaining") or 0)
    if cooldown_remaining > 0:
        state_rows.append(("Telegram-пауза", f"ещё {cooldown_remaining} сек."))

    battle = data["battle"]
    drops = data["drops"]
    summary_rows = [
        ("⚔️ Бои", f"{battle.get('battles', 0)} · побед {battle.get('wins', 0)}"),
        ("✨ Опыт", battle.get("xp", 0)),
        ("💠 Пыль", battle.get("dust", 0)),
        ("💎 Кристаллы", battle.get("crystals", 0)),
        ("🎁 Дроп", f"{drops.get('items', 0)} · карт {drops.get('cards', 0)}"),
    ]

    load_text = (
        f"{state.get('telegram_actions_1m', 0)} / 1 мин. · "
        f"{state.get('telegram_actions_10m', 0)} / 10 мин."
    )
    diagnostics = "<br>".join(
        (
            "<b>Диагностика</b>",
            "Темп: наблюдение без автокоррекции",
            f"Нагрузка Telegram: {_e(load_text)}",
            f"Последнее действие: {_e(state.get('last_action') or 'нет')}",
            f"Последний прогресс: {_e(state.get('last_progress_at') or 'нет')}",
            f"Последняя ошибка: {_e(state.get('last_error') or 'нет')}",
        )
    )
    body = rich_notice(notice)
    body += rich_table(state_rows, headers=None)
    body += _control_buttons(running, game_state)
    body += "<hr/>"
    body += rich_table(summary_rows, headers=None, caption="Текущая сессия")
    body += f"<blockquote expandable>{diagnostics}</blockquote>"
    body += _navigation("home")
    body += "<footer>Панель обновляется по кнопке ↻ и после управляющих действий.</footer>"
    return rich_document(f"{icon} FoG Farmer", body)


def _duration(seconds: int) -> str:
    hours, remainder = divmod(max(0, int(seconds)), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def stats_rich(
    data: dict,
    drop_items: list[dict] | None = None,
    telegram_days: list[dict] | None = None,
    *,
    notice: str | None = None,
) -> str:
    battle = data["battle"]
    drops = data["drops"]
    state = data["state"]
    battles = int(battle.get("battles", 0) or 0)
    wins = int(battle.get("wins", 0) or 0)
    runtime_seconds = int(data.get("runtime_seconds", 0) or 0)
    win_rate = f"{wins / battles * 100:.1f}%" if battles else "—"
    xp_per_hour = (
        round(int(battle.get("xp", 0) or 0) * 3600 / runtime_seconds)
        if runtime_seconds >= 60
        else "—"
    )
    rows = [
        ("⏱ Время", _duration(runtime_seconds)),
        ("⚔️ Боев", battles),
        ("🏆 Побед", wins),
        ("☠️ Поражений", battle.get("defeats", 0)),
        ("📊 Доля побед", win_rate),
        ("✨ Опыт", battle.get("xp", 0)),
        ("⚡ XP в час", xp_per_hour),
        ("💠 Туманная пыль", battle.get("dust", 0)),
        ("💎 Туманные кристаллы", battle.get("crystals", 0)),
        ("🎁 Предметов", drops.get("items", 0)),
        ("🃏 Карт", drops.get("cards", 0)),
        ("👣 Перемещений", state.get("moves", 0)),
        ("🔄 Цикл", f"{state.get('current_cycle', 1)}/{state.get('cycles_count', 1)}"),
    ]
    target_rows = [
        (
            row["target_name"],
            f"{row['wins']} побед · {row['xp']} XP · {row['dust']} пыли · "
            f"{row.get('crystals', 0)} 💎",
        )
        for row in data.get("targets", [])
    ]
    body = rich_notice(notice)
    body += rich_table(rows)
    if target_rows:
        body += (
            "<details><summary>🎯 Результаты по мобам</summary>"
            + rich_table(target_rows, headers=("Моб", "Результат"))
            + "</details>"
        )
    else:
        body += "<details><summary>🎯 Результаты по мобам</summary><p>Боев пока нет.</p></details>"
    if drop_items:
        regular = [row for row in drop_items if not row["is_card"]]
        cards = [row for row in drop_items if row["is_card"]]
        drop_rows = [(row["item_name"], row["quantity"]) for row in regular]
        drop_rows.extend((f"🃏 {row['item_name']}", row["quantity"]) for row in cards)
        body += (
            "<details><summary>🎁 Полученный дроп</summary>"
            + rich_table(drop_rows, headers=("Дроп", "Количество"))
            + "</details>"
        )
    else:
        body += "<details><summary>🎁 Полученный дроп</summary><p>Предметов пока нет.</p></details>"
    if telegram_days:
        telegram_rows = []
        for day in telegram_days:
            incoming = int(day.get("incoming_new_messages", 0)) + int(
                day.get("incoming_message_edits", 0)
            )
            manual_marks = int(day.get("manual_restriction_marks", 0))
            silent_stalls = int(day.get("silent_stalls", 0))
            flood_waits = int(day.get("flood_waits", 0))
            telegram_rows.append(
                (
                    str(day.get("day", "—")),
                    f"↑ {day.get('outgoing_total', 0)} · ↓ {incoming} · "
                    f"пик {day.get('peak_actions_1m', 0)}/мин., "
                    f"{day.get('peak_actions_10m', 0)}/10 мин. · "
                    f"карта {day.get('map_requests', 0)} · "
                    f"восст. {day.get('recovery_attempts', 0)} · "
                    f"ручн. {manual_marks} / авто {silent_stalls} / "
                    f"FLOOD_WAIT {flood_waits}",
                )
            )
        body += (
            "<details open><summary>📡 Telegram по дням (Москва)</summary>"
            + rich_table(telegram_rows, headers=("День", "Нагрузка и признаки"))
            + "</details>"
        )
    else:
        body += (
            "<details open><summary>📡 Telegram по дням (Москва)</summary>"
            "<p>Наблюдения начнут накапливаться после обновления.</p></details>"
        )
    body += rich_button_row(
        rich_button("↻ Обновить", "ui:stats", style="primary"),
        rich_button("⛔ Отметить ограничение", "telegram:mark", style="danger"),
    )
    body += _navigation("stats")
    return rich_document("📈 Статистика сессии", body)


def events_rich(events: list[dict], *, notice: str | None = None) -> str:
    body = rich_notice(notice)
    if events:
        rows = []
        for event in events:
            stamp = str(event["created_at"]).replace("T", " ")[:19]
            rows.append((stamp, f"{event['level']} · {event['message']}"))
        body += rich_table(rows, headers=("Время", "Событие"))
    else:
        body += "<p>Событий пока нет.</p>"
    body += rich_button_row(rich_button("↻ Обновить", "ui:events", style="primary"))
    body += _navigation("events")
    return rich_document("📋 Журнал", body)


def _enabled_targets_summary(settings: SettingsService) -> str:
    selected = settings.values.enabled_targets or []
    if not selected:
        return "<p>Не выбрано ни одной цели.</p>"
    return "<ul>" + "".join(f"<li>{_e(target)}</li>" for target in selected) + "</ul>"


def settings_rich(settings: SettingsService, *, notice: str | None = None) -> str:
    s = settings.values
    body = rich_notice(notice)
    body += rich_table(
        [
            (
                "План",
                f"{s.cycles_count} цикл(а) · "
                f"{s.moves_per_cycle_min}–{s.moves_per_cycle_max} ходов",
            ),
            ("Цели", len(s.enabled_targets or [])),
            ("Порог лечения", f"{s.heal_threshold} HP"),
            ("Перед боем", f"{s.battle_start_hp_percent}% HP"),
            (
                "Боевой движок",
                {
                    "shadow": "тень",
                    "guarded": "осторожный",
                    "active": "активный",
                }.get(s.combat_planner_mode, "тень"),
            ),
            ("Благословение", "включено" if s.blessing_enabled else "выключено"),
            ("Темп", "по заданным задержкам"),
        ],
        headers=None,
    )
    body += rich_button_row(
        rich_button("🗺 План фарма", "settings:farm", style="primary"),
        rich_button("❤️ Бой", "settings:combat", style="primary"),
    )
    body += rich_button_row(
        rich_button("🎯 Цели", "targets:locations"),
        rich_button("⏱ Задержки", "settings:delays"),
    )
    body += (
        "<details><summary>🎯 Активные цели</summary>"
        f"{_enabled_targets_summary(settings)}</details>"
    )
    body += _navigation("settings")
    return rich_document("⚙️ Настройки", body)


def farm_settings_rich(settings: SettingsService, *, notice: str | None = None) -> str:
    s = settings.values
    body = rich_notice(notice)
    body += rich_table(
        [
            ("Количество циклов", s.cycles_count),
            ("Ходов в цикле", f"{s.moves_per_cycle_min}–{s.moves_per_cycle_max}"),
            ("Активных целей", len(s.enabled_targets or [])),
        ],
        headers=None,
    )
    body += rich_button_row(
        rich_button("Изменить циклы", "input:cycles", style="primary"),
        rich_button("Изменить ходы", "input:moves", style="primary"),
    )
    body += rich_button_row(rich_button("🎯 Выбрать мобов", "targets:locations"))
    body += rich_button_row(rich_button("← Настройки", "ui:settings", style="link"))
    return rich_document("🗺 План фарма", body)


def combat_settings_rich(settings: SettingsService, *, notice: str | None = None) -> str:
    s = settings.values
    planner_labels = {
        "shadow": "тень · только анализ",
        "guarded": "осторожный · только доказуемые замены",
        "active": "активный · экспериментальный",
    }
    body = rich_notice(notice)
    body += rich_table(
        [
            ("Порог лечения", f"{s.heal_threshold} HP и ниже"),
            ("HP перед новым боем", f"{s.battle_start_hp_percent}%"),
            ("Благословение", "включено" if s.blessing_enabled else "выключено"),
            ("Резерв маны", "4 для Лечения и Обновления"),
            ("Боевой планировщик", planner_labels.get(s.combat_planner_mode, "тень")),
        ],
        headers=None,
    )
    body += rich_button_row(
        rich_button("❤️ Изменить порог", "input:heal", style="primary"),
        rich_button(
            "✨ Благословение: ВКЛ" if s.blessing_enabled else "✨ Благословение: ВЫКЛ",
            "settings:blessing",
            style="success" if s.blessing_enabled else None,
        ),
    )
    body += rich_button_row(
        rich_button(
            f"🧠 Движок: {planner_labels.get(s.combat_planner_mode, 'тень')}",
            "settings:planner",
            style="success" if s.combat_planner_mode == "active" else "primary",
        )
    )
    body += rich_button_row(
        rich_button(
            "50%",
            "settings:hp:50",
            style="primary" if s.battle_start_hp_percent == 50 else None,
            disabled=s.battle_start_hp_percent == 50,
        ),
        rich_button(
            "100%",
            "settings:hp:100",
            style="primary" if s.battle_start_hp_percent == 100 else None,
            disabled=s.battle_start_hp_percent == 100,
        ),
    )
    body += (
        "<footer>HP перед боем определяет только вход в новый бой; "
        "порог лечения работает внутри боя. Режим «тень» ничего не меняет; "
        "«осторожный» принимает только доказуемо безопасные улучшения.</footer>"
    )
    body += rich_button_row(rich_button("← Настройки", "ui:settings", style="link"))
    return rich_document("❤️ Персонаж и бой", body)


def delay_settings_rich(settings: SettingsService, *, notice: str | None = None) -> str:
    s = settings.values
    rows = [
        ("Перемещение", f"{s.move_delay_min:g}–{s.move_delay_max:g} сек."),
        ("Открытие нападения", f"{s.attack_delay_min:g}–{s.attack_delay_max:g} сек."),
        ("Выбор цели", f"{s.target_delay_min:g}–{s.target_delay_max:g} сек."),
        ("Использование навыка", f"{s.skill_delay_min:g}–{s.skill_delay_max:g} сек."),
        (
            "Короткая пауза",
            f"{s.long_pause_min:g}–{s.long_pause_max:g} сек. · "
            f"{s.long_pause_chance * 100:g}%",
        ),
        (
            "Между циклами",
            f"{s.cycle_rest_min / 60:g}–{s.cycle_rest_max / 60:g} мин.",
        ),
        ("Длительный перерыв", "после 25–40 ходов или 25–45 мин.; 4–8 мин."),
    ]
    body = rich_notice(notice)
    body += rich_table(rows, headers=("Задержка", "Текущее значение"))
    body += rich_button_row(
        rich_button("Перемещение", "input:delay:move_delay"),
        rich_button("Нападение", "input:delay:attack_delay"),
    )
    body += rich_button_row(
        rich_button("Цель", "input:delay:target_delay"),
        rich_button("Навык", "input:delay:skill_delay"),
    )
    body += rich_button_row(
        rich_button("Короткая пауза", "input:delay:long_pause"),
        rich_button("Шанс паузы", "input:chance"),
    )
    body += rich_button_row(rich_button("Между циклами", "input:delay:cycle_rest"))
    body += (
        "<footer>Фактический темп следует указанным диапазонам; Telegram-нагрузка "
        "только записывается и не меняет задержки автоматически.</footer>"
    )
    body += rich_button_row(rich_button("← Настройки", "ui:settings", style="link"))
    return rich_document("⏱ Задержки и темп", body)


def locations_rich(
    locations: Sequence[tuple[str, int, int]],
    *,
    notice: str | None = None,
) -> str:
    body = rich_notice(notice)
    enabled_total = sum(enabled for _, enabled, _ in locations)
    target_total = sum(total for _, _, total in locations)
    configured_locations = sum(enabled > 0 for _, enabled, _ in locations)
    body += rich_table(
        [
            ("Активных целей", f"{enabled_total}/{target_total}"),
            ("Настроено локаций", f"{configured_locations}/{len(locations)}"),
        ],
        headers=None,
    )
    groups = (
        (
            "✅ Выбраны полностью",
            [
                (index, category, enabled, total)
                for index, (category, enabled, total) in enumerate(locations)
                if total > 0 and enabled == total
            ],
            "success",
        ),
        (
            "☑️ Выбраны частично",
            [
                (index, category, enabled, total)
                for index, (category, enabled, total) in enumerate(locations)
                if 0 < enabled < total
            ],
            "primary",
        ),
        (
            "○ Не выбраны",
            [
                (index, category, enabled, total)
                for index, (category, enabled, total) in enumerate(locations)
                if enabled == 0
            ],
            None,
        ),
    )
    for title, group, style in groups:
        if not group:
            continue
        body += f"<h3>{_e(title)}</h3>"
        buttons = [
            rich_button(
                f"{category} · {'все' if total and enabled == total else f'{enabled}/{total}'}",
                f"targets:location:{index}",
                style=style,
            )
            for index, category, enabled, total in group
        ]
        for index in range(0, len(buttons), 2):
            body += rich_button_row(*buttons[index : index + 2])
    body += "<hr/>"
    body += rich_button_row(rich_button("← Настройки", "ui:settings", style="link"))
    return rich_document("🎯 Активные цели", body, subtitle="Настройка по локациям")


def targets_rich(
    category: str,
    targets: Sequence[tuple[str, bool]],
    *,
    category_index: int,
    notice: str | None = None,
) -> str:
    enabled_count = sum(enabled for _, enabled in targets)
    body = rich_notice(notice)
    body += rich_table(
        [("Выбрано", f"{enabled_count}/{len(targets)}")],
        headers=None,
    )
    all_enabled = bool(targets) and enabled_count == len(targets)
    body += rich_button_row(
        rich_button(
            "Снять всех" if all_enabled else "Выбрать всех",
            f"targets:all:{category_index}:{0 if all_enabled else 1}",
            style="danger" if all_enabled else "success",
        )
    )
    grouped_targets = (
        (
            "✅ Активные",
            [
                (target_index, name)
                for target_index, (name, enabled) in enumerate(targets)
                if enabled
            ],
            "success",
        ),
        (
            "○ Не выбраны",
            [
                (target_index, name)
                for target_index, (name, enabled) in enumerate(targets)
                if not enabled
            ],
            None,
        ),
    )
    for title, group, style in grouped_targets:
        if not group:
            continue
        body += f"<h3>{_e(title)}</h3>"
        buttons = [
            rich_button(
                name,
                f"targets:one:{category_index}:{target_index}",
                style=style,
            )
            for target_index, name in group
        ]
        for index in range(0, len(buttons), 2):
            body += rich_button_row(*buttons[index : index + 2])
    body += "<hr/>"
    body += rich_button_row(rich_button("← Локации", "targets:locations", style="link"))
    return rich_document(f"🎯 {category}", body)


def input_prompt_rich(
    title: str,
    instruction: str,
    *,
    current_value: object,
    error: str | None = None,
) -> str:
    body = rich_notice(error, error=bool(error))
    body += rich_table([("Сейчас", current_value)], headers=None)
    body += f"<aside>{_e(instruction)}</aside>"
    body += rich_button_row(rich_button("Отменить", "input:cancel", style="danger"))
    body += "<footer>Отправьте новое значение обычным сообщением.</footer>"
    return rich_document(title, body)


def notification_rich(
    title: str,
    rows: list[tuple[object, object]] | None = None,
    text: str | None = None,
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
    reply_markup: ReplyMarkup | None = None,
    fallback_reply_markup: ReplyMarkup | None = None,
    disable_notification: bool | None = None,
) -> Message:
    """Отправляет RichMessage и откатывается на обычное сообщение при отказе API."""
    try:
        return await bot.send_rich_message(
            chat_id=chat_id,
            rich_message=InputRichMessage(html=html, skip_entity_detection=True),
            reply_markup=reply_markup,
            disable_notification=disable_notification,
        )
    except (TelegramBadRequest, TelegramNetworkError) as error:
        logger.warning("RichMessage недоступен, использован fallback: %s", error)
        return await bot.send_message(
            chat_id=chat_id,
            text=fallback_text,
            reply_markup=fallback_reply_markup or reply_markup,
            disable_notification=disable_notification,
        )


async def edit_rich_with_fallback(
    bot: Bot,
    *,
    chat_id: int,
    message_id: int,
    html: str,
    fallback_text: str,
    fallback_reply_markup: InlineKeyboardMarkup | None = None,
) -> Message | bool | None:
    """Редактирует панель или заменяет её, если Telegram запретил редактирование."""

    def is_uneditable(error: TelegramBadRequest) -> bool:
        error_text = str(error).casefold()
        return any(
            marker in error_text
            for marker in (
                "message can't be edited",
                "message to edit not found",
            )
        )

    async def replace_panel(error: TelegramBadRequest) -> Message:
        logger.warning(
            "Панель %s нельзя редактировать; отправляю замену: %s",
            message_id,
            error,
        )
        replacement = await send_rich_with_fallback(
            bot,
            chat_id=chat_id,
            html=html,
            fallback_text=fallback_text,
            reply_markup=ReplyKeyboardRemove(),
            fallback_reply_markup=fallback_reply_markup,
        )
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramBadRequest as delete_error:
            logger.warning(
                "Старая панель %s осталась в чате: %s",
                message_id,
                delete_error,
            )
        return replacement

    try:
        return await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            rich_message=InputRichMessage(html=html, skip_entity_detection=True),
        )
    except TelegramBadRequest as error:
        if "message is not modified" in str(error).casefold():
            return None
        if is_uneditable(error):
            return await replace_panel(error)
        logger.warning("RichMessage нельзя отредактировать, использован fallback: %s", error)
        try:
            return await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=fallback_text,
                reply_markup=fallback_reply_markup,
            )
        except TelegramBadRequest as fallback_error:
            if "message is not modified" in str(fallback_error).casefold():
                return None
            if not is_uneditable(fallback_error):
                raise
            return await replace_panel(fallback_error)
