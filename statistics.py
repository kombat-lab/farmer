from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass

from rewards import BattleReward, parse_item_stack


@dataclass(frozen=True)
class SessionReport:
    elapsed_seconds: int
    wins: int
    defeats: int
    xp: int
    dust: int
    drops: dict[str, int]


class FarmStatistics:
    """
    Оперативные счётчики для консольного отчёта.
    Постоянное хранение выполняет storage.py в SQLite.
    """

    def __init__(self) -> None:
        self.started_at = time.monotonic()
        self.session_wins = 0
        self.session_defeats = 0
        self.session_xp = 0
        self.session_dust = 0
        self.session_drops: Counter[str] = Counter()
        self._battle_ids: set[int] = set()

    def elapsed_seconds(self) -> int:
        return int(time.monotonic() - self.started_at)

    def add_victory(self, message_id, reward: BattleReward):
        if message_id in self._battle_ids:
            return False
        self._battle_ids.add(message_id)
        self.session_wins += 1
        self.session_xp += reward.xp
        self.session_dust += reward.dust
        for item in reward.items:
            name, quantity = parse_item_stack(item)
            self.session_drops[name] += quantity
        return True

    def add_defeat(self, message_id):
        if message_id in self._battle_ids:
            return False
        self._battle_ids.add(message_id)
        self.session_defeats += 1
        return True

    def session_report(self):
        return SessionReport(
            self.elapsed_seconds(),
            self.session_wins,
            self.session_defeats,
            self.session_xp,
            self.session_dust,
            dict(self.session_drops),
        )

def format_duration(seconds: int) -> str:
    h, r = divmod(max(0, seconds), 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def format_report(title: str, report: SessionReport) -> str:
    lines = [
        "=" * 72,
        title,
        f"Затрачено времени: {format_duration(report.elapsed_seconds)}",
        f"Побед: {report.wins}",
        f"Поражений: {report.defeats}",
        f"Получено опыта: {report.xp}",
        f"Получено Туманной пыли: {report.dust}",
        "Дроп:",
    ]
    if report.drops:
        for name, count in sorted(report.drops.items(), key=lambda x: (-x[1], x[0])):
            lines.append(f"  {name}: {count}")
    else:
        lines.append("  нет")
    lines.append("=" * 72)
    return "\n".join(lines)
