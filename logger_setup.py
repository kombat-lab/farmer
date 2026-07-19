from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(
    *,
    log_directory: str = "logs",
    log_filename: str = "farmer.log",
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
    level: int = logging.INFO,
) -> logging.Logger:
    """
    Настраивает единый журнал:

    - вывод в консоль;
    - запись в UTF-8 файл;
    - автоматическая ротация по размеру.

    При 5 МБ и backup_count=5 будут храниться:
    farmer.log, farmer.log.1 ... farmer.log.5.
    """
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(
            encoding="utf-8",
            errors="replace",
        )

    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(
            encoding="utf-8",
            errors="replace",
        )

    log_path = Path(log_directory)
    log_path.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("fog_farmer")
    logger.setLevel(level)
    logger.propagate = False

    # Не добавляем обработчики повторно при повторном импорте.
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        filename=log_path / log_filename,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    return logger
