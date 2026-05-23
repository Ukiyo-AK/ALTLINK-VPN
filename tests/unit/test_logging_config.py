from __future__ import annotations

import logging
from types import SimpleNamespace

from altlink.logging_config import configure_logging


def test_configure_logging_writes_rotating_service_files(tmp_path):
    log_dir = tmp_path / "logs"
    settings = SimpleNamespace(
        log_to_file=True,
        log_dir=str(log_dir),
        log_file_max_bytes=100_000,
        log_file_backup_count=2,
    )

    configure_logging(False, settings=settings, service_name="client-bot")
    logger = logging.getLogger("altlink.tests.logging")
    logger.info("info entry")
    logger.error("error entry")

    for handler in logging.getLogger().handlers:
        handler.flush()

    info_log = (log_dir / "client-bot.log").read_text(encoding="utf-8")
    error_log = (log_dir / "client-bot.error.log").read_text(encoding="utf-8")

    assert "info entry" in info_log
    assert "error entry" in info_log
    assert "error entry" in error_log
    assert "info entry" not in error_log
