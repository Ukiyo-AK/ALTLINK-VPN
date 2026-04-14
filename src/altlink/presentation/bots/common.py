from __future__ import annotations

import asyncio
from decimal import Decimal

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from altlink.utils.heartbeat import touch_heartbeat


def money(value: Decimal | int | float) -> str:
    return f"{Decimal(value):.2f} ₽"


def nav_keyboard(*rows: tuple[str, str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for text, callback_data in rows:
        builder.add(InlineKeyboardButton(text=text, callback_data=callback_data))
    builder.adjust(1)
    return builder.as_markup()


async def heartbeat_loop(path: str, delay: int = 30) -> None:
    while True:
        touch_heartbeat(path)
        await asyncio.sleep(delay)

