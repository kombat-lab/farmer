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
DEFAULT_MOVES_PER_CYCLE_MIN = 80
DEFAULT_MOVES_PER_CYCLE_MAX = 120
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

# Длительные перерывы выполняются только на пустой карте. Условие срабатывает
# по первому из двух ограничений: перемещениям или активному времени.
ACTIVITY_BREAK_MOVES_MIN = 25
ACTIVITY_BREAK_MOVES_MAX = 40
ACTIVITY_BREAK_WORK_MIN = 25 * 60.0
ACTIVITY_BREAK_WORK_MAX = 45 * 60.0
ACTIVITY_BREAK_DURATION_MIN = 4 * 60.0
ACTIVITY_BREAK_DURATION_MAX = 8 * 60.0

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
# Лимиты являются локальной политикой Farmer, а не заявленными лимитами
# Telegram. Длинное окно не позволяет накопить слишком плотную серию запросов.
TELEGRAM_ACTION_MIN_INTERVAL = 1.0
TELEGRAM_ACTION_LIMITS = ((12, 60.0), (70, 600.0))
TELEGRAM_RECOVERY_LIMIT = 3
TELEGRAM_RECOVERY_WINDOW = 10 * 60.0

# Ограничение Telegram никогда не останавливает фармер. Серверное ожидание
# дополняется запасом, а повторные инциденты увеличивают минимальную паузу.
TELEGRAM_FLOOD_WAIT_BUFFER = 2.0
TELEGRAM_FLOOD_INCIDENT_WINDOW = 10 * 60.0
TELEGRAM_FLOOD_BACKOFF_BASE = 15.0
TELEGRAM_FLOOD_BACKOFF_MAX = 5 * 60.0
TELEGRAM_CALLBACK_TIMEOUT_BASE = 30.0
TELEGRAM_CALLBACK_TIMEOUT_MAX = 5 * 60.0

# Автоматический темп плавно масштабирует пользовательские диапазоны.
TELEGRAM_PACING_MIN_FACTOR = 0.90
TELEGRAM_PACING_MAX_FACTOR = 1.50
TELEGRAM_PACING_ADJUST_INTERVAL = 3 * 60.0
TELEGRAM_PACING_ACCELERATION_LOCK = 30 * 60.0
TELEGRAM_PACING_SOFT_1M = 9
TELEGRAM_PACING_HARD_1M = 11
TELEGRAM_PACING_SOFT_10M = 60
TELEGRAM_PACING_HARD_10M = 66

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
