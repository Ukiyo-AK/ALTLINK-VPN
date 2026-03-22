from __future__ import annotations

import argparse
import asyncio
import getpass
import time
from pathlib import Path

from altlink.application.services import AdminAuthService, BootstrapService, ServerService
from altlink.core.logging import configure_logging
from altlink.infrastructure.db import models  # noqa: F401
from altlink.infrastructure.db.session import create_engine_and_factory
from altlink.infrastructure.remnawave import RemnawaveClient
from altlink.settings import get_settings


async def seed_defaults() -> None:
    settings = get_settings()
    configure_logging(settings)
    engine, session_factory = create_engine_and_factory(settings)
    remnawave = RemnawaveClient(settings)
    async with session_factory() as session:
        await BootstrapService(session, settings, remnawave).ensure_defaults()
        await session.commit()
    await remnawave.close()
    await engine.dispose()


async def create_admin(username: str | None, telegram_id: int | None, full_name: str | None) -> None:
    settings = get_settings()
    configure_logging(settings)
    engine, session_factory = create_engine_and_factory(settings)
    remnawave = RemnawaveClient(settings)
    username = username or input("Логин администратора: ").strip()
    password = getpass.getpass("Пароль администратора: ")
    async with session_factory() as session:
        service = AdminAuthService(session, settings, remnawave)
        admin = await service.create_or_update_admin(
            username=username,
            password=password,
            telegram_id=telegram_id,
            full_name=full_name,
        )
        await session.commit()
        print(f"Администратор готов: {admin.username}")
    await remnawave.close()
    await engine.dispose()


async def sync_servers() -> None:
    settings = get_settings()
    configure_logging(settings)
    engine, session_factory = create_engine_and_factory(settings)
    remnawave = RemnawaveClient(settings)
    async with session_factory() as session:
        servers = await ServerService(session, settings, remnawave).sync_from_remnawave()
        await session.commit()
        print(f"Синхронизировано серверов: {len(servers)}")
    await remnawave.close()
    await engine.dispose()


def check_heartbeat(path: str, max_age: int) -> int:
    heartbeat = Path(path)
    if not heartbeat.exists():
        return 1
    age = time.time() - heartbeat.stat().st_mtime
    return 0 if age <= max_age else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="ALTLINK CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("seed-defaults")
    create_admin_parser = subparsers.add_parser("create-admin")
    create_admin_parser.add_argument("--username")
    create_admin_parser.add_argument("--telegram-id", type=int)
    create_admin_parser.add_argument("--full-name")

    subparsers.add_parser("sync-servers")

    heartbeat_parser = subparsers.add_parser("check-heartbeat")
    heartbeat_parser.add_argument("--path", required=True)
    heartbeat_parser.add_argument("--max-age", required=True, type=int)

    args = parser.parse_args()

    if args.command == "seed-defaults":
        asyncio.run(seed_defaults())
    elif args.command == "create-admin":
        asyncio.run(create_admin(args.username, args.telegram_id, args.full_name))
    elif args.command == "sync-servers":
        asyncio.run(sync_servers())
    elif args.command == "check-heartbeat":
        raise SystemExit(check_heartbeat(args.path, args.max_age))


if __name__ == "__main__":
    main()
