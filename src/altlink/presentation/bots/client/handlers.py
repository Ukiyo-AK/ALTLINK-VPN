from __future__ import annotations

from decimal import Decimal, InvalidOperation
from io import BytesIO

import qrcode
from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, BufferedInputFile, Message
from sqlalchemy import desc, select

from altlink.application.services import AccountService, BillingService, ServerService, SubscriptionService
from altlink.infrastructure.db.models import Server, TopupRequest, User
from altlink.presentation.bots.common.context import BotContext, open_session
from altlink.presentation.bots.common.formatters import profile_text, subscription_text
from altlink.presentation.bots.common.keyboards import (
    client_main_keyboard,
    client_profile_inline,
    client_server_detail_inline,
    client_servers_inline,
    client_subscription_inline,
    client_topup_list_inline,
)

router = Router()


class TopupStates(StatesGroup):
    waiting_amount = State()


async def _ensure_user(message: Message, bot_context: BotContext) -> User:
    async with open_session(bot_context) as session:
        service = AccountService(session, bot_context.settings, bot_context.remnawave)
        user = await service.register_or_update_telegram_user(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
            language_code=message.from_user.language_code,
        )
        return user


async def _profile_summary(message: Message, bot_context: BotContext) -> dict:
    async with open_session(bot_context) as session:
        service = AccountService(session, bot_context.settings, bot_context.remnawave)
        user = await service.get_user_by_telegram_id(message.from_user.id)
        return await service.get_profile_summary(user)


@router.message(CommandStart())
async def start(message: Message, bot_context: BotContext) -> None:
    await _ensure_user(message, bot_context)
    await message.answer(
        "Добро пожаловать в ALTLINK.\n\n"
        "Здесь можно управлять VPN без команд: смотреть статус подписки, баланс, пополнения, доступные серверы и получать QR-коды.",
        reply_markup=client_main_keyboard(),
    )
    summary = await _profile_summary(message, bot_context)
    await message.answer(profile_text(summary), reply_markup=client_profile_inline())


@router.message(F.text == "Профиль")
async def profile(message: Message, bot_context: BotContext) -> None:
    await _ensure_user(message, bot_context)
    summary = await _profile_summary(message, bot_context)
    await message.answer(profile_text(summary), reply_markup=client_profile_inline())


@router.message(F.text == "Подписка")
async def subscription(message: Message, bot_context: BotContext) -> None:
    await _ensure_user(message, bot_context)
    summary = await _profile_summary(message, bot_context)
    await message.answer(subscription_text(summary), reply_markup=client_subscription_inline())


@router.message(F.text == "Баланс")
async def balance(message: Message, bot_context: BotContext) -> None:
    summary = await _profile_summary(message, bot_context)
    user = summary["user"]
    await message.answer(
        f"Ваш баланс: {user.balance_rub:.2f} ₽.\n"
        "Вы можете создать заявку на пополнение, а затем администратор подтвердит её после получения перевода.",
        reply_markup=client_profile_inline(),
    )


@router.message(F.text == "Серверы")
async def servers(message: Message, bot_context: BotContext) -> None:
    await _ensure_user(message, bot_context)
    async with open_session(bot_context) as session:
        account = AccountService(session, bot_context.settings, bot_context.remnawave)
        user = await account.get_user_by_telegram_id(message.from_user.id)
        server_items = [
            (access.server_id, access.server.name)
            for access in user.server_accesses
            if access.server and access.status.value == "active"
        ]
        if not server_items:
            await message.answer("Сейчас у вас нет активных серверов. Активируйте тест или тариф.")
            return
        await message.answer("Доступные серверы:", reply_markup=client_servers_inline(server_items))


@router.message(F.text == "Помощь")
async def help_screen(message: Message) -> None:
    await message.answer(
        "Как пользоваться:\n"
        "1. Откройте профиль и получите тестовый период.\n"
        "2. При необходимости создайте заявку на пополнение.\n"
        "3. В разделе «Подписка» выберите тариф.\n"
        "4. В разделе «Серверы» можно открыть ссылку или QR-код конфигурации.\n\n"
        "Если что-то не получается, напишите администратору."
    )


@router.callback_query(F.data == "client:subscription")
async def cb_subscription(callback: CallbackQuery, bot_context: BotContext) -> None:
    summary = await _profile_summary(callback.message, bot_context)
    await callback.message.answer(subscription_text(summary), reply_markup=client_subscription_inline())
    await callback.answer()


@router.callback_query(F.data == "client:trial")
async def cb_trial(callback: CallbackQuery, bot_context: BotContext) -> None:
    async with open_session(bot_context) as session:
        account = AccountService(session, bot_context.settings, bot_context.remnawave)
        user = await account.get_user_by_telegram_id(callback.from_user.id)
        try:
            await SubscriptionService(session, bot_context.settings, bot_context.remnawave).activate_trial(user)
            text = "Тестовый период на 2 дня успешно активирован."
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
    await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data.startswith("client:plan:"))
async def cb_activate_plan(callback: CallbackQuery, bot_context: BotContext) -> None:
    plan_code = callback.data.split(":")[-1]
    async with open_session(bot_context) as session:
        account = AccountService(session, bot_context.settings, bot_context.remnawave)
        user = await account.get_user_by_telegram_id(callback.from_user.id)
        try:
            await SubscriptionService(session, bot_context.settings, bot_context.remnawave).activate_paid_plan(user, plan_code)
            text = "Тариф успешно активирован."
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
    await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data == "client:topup:create")
