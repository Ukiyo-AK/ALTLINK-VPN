from __future__ import annotations

from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from altlink.application.services.base import ConflictError, NotFoundError, ServiceError
from altlink.application.services.registry import AppContainer
from altlink.domain.billing import bytes_to_gb_cost
from altlink.domain.enums import ServerType
from altlink.domain.plans import WHITELIST_GB_PRICE_RUB, parse_paid_plan_code
from altlink.presentation.bots.client_keyboards import (
    balance_actions,
    channel_gate_actions,
    main_menu,
    plan_actions,
    subscription_actions,
    topup_actions,
)
from altlink.utils.qr import render_qr_png
from altlink.utils.telegram_web import check_channel_membership

router = Router(name="client-router")


class TopupStates(StatesGroup):
    waiting_for_amount = State()


async def ensure_user(telegram_user, container: AppContainer, hub=None):
    if hub is not None:
        return await hub.accounts.get_or_create_user(
            telegram_id=telegram_user.id,
            username=telegram_user.username,
            first_name=telegram_user.first_name,
            last_name=telegram_user.last_name,
            language_code=telegram_user.language_code,
        )
    async with container.hub() as inner_hub:
        return await inner_hub.accounts.get_or_create_user(
            telegram_id=telegram_user.id,
            username=telegram_user.username,
            first_name=telegram_user.first_name,
            last_name=telegram_user.last_name,
            language_code=telegram_user.language_code,
        )


async def is_channel_member(telegram_id: int, container: AppContainer) -> bool:
    settings = container.settings
    if not settings.required_subscription_channel:
        return True
    return await check_channel_membership(
        bot_token=settings.client_bot_token,
        channel=settings.required_subscription_channel,
        user_id=telegram_id,
    )


async def ensure_channel_access(message: Message | CallbackQuery, container: AppContainer) -> bool:
    telegram_id = message.from_user.id
    if await is_channel_member(telegram_id, container):
        return True

    settings = container.settings
    text = (
        "Добро пожаловать в ALTLINK.\n\n"
        "Перед началом работы подпишитесь на официальный Telegram-канал проекта. "
        "Без подписки бот не открывает тарифы, тест и конфиги.\n\n"
        "После подписки нажмите «Проверить подписку»."
    )
    markup = channel_gate_actions(settings.required_subscription_channel_url or None).as_markup()
    if isinstance(message, CallbackQuery):
        await message.message.edit_text(text, reply_markup=markup)
        await message.answer()
    else:
        await message.answer(text, reply_markup=markup)
    return False


def server_badge(server_type: ServerType) -> str:
    if server_type == ServerType.TEN_GBIT:
        return "⚡"
    if server_type == ServerType.WHITELIST:
        return "WL"
    return "•"


def profile_text(user, subscription) -> str:
    plan_name = subscription.plan.name if subscription and subscription.plan else "не выбран"
    next_billing = subscription.next_billing_at.strftime("%d.%m.%Y %H:%M") if subscription else "—"
    debt = Decimal(subscription.accrued_debt_rub) if subscription else Decimal("0")
    assigned = user.assigned_server.name if getattr(user, "assigned_server", None) else "ещё не назначен"
    return (
        "Профиль\n\n"
        f"Telegram ID: {user.telegram_id}\n"
        f"Статус: {user.status}\n"
        f"Баланс: {Decimal(user.balance_rub):.2f} ₽\n"
        f"Тариф: {plan_name}\n"
        f"Следующее списание: {next_billing}\n"
        f"Задолженность: {debt:.2f} ₽\n"
        f"Выделенный сервер: {assigned}"
    )


def subscription_text(bundle: dict, user_servers: list) -> str:
    user = bundle["user"]
    subscription = bundle.get("subscription")
    if not subscription:
        return (
            "Подписка пока не активирована.\n\n"
            "Выберите тариф или запустите тестовый период на 2 дня."
        )

    server_lines = []
    for access in user_servers:
        if not access.server:
            continue
        icon = server_badge(access.server.server_type)
        type_label = {
            ServerType.TEN_GBIT: "10 Гбит",
            ServerType.WHITELIST: "Белые списки",
            ServerType.REGULAR: "Обычный",
        }[access.server.server_type]
        server_lines.append(f"{icon} {access.server.name} • {type_label} • {access.status}")
    whitelist_cost = bytes_to_gb_cost(
        subscription.whitelist_traffic_used_bytes,
        WHITELIST_GB_PRICE_RUB,
    )
    return (
        "Моя подписка\n\n"
        f"Статус: {user.status}\n"
        f"Тариф: {subscription.plan.name}\n"
        f"Следующее списание: {subscription.next_billing_at:%d.%m.%Y %H:%M}\n"
        f"Накопленный долг: {Decimal(subscription.accrued_debt_rub):.2f} ₽\n"
        f"Общий трафик: {subscription.traffic_used_bytes / 1024**3:.2f} ГБ\n"
        f"Трафик по белым спискам: {subscription.whitelist_traffic_used_bytes / 1024**3:.2f} ГБ\n"
        f"Начисление за белые списки: {whitelist_cost:.2f} ₽\n\n"
        "Доступные серверы:\n"
        f"{chr(10).join(server_lines) if server_lines else 'Пока нет активных серверов.'}"
    )


