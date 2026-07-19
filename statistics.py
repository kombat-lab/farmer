from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass

from rewards import BattleReward


@dataclass(frozen=True)
class SessionReport:
    elapsed_seconds: int
    wins: int
    defeats: int
    xp: int
    dust: int
    drops: dict[str, int]
    targets: dict[str, dict[str, int]]
    technical: dict[str, int]


class FarmStatistics:
    """
    Оперативные счётчики для консольного отчёта.
    Постоянное хранение выполняет storage.py в SQLite.
    """
    def __init__(self) -> None:
        self.started_at = time.monotonic()
        self.runtime_finalized = False
        self.session_wins = 0
        self.session_defeats = 0
        self.session_xp = 0
        self.session_dust = 0
        self.session_drops: Counter[str] = Counter()
        self.session_targets: dict[str, Counter[str]] = {}
        self.session_technical: Counter[str] = Counter()
        self._battle_ids: set[int] = set()
        self._target_events: set[str] = set()

    def elapsed_seconds(self) -> int:
        return int(time.monotonic() - self.started_at)

    def record_target_list(self, event_key, target_counts):
        if event_key in self._target_events:
            return False
        self._target_events.add(event_key)
        for name, (found, occupied) in target_counts.items():
            c = self.session_targets.setdefault(name, Counter())
            c["found"] += found
            c["occupied"] += occupied
            c["free"] += max(0, found - occupied)
        return True

    def record_attacked(self, target_name):
        self.session_targets.setdefault(target_name, Counter())["attacked"] += 1

    def add_victory(self, message_id, target_name, reward: BattleReward):
        if message_id in self._battle_ids:
            return False
        self._battle_ids.add(message_id)
        self.session_wins += 1
        self.session_xp += reward.xp
        self.session_dust += reward.dust
        self.session_drops.update(reward.items)
        self.session_targets.setdefault(target_name, Counter())["killed"] += 1
        return True

    def add_defeat(self, message_id, target_name):
        if message_id in self._battle_ids:
            return False
        self._battle_ids.add(message_id)
        self.session_defeats += 1
        self.session_targets.setdefault(target_name, Counter())["defeated"] += 1
        return True

    def record_watchdog_trigger(self): self.session_technical["watchdog_triggers"] += 1
    def record_recovery_attempt(self): self.session_technical["recovery_attempts"] += 1
    def record_successful_recovery(self): self.session_technical["successful_recoveries"] += 1
    def record_startup_state_recovery(self): self.session_technical["startup_state_recoveries"] += 1
    def finalize_runtime(self): self.runtime_finalized = True

    def session_report(self):
        return SessionReport(
            self.elapsed_seconds(), self.session_wins, self.session_defeats,
            self.session_xp, self.session_dust, dict(self.session_drops),
            {k: dict(v) for k, v in self.session_targets.items()},
            dict(self.session_technical),
        )

    def total_report(self):
        return self.session_report()


def format_duration(seconds: int) -> str:
    h, r = divmod(max(0, seconds), 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def format_report(title: str, report: SessionReport) -> str:
    lines = [
        "=" * 72, title,
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
