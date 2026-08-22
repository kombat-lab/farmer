from __future__ import annotations

import os
from pathlib import Path


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задана переменная окружения {name}")
    return value


# Секретные значения — только через переменные окружения.
API_ID = int(_required("TELEGRAM_API_ID"))
API_HASH = _required("TELEGRAM_API_HASH")
CONTROL_BOT_TOKEN = _required("CONTROL_BOT_TOKEN")
ADMIN_TELEGRAM_ID = int(_required("ADMIN_TELEGRAM_ID"))

# Пути контейнера.
DATA_DIR = Path("/app/data")
DB_DIR = DATA_DIR / "db_farmer"
DATABASE_PATH = DB_DIR / "fog_farmer.sqlite3"
SESSION_DIR = DATA_DIR / "telegram"
SESSION_NAME = str(SESSION_DIR / "game_reader")
LOG_DIRECTORY = str(DATA_DIR / "logs")
LOG_FILENAME = "farmer.log"

GAME_BOT = "@fogmmobot"
CHARACTER_NAME = "Kombat"

# Safe initial map bounds. They are replaced with the dimensions parsed from
# the current game message as soon as the farmer receives a map.
MAP_MIN_X = 0
MAP_MAX_X = 8
MAP_MIN_Y = 0
MAP_MAX_Y = 8

DEFAULT_CYCLES_COUNT = 1
DEFAULT_MOVES_PER_CYCLE = 80
DEFAULT_HEAL_THRESHOLD = 325
DEFAULT_BATTLE_START_HP_PERCENT = 100

DEFAULT_MOVE_DELAY_MIN = 2.0
DEFAULT_MOVE_DELAY_MAX = 8.0
DEFAULT_ATTACK_DELAY_MIN = 1.0
DEFAULT_ATTACK_DELAY_MAX = 2.0
DEFAULT_TARGET_DELAY_MIN = 1.0
DEFAULT_TARGET_DELAY_MAX = 2.0
DEFAULT_SKILL_DELAY_MIN = 1.0
DEFAULT_SKILL_DELAY_MAX = 3.0

DEFAULT_LONG_PAUSE_CHANCE = 0.12
DEFAULT_LONG_PAUSE_MIN = 5.0
DEFAULT_LONG_PAUSE_MAX = 10.0
DEFAULT_CYCLE_REST_MIN = 300.0
DEFAULT_CYCLE_REST_MAX = 900.0

ACTIVITY_PROFILE_NORMAL = "normal"
ACTIVITY_PROFILE_FAST = "fast"
DEFAULT_ACTIVITY_PROFILE = ACTIVITY_PROFILE_NORMAL

# Профиль «Обычный» делает длительные перерывы только на пустой карте.
# Условие срабатывает по первому из двух ограничений: перемещениям или
# активному времени. Профиль «Быстрый» эти перерывы отключает.
ACTIVITY_BREAK_MOVES_MIN = 25
ACTIVITY_BREAK_MOVES_MAX = 40
ACTIVITY_BREAK_WORK_MIN = 25 * 60.0
ACTIVITY_BREAK_WORK_MAX = 45 * 60.0
ACTIVITY_BREAK_DURATION_MIN = 4 * 60.0
ACTIVITY_BREAK_DURATION_MAX = 8 * 60.0

FAST_MOVE_DELAY = (0.4, 1.2)
FAST_ATTACK_DELAY = (0.3, 0.8)
FAST_TARGET_DELAY = (0.3, 0.8)
FAST_SKILL_DELAY = (0.4, 1.2)

WATCHDOG_CHECK_INTERVAL = 5
MOVE_PROGRESS_TIMEOUT = 30
TARGET_SELECTION_TIMEOUT = 30
COMBAT_PROGRESS_TIMEOUT = 45
GENERAL_PROGRESS_TIMEOUT = 120
RECOVERY_WATCHDOG_TIMEOUT = 660
MAX_RECOVERY_ATTEMPTS = 3

DEATH_RECOVERY_MIN_WAIT = 120
DEATH_RECOVERY_MAX_WAIT = 600
MIN_HP_AFTER_DEATH = 250

# Независимый от пользовательских задержек предохранитель. Он охватывает
# inline-кнопки и текстовые команды игровому боту одним общим бюджетом.
TELEGRAM_ACTION_MIN_INTERVAL = 1.0
TELEGRAM_ACTION_LIMIT = 20
TELEGRAM_ACTION_WINDOW = 60.0
TELEGRAM_RECOVERY_LIMIT = 3
TELEGRAM_RECOVERY_WINDOW = 10 * 60.0

LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
DATA_RETENTION_DAYS = 7
LOG_RETENTION_DAYS = 7

for directory in (
    DB_DIR,
    SESSION_DIR,
    Path(LOG_DIRECTORY),
):
    directory.mkdir(parents=True, exist_ok=True)