@router.message(CommandStart())
async def start(message: Message, container: AppContainer):
    await ensure_user(message.from_user, container)
    if not await ensure_channel_access(message, container):
        return
    async with container.hub() as hub:
        user = await hub.accounts.get_user_by_telegram_id(message.from_user.id)
        subscription = await hub.accounts.get_current_subscription(user.id)
    text = (
        "ALTLINK VPN\n\n"
        "Здесь можно запустить тест, выбрать тариф, пополнить внутренний баланс, "
        "получить ссылку и QR для подключения, а также управлять подпиской с сайта."
    )
    if subscription:
        text += f"\n\nСейчас у вас активен тариф: {subscription.plan.name}"
    await message.answer(text, reply_markup=main_menu())


@router.callback_query(F.data == "client:check_channel")
async def check_channel(callback: CallbackQuery, container: AppContainer):
    if not await ensure_channel_access(callback, container):
        return
    await callback.message.edit_text(
        "Подписка подтверждена. Можно продолжать работу в боте.",
    )
    await callback.message.answer("Открываю главное меню.", reply_markup=main_menu())
    await callback.answer()


@router.message(F.text == "Профиль")
async def profile(message: Message, container: AppContainer):
    if not await ensure_channel_access(message, container):
        return
    async with container.hub() as hub:
        user = await ensure_user(message.from_user, container, hub)
        subscription = await hub.accounts.get_current_subscription(user.id)
    await message.answer(profile_text(user, subscription), reply_markup=main_menu())


@router.message(F.text == "Баланс")
async def balance(message: Message, container: AppContainer):
    if not await ensure_channel_access(message, container):
        return
    async with container.hub() as hub:
        user = await ensure_user(message.from_user, container, hub)
        requests = await hub.topups.list_requests(user_id=user.id)
    await message.answer(
        f"Баланс: {Decimal(user.balance_rub):.2f} ₽\n\n"
        f"Платежей в истории: {len(requests)}\n"
        "Сейчас включён тестовый внешний провайдер оплаты: платёж проходит автоматически.",
        reply_markup=balance_actions().as_markup(),
    )


@router.message(F.text == "Подписка")
async def subscription(message: Message, container: AppContainer):
    if not await ensure_channel_access(message, container):
        return
    async with container.hub() as hub:
        user = await ensure_user(message.from_user, container, hub)
        bundle = await hub.accounts.get_subscription_bundle(user.id)
        user_servers = await hub.catalog.get_user_servers(user.id)
    await message.answer(subscription_text(bundle, user_servers), reply_markup=subscription_actions().as_markup())


@router.message(F.text == "Серверы")
async def servers(message: Message, container: AppContainer):
    if not await ensure_channel_access(message, container):
        return
    async with container.hub() as hub:
        user = await ensure_user(message.from_user, container, hub)
        user_servers = await hub.catalog.get_user_servers(user.id)
    lines = []
    for access in user_servers:
        if not access.server or access.status == "blocked":
            continue
        icon = server_badge(access.server.server_type)
        type_label = {
            ServerType.TEN_GBIT: "10 Гбит",
            ServerType.WHITELIST: "Белые списки",
            ServerType.REGULAR: "Обычный сервер",
        }[access.server.server_type]
        lines.append(
            f"{icon} {access.server.name}\n"
            f"Локация: {access.server.country_code or '—'}\n"
            f"Тип: {type_label}\n"
            f"Статус: {access.status}\n"
        )
    text = "Мои серверы\n\n" + ("\n".join(lines) if lines else "Пока нет активных серверов.")
    await message.answer(text)


