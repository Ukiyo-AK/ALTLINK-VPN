from __future__ import annotations

import asyncio
import sys

import uvicorn

from altlink.logging_config import configure_logging
from altlink.presentation.bots.admin_app import run_admin_bot
from altlink.presentation.bots.client_app import run_client_bot
from altlink.presentation.web.app import create_app
from altlink.scheduler.main import run_scheduler
from altlink.settings import get_settings


def main() -> None:
    settings = get_settings()
    mode = sys.argv[1] if len(sys.argv) > 1 else "backend"
    if mode == "backend":
        configure_logging(getattr(settings, "debug", False), settings=settings, service_name="backend")
        uvicorn.run(
            "altlink.presentation.web.app:create_app",
            factory=True,
            host=settings.backend_host,
            port=settings.backend_port,
            reload=False,
            proxy_headers=True,
            forwarded_allow_ips="*",
            log_config=None,
        )
        return
    if mode == "client-bot":
        asyncio.run(run_client_bot())
        return
    if mode == "admin-bot":
        asyncio.run(run_admin_bot())
        return
    if mode == "scheduler":
        asyncio.run(run_scheduler())
        return
    raise SystemExit(f"Unknown mode: {mode}")


if __name__ == "__main__":
    main()
