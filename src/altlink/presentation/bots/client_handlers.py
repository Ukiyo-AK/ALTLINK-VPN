from __future__ import annotations

from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from altlink.application.services.base import ConflictError
from altlink.application.services.registry import AppContainer
from altlink.domain.enums import PlanCode
from altlink.presentation.bots.client_keyboards import (
    balance_actions,
    main_menu,
    plan_actions,
    subscription_actions,
    topup_actions,
)
from altlink.utils.qr import render_qr_png

router = Router(name="client-router")


class TopupStates(StatesGroup):
    waiting_for_amount = State()


async def ensure_user(telegram_user, container: AppContainer):
    async with container.hub() as hub:
        return await hub.accounts.get_or_create_user(
            telegram_id=telegram_user.id,
            username=telegram_user.username,
            first_name=telegram_user.first_name,
            last_name=telegram_user.last_name,
            language_code=telegram_user.language_code,
        )


async def profile_text(user, subscription) -> str:
    plan_name = subscription.plan.name if subscription and subscription.plan else "не выбран"
    next_billing = subscription.next_billing_at if subscription else "—"
    debt = Decimal("0")
    if subscription and subscription.plan:
        debt = max(Decimal(subscription.plan.price_rub) - Decimal(user.balance_rub), Decimal("0"))
    return (
        f"Профиль\n\n"
        f"Telegram ID: {user.telegram_id}\n"
        f"Статус: {user.status}\n"
        f"Баланс: {Decimal(user.balance_rub):.2f} ₽\n"
        f"Тариф: {plan_name}\n"
        f"Следующее продление: {next_billing}\n"
        f"Задолженность: {debt:.2f} ₽"
    )


async def subscription_text(bundle: dict) -> str:
    user = bundle["user"]
    subscription = bundle.get("subscription")
    if not subscription:
        return (
            "Подписка пока не активирована.\n\n"
            "Выберите тариф или запустите тестовый период."
        )
    accessible_nodes = bundle.get("accessible_nodes") or []
    nodes_text = "\n".join([f"• {node.nodeName} ({node.countryCode})" for node in accessible_nodes]) or "Пока пусто"
    return (
        f"Моя подписка\n\n"
        f"Статус: {user.status}\n"
        f"Тариф: {subscription.plan.name}\n"
        f"Следующее продление: {subscription.next_billing_at}\n"
        f"Трафик: {subscription.traffic_used_bytes}\n"
        f"Доступные серверы:\n{nodes_text}"
    )


@router.message(CommandStart())
async def start(message: Message, container: AppContainer):
    async with container.hub() as hub:
        user = await hub.accounts.get_or_create_user(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
            language_code=message.from_user.language_code,
        )
        subscription = await hub.accounts.get_current_subscription(user.id)
        text = (
            "Добро пожаловать в ALTLINK.\n\n"
            "Здесь можно активировать тестовый период, выбрать тариф, пополнить баланс, "
            "получить ссылку и QR для подключения, а также посмотреть статус подписки."
        )
        if subscription:
            text += f"\n\nСейчас у вас активен тариф: {subscription.plan.name}"
    await message.answer(text, reply_markup=main_menu())


