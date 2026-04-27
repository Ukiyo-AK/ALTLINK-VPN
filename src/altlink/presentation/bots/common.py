from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from decimal import Decimal

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from altlink.utils.heartbeat import touch_heartbeat

logger = logging.getLogger(__name__)


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


async def send_telegram_messages(
    *,
    bot_token: str,
    chat_ids: Iterable[int],
    text: str,
    reply_markup=None,
) -> int:
    unique_chat_ids = [int(item) for item in dict.fromkeys(int(chat_id) for chat_id in chat_ids if int(chat_id) > 0)]
    if not bot_token or not unique_chat_ids:
        return 0

    bot = Bot(token=bot_token)
    sent = 0
    try:
        for chat_id in unique_chat_ids:
            try:
                await bot.send_message(chat_id, text, reply_markup=reply_markup)
                sent += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to send Telegram message to chat_id=%s: %s", chat_id, exc)
    finally:
        await bot.session.close()
    return sent
