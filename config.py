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
APP_DIR = Path("/app")
DATA_DIR = Path("/app/data")
DB_DIR = DATA_DIR / "db_farmer"
DATABASE_PATH = DB_DIR / "fog_farmer.sqlite3"
SESSION_DIR = DATA_DIR / "telegram"
SESSION_NAME = str(SESSION_DIR / "game_reader")
LOG_DIRECTORY = str(DATA_DIR / "logs")
LOG_FILENAME = "farmer.log"

GAME_BOT = "@fogmmobot"
CHARACTER_NAME = "Kombat"

# Категории мобов одновременно описывают доступные локации и порядок
# приоритета нападения внутри каждой локации.
#
# Порядок категорий определяет порядок отображения в панели.
# Порядок мобов внутри категории определяет приоритет выбора цели.
TARGET_MONSTER_CATEGORIES: dict[str, list[str]] = {
    "Поляна": [
        "Бабочка-туманница",
        "Золотой бронзовик",
        "Поганка",
        "Бронзовик",
        "Крапива-жгучка",
        "Кузнечик-прыгун",
        "Клоп-солдатик",
        "Улитка-слизняк",
        "Гусеница-обжора",
        "Жужелица-охотник",
    ],
    "Предлес": [
        "Королевский слизень",
        "Белая лиса",
        "Слизень-кислотник",
        "Мухомор-споровик",
        "Оса-разведчица",
        "Мышь-полевка",
        "Стрекоза-лезвие",
        "Паук-охотник",
        "Оса-рабочая",
        "Оса-страж",
        "Живой мох",
        "Жук-щитоносец",
        "Лиса-сорванец",
    ],
    "Песчаная Кромка": [
        "Лазуритовый скарабей",
        "Богомол альбинос",
        "Богомол-пескожвал",
        "Сколопендра",
        "Ящерка-песчанка",
        "Колючник-сухоцвет",
    ],
}

# Мобы, которые могут самостоятельно инициировать бой.
# Механика внезапного нападения универсальна; набор нужен для описания
# особенностей локаций, статистики и дальнейших стратегий.
AGGRESSIVE_MONSTERS: frozenset[str] = frozenset(
    {
        "Лиса-сорванец",
        "Богомол-пескожвал",
    }
)

# Совместимый плоский список для существующей логики фармера.
DEFAULT_TARGET_MONSTERS: list[str] = [
    monster
    for monsters in TARGET_MONSTER_CATEGORIES.values()
    for monster in monsters
]

MAP_MIN_X = 0
MAP_MAX_X = 8
MAP_MIN_Y = 0
MAP_MAX_Y = 8

DEFAULT_CYCLES_COUNT = 1
DEFAULT_MOVES_PER_CYCLE = 80

DEFAULT_MAX_HP = 325
DEFAULT_MAX_MANA = 11
DEFAULT_HEAL_AMOUNT = 102

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

WATCHDOG_CHECK_INTERVAL = 5
MOVE_PROGRESS_TIMEOUT = 30
TARGET_SELECTION_TIMEOUT = 30
COMBAT_PROGRESS_TIMEOUT = 45
GENERAL_PROGRESS_TIMEOUT = 120
RECOVERY_WATCHDOG_TIMEOUT = 660
MAX_RECOVERY_ATTEMPTS = 3
STATE_HISTORY_LIMIT = 100

DEATH_RECOVERY_MIN_WAIT = 120
DEATH_RECOVERY_RECHECK_INTERVAL = 60
DEATH_RECOVERY_MAX_WAIT = 600
MIN_HP_AFTER_DEATH = 250

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