@router.message(F.text == "Профиль")
async def profile(message: Message, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_user(message.from_user, container)
        subscription = await hub.accounts.get_current_subscription(user.id)
    await message.answer(await profile_text(user, subscription), reply_markup=main_menu())


@router.message(F.text == "Баланс")
async def balance(message: Message, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_user(message.from_user, container)
        requests = await hub.topups.list_requests(user_id=user.id)
    await message.answer(
        f"Баланс: {Decimal(user.balance_rub):.2f} ₽\n\n"
        f"Заявок на пополнение: {len(requests)}",
        reply_markup=balance_actions().as_markup(),
    )


@router.message(F.text == "Подписка")
async def subscription(message: Message, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_user(message.from_user, container)
        bundle = await hub.accounts.get_subscription_bundle(user.id)
    await message.answer(await subscription_text(bundle), reply_markup=subscription_actions().as_markup())


@router.message(F.text == "Серверы")
async def servers(message: Message, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_user(message.from_user, container)
        bundle = await hub.accounts.get_subscription_bundle(user.id)
    accessible_nodes = bundle.get("accessible_nodes") or []
    text = "Мои серверы\n\n"
    if not accessible_nodes:
        text += "Пока нет активных серверов. Если тариф уже активен, попробуйте позже."
    else:
        for node in accessible_nodes:
            text += f"• {node.nodeName} ({node.countryCode})\n"
    text += "\nОбщая ссылка и QR доступны в разделе «Подписка»."
    await message.answer(text)


@router.message(F.text == "Помощь")
async def help_screen(message: Message):
    await message.answer(
        "Помощь\n\n"
        "1. Тестовый период доступен один раз на 2 дня.\n"
        "2. Пополнение баланса происходит через заявку в боте и ручное подтверждение администратором.\n"
        "3. Все активные серверы доступны одновременно через общую подписочную ссылку Remnawave.\n"
        "4. Если денег на продление не хватит, включится grace period на 14 дней."
    )


@router.callback_query(F.data == "client:topup_menu")
async def topup_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "Выберите сумму пополнения или укажите свою.",
        reply_markup=topup_actions().as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("client:topup_amount:"))
async def topup_amount(callback: CallbackQuery, container: AppContainer):
    amount = Decimal(callback.data.split(":")[-1])
    async with container.hub() as hub:
        user = await ensure_user(callback.from_user, container)
        request = await hub.topups.create_request(user.id, amount)
    await callback.message.edit_text(
        f"Заявка на пополнение {amount:.2f} ₽ создана.\n"
        f"После перевода средств администратор подтвердит её вручную.\n\n"
        f"Номер заявки: {request.id}"
    )
    await callback.answer()


@router.callback_query(F.data == "client:topup_custom")
async def topup_custom(callback: CallbackQuery, state: FSMContext):
    await state.set_state(TopupStates.waiting_for_amount)
    await callback.message.answer("Введите сумму пополнения в рублях, например: 350")
    await callback.answer()


@router.message(TopupStates.waiting_for_amount)
async def topup_custom_value(message: Message, state: FSMContext, container: AppContainer):
    try:
        amount = Decimal(message.text.replace(",", "."))
    except (InvalidOperation, AttributeError):
        await message.answer("Не удалось распознать сумму. Попробуйте ещё раз.")
        return
    async with container.hub() as hub:
        user = await ensure_user(message.from_user, container)
        request = await hub.topups.create_request(user.id, amount)
    await state.clear()
    await message.answer(
        f"Заявка #{request.id} на сумму {amount:.2f} ₽ создана.",
        reply_markup=main_menu(),
    )


@router.callback_query(F.data == "client:my_topups")
async def my_topups(callback: CallbackQuery, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_user(callback.from_user, container)
        requests = await hub.topups.list_requests(user_id=user.id)
    if not requests:
        text = "У вас пока нет заявок на пополнение."
    else:
        text = "Мои заявки\n\n" + "\n".join(
            [f"• {item.amount_rub} ₽ — {item.status} — {item.created_at:%d.%m %H:%M}" for item in requests[:10]]
        )
    await callback.message.edit_text(text)
    await callback.answer()


@router.callback_query(F.data == "client:plan_menu")
async def plan_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "Выберите тариф.\n\n"
        "Безлимит: 200 ₽ / 30 дней\n"
        "Лимитный: 100 ₽ / 30 дней / 50 ГБ",
        reply_markup=plan_actions().as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "client:trial_activate")
async def trial_activate(callback: CallbackQuery, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_user(callback.from_user, container)
        try:
            await hub.billing.activate_trial(user.id)
            text = "Тестовый период на 2 дня успешно активирован."
        except ConflictError as exc:
            text = str(exc)
    await callback.message.edit_text(text)
    await callback.answer()


@router.callback_query(F.data.startswith("client:activate_plan:"))
async def activate_plan(callback: CallbackQuery, container: AppContainer):
    plan_code = PlanCode(callback.data.split(":")[-1])
    async with container.hub() as hub:
        user = await ensure_user(callback.from_user, container)
        try:
            subscription = await hub.billing.activate_paid_plan(user.id, plan_code, charge_user=True)
            text = (
                f"Тариф «{subscription.plan.name}» активирован.\n"
                f"Следующее продление: {subscription.next_billing_at}"
            )
        except ConflictError as exc:
            text = f"{exc}\n\nСначала пополните баланс через раздел «Баланс»."
    await callback.message.edit_text(text)
    await callback.answer()


@router.callback_query(F.data == "client:subscription_link")
async def subscription_link(callback: CallbackQuery, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_user(callback.from_user, container)
        bundle = await hub.accounts.get_subscription_bundle(user.id)
        info = bundle.get("subscription_info")
        if not info:
            text = "Ссылка пока недоступна. Сначала активируйте тестовый период или тариф."
        else:
            text = (
                "Общая подписочная ссылка Remnawave:\n\n"
                f"{info.subscriptionUrl}\n\n"
                "Эта ссылка уже включает все доступные активные серверы."
            )
    await callback.message.edit_text(text)
    await callback.answer()


@router.callback_query(F.data == "client:subscription_qr")
async def subscription_qr(callback: CallbackQuery, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_user(callback.from_user, container)
        bundle = await hub.accounts.get_subscription_bundle(user.id)
        info = bundle.get("subscription_info")
        keys = bundle.get("connection_keys")
        payload = None
        if info:
            payload = info.subscriptionUrl
        elif keys and keys.enabledKeys:
            payload = keys.enabledKeys[0]
        if payload is None:
            await callback.message.answer("QR пока недоступен. Сначала активируйте доступ.")
            await callback.answer()
            return
        image = render_qr_png(payload)
        await callback.message.answer_photo(
            BufferedInputFile(image, filename="altlink-qr.png"),
            caption="QR-код для подключения.",
        )
    await callback.answer()


@router.callback_query(F.data == "client:traffic")
async def traffic(callback: CallbackQuery, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_user(callback.from_user, container)
        subscription = await hub.accounts.get_current_subscription(user.id)
        if not subscription:
            text = "У вас пока нет активной подписки."
        else:
            text = (
                f"Трафик по подписке\n\n"
                f"Использовано: {subscription.traffic_used_bytes}\n"
                f"Лимит: {subscription.traffic_limit_bytes or 'без лимита'}\n"
                f"Следующее продление: {subscription.next_billing_at}"
            )
    await callback.message.edit_text(text)
    await callback.answer()