@router.message(F.text == "Сайт")
async def site_link(message: Message, container: AppContainer):
    if not await ensure_channel_access(message, container):
        return
    portal_url = f"{container.settings.backend_public_url.rstrip('/')}/portal"
    await message.answer(
        "Пользовательский портал ALTLINK\n\n"
        f"{portal_url}\n\n"
        "Вход на сайте выполняется через Telegram Login, поэтому бот и сайт работают с одним аккаунтом."
    )


@router.message(F.text == "Помощь")
async def help_screen(message: Message, container: AppContainer):
    if not await ensure_channel_access(message, container):
        return
    await message.answer(
        "Помощь\n\n"
        "1. Сначала подпишитесь на официальный канал проекта.\n"
        "2. Тестовый период доступен один раз на 2 дня.\n"
        "3. Тариф 69 ₽ даёт один автоматически назначенный сервер 10 Гбит.\n"
        "4. Серверы типа «Белые списки» тарифицируются отдельно по 4 ₽ за ГБ.\n"
        "5. Тариф 200 ₽ открывает все доступные серверы без ограничений.\n"
        "6. Пополнение сейчас работает через тестовую заглушку и зачисляется автоматически.\n"
        "7. Если денег не хватает, доступ уходит в льготный период на 14 дней."
    )


@router.callback_query(F.data == "client:topup_menu")
async def topup_menu(callback: CallbackQuery, container: AppContainer):
    if not await ensure_channel_access(callback, container):
        return
    await callback.message.edit_text(
        "Выберите сумму пополнения. Сейчас работает тестовая заглушка внешнего платёжного сервиса.",
        reply_markup=topup_actions().as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("client:topup_amount:"))
async def topup_amount(callback: CallbackQuery, container: AppContainer):
    if not await ensure_channel_access(callback, container):
        return
    amount = Decimal(callback.data.split(":")[-1])
    async with container.hub() as hub:
        user = await ensure_user(callback.from_user, container, hub)
        request = await hub.topups.create_request(user.id, amount, auto_complete=True)
        await hub.session.refresh(user)
    await callback.message.edit_text(
        f"Платёж на {amount:.2f} ₽ проведён в тестовом режиме.\n"
        f"Баланс пополнен автоматически.\n\n"
        f"Номер платежа: {request.id}"
    )
    await callback.answer()


@router.callback_query(F.data == "client:topup_custom")
async def topup_custom(callback: CallbackQuery, state: FSMContext, container: AppContainer):
    if not await ensure_channel_access(callback, container):
        return
    await state.set_state(TopupStates.waiting_for_amount)
    await callback.message.answer("Введите сумму пополнения в рублях, например: 350")
    await callback.answer()


@router.message(TopupStates.waiting_for_amount)
async def topup_custom_value(message: Message, state: FSMContext, container: AppContainer):
    if not await ensure_channel_access(message, container):
        return
    try:
        amount = Decimal(message.text.replace(",", "."))
    except (InvalidOperation, AttributeError):
        await message.answer("Не удалось распознать сумму. Попробуйте ещё раз.")
        return
    async with container.hub() as hub:
        user = await ensure_user(message.from_user, container, hub)
        request = await hub.topups.create_request(user.id, amount, auto_complete=True)
    await state.clear()
    await message.answer(
        f"Платёж #{request.id} на сумму {amount:.2f} ₽ успешно зачислен.",
        reply_markup=main_menu(),
    )


@router.callback_query(F.data == "client:my_topups")
async def my_topups(callback: CallbackQuery, container: AppContainer):
    if not await ensure_channel_access(callback, container):
        return
    async with container.hub() as hub:
        user = await ensure_user(callback.from_user, container, hub)
        requests = await hub.topups.list_requests(user_id=user.id)
    if not requests:
        text = "У вас пока нет платежей."
    else:
        text = "История платежей\n\n" + "\n".join(
            [
                f"• {Decimal(item.amount_rub):.2f} ₽ — {item.status} — {item.created_at:%d.%m %H:%M}"
                for item in requests[:10]
            ]
        )
    await callback.message.edit_text(text)
    await callback.answer()


@router.callback_query(F.data == "client:plan_menu")
async def plan_menu(callback: CallbackQuery, container: AppContainer):
    if not await ensure_channel_access(callback, container):
        return
    await callback.message.edit_text(
        "Выберите тариф.\n\n"
        "Один сервер 10 Гбит: 69 ₽ в месяц, списание происходит ежедневно по частям.\n"
        "Белые списки: 4 ₽ за каждый ГБ отдельно.\n"
        "Безлимит: 200 ₽ в месяц, доступны все серверы.",
        reply_markup=plan_actions().as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "client:trial_activate")
