from __future__ import annotations

import logging
from logging.config import dictConfig

from pythonjsonlogger.json import JsonFormatter

from altlink.settings import Settings


class CompactJsonFormatter(JsonFormatter):
    def add_fields(self, log_record: dict, record: logging.LogRecord, message_dict: dict) -> None:
        super().add_fields(log_record, record, message_dict)
        log_record.setdefault("level", record.levelname)
        log_record.setdefault("logger", record.name)


def configure_logging(settings: Settings) -> None:
    formatter_name = "json" if settings.json_logs else "plain"
    formatters: dict[str, dict] = {
        "plain": {
            "format": "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        },
        "json": {
            "()": CompactJsonFormatter,
            "fmt": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
    }
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": formatters,
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "formatter": formatter_name,
                }
            },
            "root": {"level": settings.log_level.upper(), "handlers": ["default"]},
        }
    )