async def cb_topup_create(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TopupStates.waiting_amount)
    await callback.message.answer(
        "Введите сумму пополнения в рублях одним сообщением.\n"
        "Например: 300"
    )
    await callback.answer()


@router.message(TopupStates.waiting_amount)
async def topup_amount(message: Message, state: FSMContext, bot_context: BotContext) -> None:
    try:
        amount = Decimal(message.text.replace(",", "."))
        if amount <= 0:
            raise InvalidOperation
    except Exception:  # noqa: BLE001
        await message.answer("Не удалось распознать сумму. Отправьте положительное число, например 300.")
        return
    async with open_session(bot_context) as session:
        account = AccountService(session, bot_context.settings, bot_context.remnawave)
        user = await account.get_user_by_telegram_id(message.from_user.id)
        topup = await BillingService(session, bot_context.settings, bot_context.remnawave).create_topup_request(
            user, amount, comment="Создано через клиентский бот"
        )
    await state.clear()
    await message.answer(
        f"Заявка создана.\nНомер: {topup.id}\nСумма: {topup.amount_rub:.2f} ₽.\n\n"
        "Переведите деньги администратору и дождитесь подтверждения."
    )


@router.callback_query(F.data == "client:topup:list")
async def cb_topup_list(callback: CallbackQuery, bot_context: BotContext) -> None:
    async with open_session(bot_context) as session:
        account = AccountService(session, bot_context.settings, bot_context.remnawave)
        user = await account.get_user_by_telegram_id(callback.from_user.id)
        topups = (
            await session.execute(
                select(TopupRequest)
                .where(TopupRequest.user_id == user.id)
                .order_by(desc(TopupRequest.created_at))
                .limit(10)
            )
        ).scalars().all()
        if not topups:
            await callback.message.answer("У вас ещё нет заявок на пополнение.")
        else:
            items = [(item.id, f"{item.amount_rub:.2f} ₽ • {item.status.value}") for item in topups]
            await callback.message.answer("Ваши заявки:", reply_markup=client_topup_list_inline(items))
    await callback.answer()


@router.callback_query(F.data.startswith("client:topup:item:"))
async def cb_topup_item(callback: CallbackQuery, bot_context: BotContext) -> None:
    topup_id = callback.data.split(":")[-1]
    async with open_session(bot_context) as session:
        topup = await session.get(TopupRequest, topup_id)
        if topup is None:
            text = "Заявка не найдена."
        else:
            text = (
                f"Заявка {topup.id}\n"
                f"Сумма: {topup.amount_rub:.2f} ₽\n"
                f"Статус: {topup.status.value}\n"
                f"Создана: {topup.created_at:%d.%m.%Y %H:%M}"
            )
    await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data == "client:link")
async def cb_global_link(callback: CallbackQuery, bot_context: BotContext) -> None:
    async with open_session(bot_context) as session:
        account = AccountService(session, bot_context.settings, bot_context.remnawave)
        user = await account.get_user_by_telegram_id(callback.from_user.id)
        if not user.remnawave_subscription_url:
            text = "Официальная подписка пока недоступна. Сначала активируйте тест или платный тариф."
        else:
            text = (
                "Официальная ссылка подписки Remnawave:\n"
                f"{user.remnawave_subscription_url}\n\n"
                "Эта ссылка содержит все активные серверы, подключенные к вашей подписке."
            )
    await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data.startswith("client:server:"))
async def cb_server_detail(callback: CallbackQuery, bot_context: BotContext) -> None:
    server_id = callback.data.split(":")[-1]
    async with open_session(bot_context) as session:
        server = await session.get(Server, server_id)
        if server is None:
            text = "Сервер не найден."
        else:
            text = (
                f"{server.name}\n"
                f"Страна: {server.country_code or '—'}\n"
                f"Состояние: {'online' if server.is_connected else 'offline'}\n"
                f"Нагрузка: {server.load_percent}%\n"
                f"Клиенты: {server.current_clients_count}/{server.max_clients_count}\n\n"
                "Отдельная серверная ссылка официальным API не выдается, поэтому используется общая подписка Remnawave."
            )
    if server:
        await callback.message.answer(text, reply_markup=client_server_detail_inline(server_id))
    else:
        await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data.startswith("client:serverlink:"))
async def cb_server_link(callback: CallbackQuery, bot_context: BotContext) -> None:
    async with open_session(bot_context) as session:
        account = AccountService(session, bot_context.settings, bot_context.remnawave)
        user = await account.get_user_by_telegram_id(callback.from_user.id)
        text = (
            "Официальная ссылка подписки для этого сервера:\n"
            f"{user.remnawave_subscription_url or 'Недоступно'}\n\n"
            "Remnawave выдает общую подписку со всеми активными серверами."
        )
    await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data.startswith("client:serverqr:"))
async def cb_server_qr(callback: CallbackQuery, bot_context: BotContext) -> None:
    async with open_session(bot_context) as session:
        account = AccountService(session, bot_context.settings, bot_context.remnawave)
        user = await account.get_user_by_telegram_id(callback.from_user.id)
        link = user.remnawave_subscription_url
    if not link:
        await callback.message.answer("Ссылка пока недоступна. Активируйте тест или платный тариф.")
        await callback.answer()
        return
    qr = qrcode.make(link)
    buffer = BytesIO()
    qr.save(buffer, format="PNG")
    buffer.seek(0)
    await callback.message.answer_photo(
        BufferedInputFile(buffer.getvalue(), filename="altlink-subscription.png"),
        caption="QR-код для официальной подписки Remnawave.",
    )
    await callback.answer()