async def trial_activate(callback: CallbackQuery, container: AppContainer):
    if not await ensure_channel_access(callback, container):
        return
    async with container.hub() as hub:
        user = await ensure_user(callback.from_user, container, hub)
        try:
            subscription = await hub.billing.activate_trial(user.id)
            text = (
                "Тестовый период на 2 дня успешно активирован.\n"
                f"Доступ будет работать до {subscription.ends_at:%d.%m.%Y %H:%M}."
            )
        except ConflictError as exc:
            text = str(exc)
        except (NotFoundError, ServiceError) as exc:
            text = str(exc)
    await callback.message.edit_text(text)
    await callback.answer()


@router.callback_query(F.data.startswith("client:activate_plan:"))
async def activate_plan(callback: CallbackQuery, container: AppContainer):
    if not await ensure_channel_access(callback, container):
        return
    plan_code = parse_paid_plan_code(callback.data.split(":")[-1])
    if plan_code is None:
        await callback.message.edit_text(
            "Этот тариф больше не поддерживается или кнопка устарела.\n\n"
            "Откройте меню тарифов заново и выберите один из актуальных вариантов."
        )
        await callback.answer()
        return
    async with container.hub() as hub:
        user = await ensure_user(callback.from_user, container, hub)
        try:
            subscription = await hub.billing.activate_paid_plan(user.id, plan_code, charge_user=True)
            text = (
                f"Тариф «{subscription.plan.name}» активирован.\n"
                f"Следующее ежедневное списание: {subscription.next_billing_at:%d.%m.%Y %H:%M}"
            )
        except ConflictError as exc:
            text = f"{exc}\n\nСначала пополните баланс через раздел «Баланс»."
        except (NotFoundError, ServiceError) as exc:
            text = str(exc)
    await callback.message.edit_text(text)
    await callback.answer()


@router.callback_query(F.data == "client:subscription_link")
async def subscription_link(callback: CallbackQuery, container: AppContainer):
    if not await ensure_channel_access(callback, container):
        return
    async with container.hub() as hub:
        user = await ensure_user(callback.from_user, container, hub)
        bundle = await hub.accounts.get_subscription_bundle(user.id)
        info = bundle.get("subscription_info")
        if not info:
            text = "Ссылка пока недоступна. Сначала активируйте тестовый период или тариф."
        else:
            text = (
                "Персональная подписочная ссылка Remnawave:\n\n"
                f"{info.subscriptionUrl}\n\n"
                "Ссылка уже учитывает доступные вам серверы по текущему тарифу."
            )
    await callback.message.edit_text(text)
    await callback.answer()


@router.callback_query(F.data == "client:subscription_qr")
async def subscription_qr(callback: CallbackQuery, container: AppContainer):
    if not await ensure_channel_access(callback, container):
        return
    async with container.hub() as hub:
        user = await ensure_user(callback.from_user, container, hub)
        bundle = await hub.accounts.get_subscription_bundle(user.id)
        info = bundle.get("subscription_info")
        keys = bundle.get("connection_keys")
        payload = info.subscriptionUrl if info else None
        if payload is None and keys and keys.enabledKeys:
            payload = keys.enabledKeys[0]
        if payload is None:
            await callback.message.answer("QR-код пока недоступен. Сначала активируйте доступ.")
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
    if not await ensure_channel_access(callback, container):
        return
    async with container.hub() as hub:
        user = await ensure_user(callback.from_user, container, hub)
        subscription = await hub.accounts.get_current_subscription(user.id)
        if not subscription:
            text = "У вас пока нет активной подписки."
        else:
            white_cost = bytes_to_gb_cost(subscription.whitelist_traffic_used_bytes, WHITELIST_GB_PRICE_RUB)
            text = (
                "Трафик и начисления\n\n"
                f"Общий трафик: {subscription.traffic_used_bytes / 1024**3:.2f} ГБ\n"
                f"Трафик по белым спискам: {subscription.whitelist_traffic_used_bytes / 1024**3:.2f} ГБ\n"
                f"Начислено за белые списки: {white_cost:.2f} ₽\n"
                f"Накопленный долг: {Decimal(subscription.accrued_debt_rub):.2f} ₽\n"
                f"Следующее списание: {subscription.next_billing_at:%d.%m.%Y %H:%M}"
            )
    await callback.message.edit_text(text)
    await callback.answer()
