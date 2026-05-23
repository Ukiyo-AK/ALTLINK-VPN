from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from logging.config import dictConfig
from pathlib import Path


def configure_logging(debug: bool = False, *, settings=None, service_name: str = "app") -> None:
    level = "DEBUG" if debug else "INFO"
    handlers: dict[str, dict] = {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "default",
            "level": level,
        }
    }
    root_handlers = ["console"]

    log_to_file = bool(getattr(settings, "log_to_file", False))
    if log_to_file:
        log_dir = Path(str(getattr(settings, "log_dir", "logs")))
        log_dir.mkdir(parents=True, exist_ok=True)
        max_bytes = int(getattr(settings, "log_file_max_bytes", 5 * 1024 * 1024))
        backup_count = int(getattr(settings, "log_file_backup_count", 5))
        service_slug = str(service_name or "app").strip().replace("/", "-").replace("\\", "-")
        info_path = log_dir / f"{service_slug}.log"
        error_path = log_dir / f"{service_slug}.error.log"
        handlers["file"] = {
            "()": RotatingFileHandler,
            "filename": str(info_path),
            "maxBytes": max_bytes,
            "backupCount": backup_count,
            "encoding": "utf-8",
            "formatter": "default",
            "level": level,
        }
        handlers["error_file"] = {
            "()": RotatingFileHandler,
            "filename": str(error_path),
            "maxBytes": max_bytes,
            "backupCount": backup_count,
            "encoding": "utf-8",
            "formatter": "default",
            "level": "ERROR",
        }
        root_handlers.extend(["file", "error_file"])

    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                }
            },
            "handlers": handlers,
            "root": {"handlers": root_handlers, "level": level},
        }
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.INFO)
