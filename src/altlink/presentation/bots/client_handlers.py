from __future__ import annotations

import html
import logging
from decimal import Decimal, InvalidOperation
import re
from urllib.parse import quote_plus, urlparse

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, InputMediaPhoto, Message
from aiogram.utils.formatting import Bold, Strikethrough, Text, as_list, as_marked_list

from altlink.application.services.base import ConflictError, NotFoundError, ServiceError
from altlink.application.services.registry import AppContainer
from altlink.application.services.topups import MIN_TOPUP_AMOUNT_RUB
from altlink.domain.billing import bytes_to_gb_cost, quantize_money
from altlink.domain.plans import (
    SINGLE_10GBIT_MONTHLY_PRICE_RUB,
    SINGLE_10GBIT_WEEKLY_PRICE_RUB,
    UNLIMITED_MONTHLY_PRICE_RUB,
    UNLIMITED_WEEKLY_PRICE_RUB,
    WHITELIST_GB_PRICE_RUB,
    is_metered_plan_code,
    parse_paid_plan_code,
)
from altlink.presentation.bots.admin_keyboards import payment_request_actions, support_request_actions
from altlink.presentation.bots.common import send_telegram_messages
from altlink.presentation.bots.client_keyboards import (
    agreement_actions,
    balance_actions,
    channel_actions,
    insufficient_balance_actions,
    main_menu,
    menu_actions,
    plan_actions,
    plan_period_actions,
    portal_login_actions,
    portal_login_complete_actions,
    profile_actions,
    promo_onboarding_actions,
    promo_onboarding_skip_actions,
    site_actions,
    subscription_link_actions,
    subscription_actions,
    support_actions,
    topup_amount_confirm_actions,
    topup_checkout_actions,
    topup_actions,
    topup_provider_actions,
)
from altlink.utils.media import media_path
from altlink.utils.qr import render_qr_png
from altlink.utils.telegram_web import check_channel_membership

router = Router(name="client-router")
logger = logging.getLogger(__name__)
TELEGRAM_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
CLIENT_LAST_CARD: dict[int, tuple[int, bool]] = {}
CLIENT_REPLY_MENU_READY: set[int] = set()


class TopupStates(StatesGroup):
    waiting_for_amount = State()


class SupportStates(StatesGroup):
    waiting_for_issue = State()


class PromoStates(StatesGroup):
    waiting_for_code = State()


def support_username_label(settings) -> str:
    username = (settings.support_username or "").strip()
    if not username:
        return "@support_placeholder"
    return username if username.startswith("@") else f"@{username}"


def support_profile_url(settings) -> str | None:
    username = support_username_label(settings).lstrip("@")
    if not username:
        return None
    return f"https://t.me/{username}"


def admin_payment_request_text(user, amount: Decimal, request_id: str) -> str:
    user_name = f"@{user.username}" if getattr(user, "username", None) else f"Telegram ID {user.telegram_id}"
    return (
        "💳 Новый запрос на пополнение\n\n"
        f"Пользователь: {user_name}\n"
        f"Telegram ID: {user.telegram_id}\n"
        f"Сумма: {Decimal(amount):.2f} ₽\n"
        f"Номер заявки: {request_id}\n\n"
        "Проверьте оплату и выберите действие ниже."
    )


def admin_support_request_text(user, request_id: str, message: str) -> str:
    user_name = f"@{user.username}" if getattr(user, "username", None) else f"Telegram ID {user.telegram_id}"
    return (
        "🆘 Новый запрос в поддержку\n\n"
        f"Пользователь: {user_name}\n"
        f"Telegram ID: {user.telegram_id}\n"
        f"Номер запроса: {request_id}\n\n"
        f"{message}"
    )


def topup_provider_label(provider: str) -> str:
    labels = {
        "yookassa": "💳 Юкасса СБП",
        "manual": "💬 Через поддержку",
        "stub": "🧪 Тестовая касса",
    }
    return labels.get(provider, provider)


def available_topup_provider_codes(configured_provider: str, resolved_provider: str) -> list[str]:
    if configured_provider == "manual":
        return ["manual"]
    providers: list[str] = []
    if resolved_provider in {"yookassa", "stub"}:
        providers.append(resolved_provider)
    providers.append("manual")
    return list(dict.fromkeys(providers))


def format_topup_amount_token(amount: Decimal) -> str:
    return f"{Decimal(amount):.2f}"


def parse_topup_amount_token(token: str) -> Decimal:
    return Decimal(token)


def topup_amount_confirmation_text(amount: Decimal) -> str:
    return (
        "Пополнение баланса\n\n"
        f"Сумма: {Decimal(amount):.2f} ₽\n\n"
        "Если всё верно, нажмите «Оплатить». На следующем шаге можно будет выбрать способ оплаты."
    )


def topup_provider_selection_text(amount: Decimal, providers: list[str]) -> str:
    return (
        "💸 Способ оплаты\n\n"
        f"Сумма: {Decimal(amount):.2f} ₽\n\n"
        "Нажмите на удобный способ оплаты ниже."
    )


def topup_provider_status_text(*, configured_provider: str, resolved_provider: str, missing_settings: list[str]) -> str:
    if configured_provider == "yookassa":
        if resolved_provider == "yookassa":
            return "Оплата откроется через Юкасса СБП."
        missing = ", ".join(missing_settings) or "YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY"
        return (
            "Юкасса СБП выбрана как касса, но бот не видит полную настройку.\n"
            f"Не хватает: {missing}.\n"
            "Пока используется тестовая заглушка."
        )
    if resolved_provider == "manual":
        return "Пополнение доступно через поддержку с подтверждением заявки."
    if resolved_provider == "stub":
        return "Если касса не настроена, бот использует тестовую заглушку и зачисляет деньги сразу."
    return "Пополнение будет обработано автоматически."


def balance_topup_status_text(*, configured_provider: str, resolved_provider: str, missing_settings: list[str]) -> str:
    if configured_provider == "yookassa":
        if resolved_provider == "yookassa":
            return "Пополнение доступно через Юкасса СБП."
        missing = ", ".join(missing_settings) or "YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY"
        return (
            "Юкасса СБП выбрана как касса, но бот не видит полную настройку.\n"
            f"Не хватает: {missing}.\n"
            "Пока используется тестовая заглушка."
        )
    if resolved_provider == "manual":
        return "Пополнение сейчас проходит через поддержку с подтверждением заявки."
    if resolved_provider == "stub":
        return "Пополнение сейчас работает через тестовую заглушку и зачисляется автоматически."
    return "Способ пополнения будет определён автоматически."


def topup_status_label(raw_status: str) -> str:
    labels = {
        "approved": "зачислено",
        "new": "ожидает оплаты",
        "rejected": "отклонён",
        "canceled": "отменён",
        "paid": "оплачено",
        "pending": "ожидает оплаты",
        "declined": "отклонён",
        "canceled": "отменён",
        "expired": "истёк",
        "stub": "тестовый режим",
        "manual": "ручная проверка",
    }
    return labels.get(str(raw_status), str(raw_status))


def technical_maintenance_text(settings) -> str:
    return (
        "Технические работы\n\n"
        "В боте сейчас идут технические работы, поэтому его функционал временно недоступен.\n"
        "Попробуйте снова чуть позже.\n\n"
        f"Если вопрос срочный, напишите в поддержку: {support_username_label(settings)}"
    )


async def client_maintenance_active(
    container: AppContainer,
    telegram_id: int | None = None,
    hub=None,
) -> bool:
    monitoring = getattr(hub, "monitoring", None) if hub is not None else None
    if monitoring is not None:
        return await monitoring.is_client_maintenance_active(telegram_id=telegram_id)
    async with container.hub() as inner_hub:
        return await inner_hub.monitoring.is_client_maintenance_active(telegram_id=telegram_id)


async def show_technical_maintenance(target: Message | CallbackQuery, container: AppContainer) -> None:
    await answer_or_edit(
        target,
        technical_maintenance_text(container.settings),
        reply_markup=support_actions(support_profile_url(container.settings)).as_markup(),
    )


async def notify_admins_about_topup_request(
    container: AppContainer,
    *,
    user,
    amount: Decimal,
    request_id: str,
    admin_telegram_ids: list[int],
) -> None:
    if not admin_telegram_ids or not container.settings.admin_bot_token:
        return

    text = admin_payment_request_text(user, amount, request_id)
    markup = payment_request_actions(request_id, "new").as_markup()
    await send_telegram_messages(
        bot_token=container.settings.admin_bot_token,
        chat_ids=admin_telegram_ids,
        text=text,
        reply_markup=markup,
    )


async def notify_admins_about_support_request(
    container: AppContainer,
    *,
    user,
    request_id: str,
    message: str,
    admin_telegram_ids: list[int],
) -> None:
    if not admin_telegram_ids or not container.settings.admin_bot_token:
        return

    text = admin_support_request_text(user, request_id, message)
    markup = support_request_actions(request_id, False).as_markup()
    await send_telegram_messages(
        bot_token=container.settings.admin_bot_token,
        chat_ids=admin_telegram_ids,
        text=text,
        reply_markup=markup,
    )


async def answer_or_edit_topup_checkout(
    target: Message | CallbackQuery,
    text: str,
    *,
    payment_url: str | None = None,
    payment_label: str = "Оплатить",
    request_id: str | None = None,
    can_check: bool = False,
) -> None:
    reply_markup = topup_checkout_actions(
        payment_url=payment_url,
        payment_label=payment_label,
        request_id=request_id,
        can_check=can_check,
    ).as_markup()
    if isinstance(target, CallbackQuery):
        rendered = False
        if await try_edit_tracked_client_card(target, text, reply_markup=reply_markup, media_file=None):
            rendered = True
        else:
            try:
                result = await target.message.edit_text(text, reply_markup=reply_markup)
                remember_client_card(result, has_media=False)
                rendered = True
            except TelegramBadRequest:
                result = await target.message.answer(text, reply_markup=reply_markup)
                remember_client_card(result, has_media=False)
                rendered = True
        if rendered:
            if payment_url:
                await target.answer(url=payment_url)
            else:
                await target.answer()
            return
    await answer_or_edit(target, text, reply_markup=reply_markup)


async def handle_topup_checkout(
    target: Message | CallbackQuery,
    container: AppContainer,
    *,
    user,
    amount: Decimal,
    checkout,
    admin_telegram_ids: list[int] | None = None,
) -> None:
    if checkout.provider == "manual":
        await notify_admins_about_topup_request(
            container,
            user=user,
            amount=amount,
            request_id=checkout.request.id,
            admin_telegram_ids=admin_telegram_ids or [],
        )
        support_url = support_profile_url(container.settings)
        await answer_or_edit_topup_checkout(
            target,
            (
                f"🧾 Заявка на пополнение создана.\n\n"
                f"Сумма: {amount:.2f} ₽\n"
                f"Номер заявки: {checkout.request.id}\n"
                "Откройте поддержку, отправьте оплату и сообщите номер заявки. "
                "После проверки администратор подтвердит или отклонит её в боте."
            ),
            payment_url=support_url,
            payment_label="Открыть поддержку",
            request_id=checkout.request.id,
            can_check=False,
        )
        return

    if checkout.provider == "yookassa":
        await answer_or_edit_topup_checkout(
            target,
            (
                "💳 Ссылка на оплату готова.\n\n"
                f"Сумма: {amount:.2f} ₽\n"
                f"Номер заявки: {checkout.request.id}\n"
                "Откройте ссылку, завершите оплату в Юкасса СБП и затем нажмите кнопку проверки статуса."
            ),
            payment_url=checkout.payment_url,
            payment_label="Оплатить",
            request_id=checkout.request.id,
            can_check=True,
        )
        return

    await answer_or_edit(
        target,
        (
            "🧪 Тестовое пополнение выполнено.\n\n"
            f"Сумма: {amount:.2f} ₽\n"
            f"Номер заявки: {checkout.request.id}\n"
            "Касса сейчас работает в режиме заглушки, поэтому деньги зачислены сразу."
        ),
        reply_markup=balance_actions().as_markup(),
    )


async def show_topup_amount_confirmation(target: Message | CallbackQuery, amount: Decimal) -> None:
    amount_token = format_topup_amount_token(amount)
    await answer_or_edit(
        target,
        topup_amount_confirmation_text(amount),
        reply_markup=topup_amount_confirm_actions(amount_token).as_markup(),
    )


async def show_topup_provider_menu(
    target: Message | CallbackQuery,
    amount: Decimal,
    providers: list[str],
) -> None:
    amount_token = format_topup_amount_token(amount)
    provider_items = [(provider, topup_provider_label(provider)) for provider in providers]
    await answer_or_edit(
        target,
        topup_provider_selection_text(amount, providers),
        reply_markup=topup_provider_actions(
            amount_token,
            provider_items,
        ).as_markup(),
    )


async def continue_topup_flow(
    target: Message | CallbackQuery,
    container: AppContainer,
    *,
    user,
    amount: Decimal,
) -> None:
    checkout = None
    admin_telegram_ids: list[int] = []

    async with container.hub() as hub:
        configured_provider = hub.topups.configured_provider()
        resolved_provider = hub.topups.resolved_provider()
        providers = available_topup_provider_codes(configured_provider, resolved_provider)

        if len(providers) == 1:
            try:
                checkout = await hub.topups.create_checkout(user.id, amount, provider_code=providers[0])
            except ConflictError as exc:
                await answer_or_edit(target, str(exc), reply_markup=balance_actions().as_markup())
                return
            if checkout.provider == "manual":
                admin_telegram_ids = await hub.accounts.list_admin_telegram_ids()

    if len(providers) == 1 and checkout is not None:
        await handle_topup_checkout(
            target,
            container,
            user=user,
            amount=amount,
            checkout=checkout,
            admin_telegram_ids=admin_telegram_ids,
        )
        return

    await show_topup_provider_menu(target, amount, providers)


def agreement_url(settings) -> str | None:
    public_url = (settings.backend_public_url or "").strip()
    parsed = urlparse(public_url)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return f"{public_url.rstrip('/')}/legal/agreement"
    return (settings.user_agreement_telegraph_url or "").strip() or None


def privacy_url(settings) -> str | None:
    public_url = (settings.backend_public_url or "").strip()
    parsed = urlparse(public_url)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return f"{public_url.rstrip('/')}/legal/privacy"
    return (settings.privacy_policy_telegraph_url or "").strip() or None


def billing_cycle_label(plan) -> str:
    if plan is None:
        return "—"
    return "еженедельно" if plan.period_days <= 7 else "ежемесячно"


def device_limit_label(plan) -> str:
    if plan is None or plan.device_limit is None:
        return "без лимита"
    return str(plan.device_limit)


def can_manage_auto_renew(subscription) -> bool:
    return bool(subscription and subscription.plan and not subscription.plan.is_trial)


def share_vpn_target_url(settings) -> str | None:
    bot_name = (settings.client_bot_name or "").strip().lstrip("@")
    if bot_name and TELEGRAM_USERNAME_RE.fullmatch(bot_name):
        return f"https://t.me/{bot_name}"

    return portal_public_url(settings)


def share_vpn_url(settings) -> str | None:
    target_url = share_vpn_target_url(settings)
    if target_url is None:
        return None
    text = "Попробуй ALTLINK VPN. Подключение, баланс и подписка доступны прямо в Telegram."
    return f"https://t.me/share/url?url={quote_plus(target_url)}&text={quote_plus(text)}"


def bot_public_url(settings) -> str | None:
    bot_name = (settings.client_bot_name or "").strip().lstrip("@")
    if bot_name and TELEGRAM_USERNAME_RE.fullmatch(bot_name):
        return f"https://t.me/{bot_name}"
    return None


def site_public_url(settings) -> str | None:
    public_url = (settings.backend_public_url or "").strip()
    parsed = urlparse(public_url)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return public_url.rstrip("/")
    return None


def portal_public_url(settings) -> str | None:
    public_url = (settings.backend_public_url or "").strip()
    parsed = urlparse(public_url)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return f"{public_url.rstrip('/')}/portal"
    return None


def referral_share_vpn_url(settings, referral_code: str | None) -> str | None:
    target_url = bot_public_url(settings)
    if referral_code and target_url:
        target_url = f"{target_url}?start=ref_{referral_code}"
    elif target_url is None:
        target_url = site_public_url(settings)
    if target_url is None:
        return share_vpn_url(settings)
    text = "Попробуй ALTLINK VPN по моей ссылке и подключись через Telegram."
    return f"https://t.me/share/url?url={quote_plus(target_url)}&text={quote_plus(text)}"


def connection_help_url(settings) -> str | None:
    public_url = (settings.backend_public_url or "").strip()
    parsed = urlparse(public_url)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return f"{public_url.rstrip('/')}/help/connect"
    return None


def portal_login_request_text(settings) -> str:
    portal_url = portal_public_url(settings)
    tail = f"\n\nПосле подтверждения кабинет откроется автоматически: {portal_url}" if portal_url else ""
    return (
        "Вход в личный кабинет\n\n"
        "Подтвердите вход в сайт через этот Telegram-аккаунт. "
        "Если это именно ваш запрос, нажмите кнопку ниже."
        f"{tail}"
    )


def portal_login_resume_url(settings, token: str) -> str | None:
    public_url = (settings.backend_public_url or "").strip()
    parsed = urlparse(public_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return f"{public_url.rstrip('/')}/portal/login?token={quote_plus(token)}"


def portal_login_confirmed_text(settings) -> str:
    portal_url = portal_public_url(settings)
    tail = f"\n\nЕсли кабинет не открылся сам, нажмите кнопку ниже или перейдите вручную: {portal_url}" if portal_url else ""
    return (
        "Вход подтверждён.\n\n"
        "Сейчас попробуем открыть личный кабинет автоматически."
        f"{tail}"
    )


def portal_login_canceled_text(settings) -> str:
    portal_url = portal_public_url(settings)
    tail = f"\n\nЕсли нужно, вернитесь на сайт и начните вход заново: {portal_url}" if portal_url else ""
    return (
        "Вход для сайта отменён.\n\n"
        "Текущая попытка входа больше не активна."
        f"{tail}"
    )


def show_metered_usage(subscription) -> bool:
    return bool(subscription and subscription.plan and is_metered_plan_code(subscription.plan.code))


def resolve_subscription_payload(bundle: dict | None) -> str | None:
    if not bundle:
        return None
    info = bundle.get("subscription_info")
    if info:
        return info.subscriptionUrl
    keys = bundle.get("connection_keys")
    if keys and keys.enabledKeys:
        return keys.enabledKeys[0]
    return None


async def safe_get_subscription_bundle(hub, user_id: str) -> dict | None:
    try:
        return await hub.accounts.get_subscription_bundle(user_id)
    except Exception:
        logger.exception("Failed to load subscription bundle for user %s", user_id)
        return None


def activation_link_pending_note() -> str:
    return (
        "\n\nСсылка для подключения появится в разделе «Моя ссылка», "
        "как только панель подтвердит доступ."
    )


def subscription_markup(subscription):
    return subscription_actions(
        show_link=bool(subscription),
        show_traffic=show_metered_usage(subscription),
        can_cancel=can_manage_auto_renew(subscription),
        auto_renew_disabled=bool(subscription and not subscription.auto_renew),
    ).as_markup()


def billed_whitelist_cost(subscription) -> Decimal:
    used_bytes = max(int(getattr(subscription, "whitelist_traffic_used_bytes", 0) or 0), 0)
    billed_bytes = max(int(getattr(subscription, "whitelist_traffic_billed_bytes", 0) or 0), 0)
    return bytes_to_gb_cost(min(used_bytes, billed_bytes), WHITELIST_GB_PRICE_RUB)


def outstanding_whitelist_cost(subscription) -> Decimal:
    total_cost = bytes_to_gb_cost(max(int(getattr(subscription, "whitelist_traffic_used_bytes", 0) or 0), 0), WHITELIST_GB_PRICE_RUB)
    charged_cost = billed_whitelist_cost(subscription)
    remaining = total_cost - charged_cost
    return remaining if remaining > Decimal("0.00") else Decimal("0.00")


def subscription_link_caption(payload: str) -> str:
    escaped_payload = html.escape(payload)
    return (
        "🔗 Ваша персональная ссылка VPN\n\n"
        f"<code>{escaped_payload}</code>\n\n"
        "Откройте её в VPN-клиенте или отсканируйте QR-код."
    )


def activation_success_caption(subscription, payload: str) -> str:
    escaped_payload = html.escape(payload)
    plan_name = html.escape(subscription.plan.name if subscription and subscription.plan else "VPN")
    return (
        f"✅ Тариф «{plan_name}» активирован.\n\n"
        f"Следующее списание: {subscription.next_billing_at:%d.%m.%Y %H:%M}\n"
        f"Формат списания: {billing_cycle_label(subscription.plan)}\n"
        f"Лимит устройств: {device_limit_label(subscription.plan)}\n\n"
        "🔗 Ваша персональная ссылка VPN\n"
        f"<code>{escaped_payload}</code>\n\n"
        "Откройте ссылку в VPN-клиенте или импортируйте её в приложение. Можно также отсканировать QR-код."
    )


def trial_activation_caption(subscription, payload: str) -> str:
    escaped_payload = html.escape(payload)
    return (
        "🎁 Тестовый период Pro активирован.\n\n"
        f"Доступ ко всем активным серверам открыт до {subscription.ends_at:%d.%m.%Y %H:%M}.\n"
        f"Лимит устройств: {device_limit_label(subscription.plan)}\n\n"
        "🔗 Ваша персональная ссылка VPN\n"
        f"<code>{escaped_payload}</code>\n\n"
        "Откройте ссылку в VPN-клиенте или импортируйте её в приложение. Можно также отсканировать QR-код."
    )


def subscription_link_markup(settings, subscription):
    return subscription_link_actions(
        show_traffic=show_metered_usage(subscription),
        help_url=connection_help_url(settings),
    ).as_markup()


async def ensure_user(telegram_user, container: AppContainer, hub=None):
    if hub is not None:
        user = await hub.accounts.get_or_create_user(
            telegram_id=telegram_user.id,
            username=telegram_user.username,
            first_name=telegram_user.first_name,
            last_name=telegram_user.last_name,
            language_code=telegram_user.language_code,
        )
        return await hub.accounts.get_user(user.id)
    async with container.hub() as inner_hub:
        user = await inner_hub.accounts.get_or_create_user(
            telegram_id=telegram_user.id,
            username=telegram_user.username,
            first_name=telegram_user.first_name,
            last_name=telegram_user.last_name,
            language_code=telegram_user.language_code,
        )
        return await inner_hub.accounts.get_user(user.id)


async def is_channel_member(telegram_id: int, container: AppContainer) -> bool:
    settings = container.settings
    if not settings.required_subscription_channel:
        return True
    return await check_channel_membership(
        bot_token=settings.client_bot_token,
        channel=settings.required_subscription_channel,
        user_id=telegram_id,
    )


async def answer_or_edit(message: Message | CallbackQuery, text: str, *, reply_markup=None, **kwargs):
    kwargs = {key: value for key, value in kwargs.items() if value is not None}
    if isinstance(message, CallbackQuery):
        if await try_edit_tracked_client_card(message, text, reply_markup=reply_markup, media_file=None, **kwargs):
            await message.answer()
            return message.message
        try:
            result = await message.message.edit_text(text, reply_markup=reply_markup, **kwargs)
            remember_client_card(message.message, has_media=False)
        except TelegramBadRequest:
            result = await message.message.answer(text, reply_markup=reply_markup, **kwargs)
            remember_client_card(result, has_media=False)
        await message.answer()
        return result
    result = await message.answer(text, reply_markup=reply_markup, **kwargs)
    remember_client_card(result, has_media=False)
    return result


async def show_inline_card(
    target: Message | CallbackQuery,
    text: str,
    *,
    inline_markup,
) -> None:
    await answer_or_edit(target, text, reply_markup=inline_markup)


def section_media_filename(section: str) -> str | None:
    mapping = {
        "home": "bot_image_yourMainMenu.png",
        "balance": "bot_image_yourBalance.png",
        "profile": "bot_image_yourProfile.png",
        "subscription": "bot_image_yourSubscription.png",
        "support": "logo with title.png",
        "onboarding": "logo with title.png",
    }
    return mapping.get(section)


async def send_card_with_optional_media(
    target: Message | CallbackQuery,
    text: str,
    *,
    primary_markup,
    media_section: str | None = None,
    fallback_markup=None,
    force_new_message: bool = False,
    parse_mode: str | None = None,
) -> None:
    anchor = target.message if isinstance(target, CallbackQuery) else target
    media_filename = section_media_filename(media_section) if media_section else None
    media_file = media_path(media_filename) if media_filename else None
    markups = [primary_markup]
    if fallback_markup is not None and fallback_markup is not primary_markup:
        markups.append(fallback_markup)

    last_exc: TelegramBadRequest | None = None
    for markup in markups:
        text_kwargs = {"reply_markup": markup}
        if parse_mode is not None:
            text_kwargs["parse_mode"] = parse_mode
        if isinstance(target, CallbackQuery) and not force_new_message and await try_edit_tracked_client_card(
            target,
            text,
            reply_markup=markup,
            media_file=media_file,
            parse_mode=parse_mode,
        ):
            if isinstance(target, CallbackQuery):
                await target.answer()
            return

        if media_file is not None and hasattr(anchor, "answer_photo"):
            try:
                photo_kwargs = {"caption": text, "reply_markup": markup}
                if parse_mode is not None:
                    photo_kwargs["parse_mode"] = parse_mode
                sent = await anchor.answer_photo(FSInputFile(str(media_file)), **photo_kwargs)
                remember_client_card(sent, has_media=True)
                if isinstance(target, CallbackQuery):
                    await target.answer()
                return
            except TelegramBadRequest as exc:
                last_exc = exc
                logger.warning("Failed to send media card %s, retrying: %s", media_section, exc)

        try:
            if force_new_message:
                sent = await anchor.answer(text, **text_kwargs)
                remember_client_card(sent, has_media=False)
                if isinstance(target, CallbackQuery):
                    await target.answer()
            else:
                await answer_or_edit(target, text, **text_kwargs)
            return
        except TelegramBadRequest as exc:
            last_exc = exc
            logger.warning("Failed to send text card %s, retrying: %s", media_section, exc)

    if last_exc is not None:
        raise last_exc


async def edit_or_send_dynamic_media_card(
    callback: CallbackQuery,
    *,
    image_bytes: bytes,
    filename: str,
    caption: str,
    reply_markup,
    parse_mode: str | None = None,
) -> None:
    anchor = callback.message
    try:
        await anchor.bot.edit_message_media(
            chat_id=anchor.chat.id,
            message_id=anchor.message_id,
            media=InputMediaPhoto(
                media=BufferedInputFile(image_bytes, filename=filename),
                caption=caption,
                parse_mode=parse_mode,
            ),
            reply_markup=reply_markup,
        )
        remember_client_card(anchor, has_media=True)
    except TelegramBadRequest:
        sent = await anchor.answer_photo(
            BufferedInputFile(image_bytes, filename=filename),
            caption=caption,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
        remember_client_card(sent, has_media=True)
    await callback.answer()


def remember_client_card(message: Message, *, has_media: bool) -> None:
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        chat_id = getattr(message, "chat_id", None)
    if chat_id is None:
        from_user = getattr(message, "from_user", None)
        chat_id = getattr(from_user, "id", None)
    message_id = getattr(message, "message_id", None)
    if chat_id is None or message_id is None:
        return
    CLIENT_LAST_CARD[chat_id] = (message_id, has_media)


async def try_edit_tracked_client_card(
    target: Message | CallbackQuery,
    text: str,
    *,
    reply_markup,
    media_file,
    parse_mode: str | None = None,
) -> bool:
    anchor = target.message if isinstance(target, CallbackQuery) else target
    tracked = CLIENT_LAST_CARD.get(anchor.chat.id)
    if tracked is None:
        return False

    message_id, has_media = tracked
    anchor_message_id = getattr(anchor, "message_id", None)
    if anchor_message_id is not None and anchor_message_id != message_id:
        # If the user clicked an older inline button or a notification CTA,
        # update that exact message instead of silently editing some other
        # tracked card elsewhere in the chat history.
        return False

    try:
        if media_file is not None and has_media:
            await anchor.bot.edit_message_media(
                media=InputMediaPhoto(media=FSInputFile(str(media_file)), caption=text, parse_mode=parse_mode),
                chat_id=anchor.chat.id,
                message_id=message_id,
                reply_markup=reply_markup,
            )
            CLIENT_LAST_CARD[anchor.chat.id] = (message_id, True)
            return True
        if media_file is None and not has_media:
            await anchor.bot.edit_message_text(
                text,
                chat_id=anchor.chat.id,
                message_id=message_id,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            CLIENT_LAST_CARD[anchor.chat.id] = (message_id, False)
            return True
        if media_file is None and has_media:
            await anchor.bot.edit_message_caption(
                chat_id=anchor.chat.id,
                message_id=message_id,
                caption=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            CLIENT_LAST_CARD[anchor.chat.id] = (message_id, True)
            return True
    except TelegramBadRequest:
        return False
    return False


def agreement_text(*, consent_accepted: bool = False, agreement_link_available: bool = False) -> str:
    intro = (
        "Согласие уже подтверждено. Полный документ можно открыть по кнопке ниже."
        if consent_accepted
        else "Откройте полный текст соглашения по кнопке ниже и подтвердите согласие, чтобы открыть доступ к меню и управлению VPN."
    )
    tail = (
        "\n\nЕсли ссылка не открывается, напишите в поддержку."
        if agreement_link_available
        else "\n\nЕсли ссылка пока недоступна, напишите в поддержку."
    )
    return (
        "Пользовательское соглашение\n\n"
        f"{intro}\n\n"
        "Подтверждая соглашение, вы соглашаетесь с тем, что:\n"
        "1. Используете сервис на свой аккаунт.\n"
        "2. Самостоятельно отвечаете за безопасность своих устройств.\n"
        "3. Соблюдаете правила Telegram и законодательства вашей страны.\n"
        "4. Соблюдаете правила использования сервиса и подключений."
        f"{tail}"
    )


def channel_subscription_text(*, consent_ok: bool, channel_ok: bool, settings, legal_url: str | None = None) -> str:
    status = (
        "Подписка подтверждена. Можно продолжать работу в боте."
        if channel_ok
        else "Подпишитесь на канал и нажмите кнопку проверки подписки."
    )
    agreement_link = legal_url or agreement_url(settings)
    agreement_note = (
        "Продолжая пользоваться ботом, вы автоматически соглашаетесь с пользовательским соглашением"
        f": {agreement_link}"
        if agreement_link
        else "Продолжая пользоваться ботом, вы автоматически соглашаетесь с пользовательским соглашением."
    )
    return (
        "Шаг 1 из 2. Подписка на канал\n\n"
        f"{status}\n"
        "Без подписки бот не откроет меню и действия с VPN.\n"
        f"Канал проекта: {settings.required_subscription_channel_url or settings.required_subscription_channel or 'ссылка будет добавлена позже'}\n\n"
        f"{agreement_note}"
    )


def promo_onboarding_text() -> str:
    return (
        "Шаг 2 из 2. Промокод\n\n"
        "Если у вас есть промокод, можно ввести его сейчас и сразу получить бонус или скидку.\n"
        "Если промокода нет, просто нажмите кнопку «Пропустить». Потом промокод всё равно можно будет ввести в разделе «Баланс»."
    )


def promo_code_prompt_text(*, onboarding: bool = False) -> str:
    if onboarding:
        return (
            "Введите промокод одним сообщением, например: START100\n\n"
            "Если промокода нет, можно нажать «Пропустить»."
        )
    return "Введите промокод одним сообщением, например: START100"


def access_links_text(settings) -> str:
    links: list[str] = []
    site_link = site_public_url(settings)
    portal_link = portal_public_url(settings)
    if site_link:
        links.append(f"Сайт: {site_link}")
    if portal_link:
        links.append(f"Кабинет: {portal_link}")
    return "\n".join(links) if links else "Ссылки появятся после настройки публичных адресов."


async def create_portal_autologin_url(hub, settings, user_id: str) -> str | None:
    if portal_public_url(settings) is None:
        return None
    try:
        attempt = await hub.portal_auth.create_login_attempt()
        await hub.portal_auth.approve_login_attempt(attempt.token, user_id)
    except Exception as exc:
        logger.warning("Failed to create portal autologin URL for user %s: %s", user_id, exc)
        return None
    return portal_login_resume_url(settings, attempt.token)


def start_whitelist_notice_lines(subscription) -> list[str]:
    plan_code = getattr(subscription.plan, "code", None) if subscription and subscription.plan else None
    if not (plan_code and is_metered_plan_code(plan_code)):
        return []
    whitelist_used_gb = max(int(getattr(subscription, "whitelist_traffic_used_bytes", 0) or 0), 0) / 1024**3
    whitelist_charged_rub = bytes_to_gb_cost(
        max(int(getattr(subscription, "whitelist_traffic_billed_bytes", 0) or 0), 0),
        WHITELIST_GB_PRICE_RUB,
    )
    return [
        "⚠️ Start: белые списки тарифицируются отдельно — 4 ₽/ГБ.",
        f"БС: {whitelist_used_gb:.2f} ГБ • списано {whitelist_charged_rub:.2f} ₽",
    ]


def home_text(user, subscription, settings, latest_subscription=None) -> str:
    if subscription:
        lines = [
            "🏠 Главное меню",
            "",
            f"Текущий тариф: {subscription.plan.name}",
            f"Формат списания: {billing_cycle_label(subscription.plan)}",
            f"Баланс: {Decimal(user.balance_rub):.2f} ₽",
            *start_whitelist_notice_lines(subscription),
            "✨ Всё управление VPN доступно кнопками ниже.",
        ]
        if subscription.notes:
            lines.extend(["", subscription.notes])
        lines.append("")
        lines.append("Выберите нужный раздел кнопками ниже.")
        return "\n".join(lines)

    if user.status == "blocked" or (latest_subscription and getattr(latest_subscription, "status", None) == "blocked"):
        latest_plan = getattr(latest_subscription, "plan", None)
        if latest_plan is not None and getattr(latest_plan, "is_trial", False):
            lines = [
                "🏠 Главное меню",
                "",
                f"Баланс: {Decimal(user.balance_rub):.2f} ₽",
                "Тестовый период завершён, поэтому доступ сейчас остановлен.",
                "",
                "Что делать дальше:",
                "1. Откройте «Подписка».",
                "2. Нажмите «Выбрать тариф».",
                "3. Если средств не хватает, сначала пополните баланс.",
            ]
            return "\n".join(lines)
        lines = [
            "🏠 Главное меню",
            "",
            f"Баланс: {Decimal(user.balance_rub):.2f} ₽",
            "Доступ сейчас остановлен. Обычно это означает, что закончился баланс для продления.",
            "",
            "Пополните баланс и заново выберите тариф или включите продление.",
        ]
        return "\n".join(lines)

    lines = [
        "🏠 Главное меню",
        "",
        f"Баланс: {Decimal(user.balance_rub):.2f} ₽",
        "Тариф пока не выбран. Нажмите «Подписка», затем «Выбрать тариф», чтобы активировать Start / Pro или запустить тест на 2 дня.",
        "",
        "🚀 Личный кабинет открывается отдельной кнопкой ниже.",
    ]
    return "\n".join(lines)


def profile_text(user, subscription, settings) -> str:
    plan_name = subscription.plan.name if subscription and subscription.plan else "не выбран"
    lines = [
        "👤 Профиль",
        "",
        f"💳 Баланс: {Decimal(user.balance_rub):.2f} ₽",
        f"Тариф: {plan_name}",
    ]
    if subscription:
        lines.extend(start_whitelist_notice_lines(subscription))
        if subscription.plan and not subscription.plan.is_trial:
            auto_renew = "включено" if subscription.auto_renew else "отключено"
            lines.append(f"Автопродление: {auto_renew}")
            lines.append(f"Следующее списание: {subscription.next_billing_at:%d.%m.%Y %H:%M}")
        else:
            lines.append(f"Действует до: {subscription.next_billing_at:%d.%m.%Y %H:%M}")
    return "\n".join(lines)


def subscription_text(bundle: dict, user_servers: list, settings, latest_subscription=None, activity_summary: dict | None = None) -> str:
    user = bundle["user"]
    subscription = bundle.get("subscription")
    subscription_for_state = subscription or latest_subscription
    if not subscription_for_state:
        return (
            "🔐 Подписка\n\n"
            "У вас пока нет активного тарифа.\n"
            "Выберите Start или Pro, либо запустите тест на 2 дня.\n\n"
            f"{access_links_text(settings)}"
        )

    if subscription is None:
        latest_plan = getattr(subscription_for_state, "plan", None)
        if latest_plan is not None and getattr(latest_plan, "is_trial", False):
            return (
                "🔐 Подписка\n\n"
                "Пробный период уже завершён.\n"
                f"Баланс: {Decimal(user.balance_rub):.2f} ₽\n\n"
                "Чтобы вернуться в сеть, откройте «Выбрать тариф» и активируйте Start или Pro.\n"
                "Если на балансе не хватает средств, сначала пополните его.\n\n"
                f"{access_links_text(settings)}"
            )
        return (
            "🔐 Подписка\n\n"
            f"Последний тариф: {subscription_for_state.plan.name if subscription_for_state.plan else 'не выбран'}\n"
            f"Статус: {subscription_for_state.status}\n"
            f"Баланс: {Decimal(user.balance_rub):.2f} ₽\n\n"
            "Сейчас доступа нет. Пополните баланс и выберите тариф заново.\n\n"
            f"{access_links_text(settings)}"
        )

    server_lines = []
    whitelist_servers = 0
    standard_servers = 0
    for access in user_servers:
        if not access.server:
            continue
        type_label = {
            "ten_gbit": "Start",
            "whitelist": "Белые списки",
            "regular": "Обычный",
        }[access.server.server_type.value]
        if access.server.server_type.value == "whitelist":
            whitelist_servers += 1
        else:
            standard_servers += 1
        server_lines.append(f"{access.server.name} • {type_label} • {access.status}")

    lines = [
        "🔐 Подписка",
        "",
        f"✨ Статус: {user.status}",
        f"🧾 Тариф: {subscription.plan.name}",
        f"🔁 Автопродление: {'включено' if subscription.auto_renew else 'отключено'}",
        f"📅 Следующее списание: {subscription.next_billing_at:%d.%m.%Y %H:%M}",
        f"📱 Лимит устройств: {device_limit_label(subscription.plan)}",
    ]
    if subscription.notes:
        lines.extend(["", subscription.notes])
    if show_metered_usage(subscription):
        charged_whitelist_cost = billed_whitelist_cost(subscription)
        pending_whitelist_cost = outstanding_whitelist_cost(subscription)
        lines.extend(
            [
                f"Общий трафик: {subscription.traffic_used_bytes / 1024**3:.2f} ГБ",
                f"Трафик по белым спискам: {subscription.whitelist_traffic_used_bytes / 1024**3:.2f} ГБ",
                f"Уже списано за белые списки: {charged_whitelist_cost:.2f} ₽",
            ]
        )
        if pending_whitelist_cost > Decimal("0.00"):
            lines.append(f"Осталось удержать после пополнения: {pending_whitelist_cost:.2f} ₽")
    lines.extend(
        [
            "",
            "🌐 Доступные серверы:",
            "\n".join(server_lines) if server_lines else "Пока нет активных серверов.",
        ]
    )
    return "\n".join(lines)


def plan_menu_text() -> str:
    return as_list(
        Bold("🌍 Тарифы ALTLINK"),
        as_list(
            Bold("Start"),
            as_marked_list(
                "🔹 Один основной сервер",
                "📱 До 2 устройств",
                "∞ Безлимитный трафик на основном сервере",
                "⚡ Один случайный высокоскоростной сервер",
                "🛡️ Белые списки доступны отдельно: 4 ₽ за 1 ГБ",
                marker="• ",
            ),
            sep="\n",
        ),
        as_list(
            Bold("Pro"),
            as_marked_list(
                "🚀 Все активные серверы",
                "📱 До 8 устройств",
                "∞ Безлимитный трафик на всех серверах",
                "🌐 Разные локации и серверы со скоростью до 10 Гбит/с",
                "🛡️ Поддержка режима белых списков",
                marker="• ",
            ),
            sep="\n",
        ),
        as_list(
            Bold("Что такое режим белых списков"),
            "Это режим для ситуаций, когда на мобильном интернете работают только отдельные российские сервисы, а часть сайтов и приложений не открывается. ALTLINK помогает вернуть доступ к привычным сервисам!",
            sep="\n",
        ),
        "Подсказка по меткам: ⚡ — высокоскоростной сервер, «БС» — сервер белых списков.",
        "Выберите тариф, а затем срок подключения.",
        sep="\n\n",
    ).as_html()


def plan_family_text(family: str) -> str:
    if family == "10gbit":
        return as_list(
            Bold("Start"),
            as_marked_list(
                "🔹 Один основной сервер",
                "📱 До 2 устройств",
                "∞ Безлимитный трафик на основном сервере",
                "⚡ Один случайный высокоскоростной сервер",
                "🏷️ В интерфейсе он отмечен ⚡",
                marker="• ",
            ),
            as_list(
                Bold("Что такое режим белых списков"),
                "Это режим для ситуаций, когда на мобильном интернете работают только отдельные российские сервисы, а часть сайтов и приложений не открывается. ALTLINK помогает вернуть доступ к привычным сервисам!",
                sep="\n",
            ),
            "🛡️ Для Start трафик через белые списки считается отдельно: 4 ₽ за 1 ГБ.",
            as_list(
                Bold("Стоимость"),
                f"На месяц: {SINGLE_10GBIT_MONTHLY_PRICE_RUB} ₽",
                f"На неделю: {SINGLE_10GBIT_WEEKLY_PRICE_RUB} ₽",
                sep="\n",
            ),
            sep="\n\n",
        ).as_html()

    return as_list(
        Bold("Pro"),
        as_marked_list(
            "🚀 Все активные серверы",
            "📱 До 8 устройств",
            "∞ Безлимитный трафик на всех серверах",
            "🌐 Разные локации для выбора под ваш маршрут",
            "⚡ Серверы сети рассчитаны на скорость до 10 Гбит/с",
            "🛡️ Поддержка режима белых списков",
            marker="• ",
        ),
        as_list(
            Bold("Что такое режим белых списков"),
            "Это режим для ситуаций, когда на мобильном интернете работают только отдельные российские сервисы, а часть сайтов и приложений не открывается. ALTLINK помогает вернуть доступ к привычным сервисам!",
            sep="\n",
        ),
        as_list(
            Bold("Стоимость"),
            f"На месяц: {UNLIMITED_MONTHLY_PRICE_RUB} ₽",
            f"На неделю: {UNLIMITED_WEEKLY_PRICE_RUB} ₽",
            sep="\n",
        ),
        sep="\n\n",
    ).as_html()


def format_rub_compact(amount: Decimal) -> str:
    normalized = quantize_money(Decimal(amount))
    if normalized == normalized.to_integral():
        return f"{int(normalized)} ₽"
    return f"{normalized:.2f} ₽"


def format_percent_compact(value: Decimal) -> str:
    normalized = quantize_money(Decimal(value))
    rendered = format(normalized.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return f"{rendered}%"


def plan_family_base_prices(family: str) -> tuple[Decimal, Decimal]:
    if family == "10gbit":
        return SINGLE_10GBIT_MONTHLY_PRICE_RUB, SINGLE_10GBIT_WEEKLY_PRICE_RUB
    return UNLIMITED_MONTHLY_PRICE_RUB, UNLIMITED_WEEKLY_PRICE_RUB


def discounted_amount(amount: Decimal, percent: Decimal) -> Decimal:
    safe_percent = min(max(Decimal(percent), Decimal("0")), Decimal("100"))
    return quantize_money(Decimal(amount) - quantize_money((Decimal(amount) * safe_percent) / Decimal("100")))


def plan_price_line(label: str, amount: Decimal, *, percent: Decimal | None = None) -> str:
    if not percent or Decimal(percent) <= Decimal("0"):
        return f"{label}: {format_rub_compact(amount)}"
    discounted = discounted_amount(amount, Decimal(percent))
    return Text(
        f"{label}: ",
        Strikethrough(format_rub_compact(amount)),
        f" {format_rub_compact(discounted)}",
    )


def plan_button_price_text(label: str, amount: Decimal, *, percent: Decimal | None = None) -> str:
    if not percent or Decimal(percent) <= Decimal("0"):
        return f"{label} • {format_rub_compact(amount)}"
    safe_percent = min(max(Decimal(percent), Decimal("0")), Decimal("100"))
    discounted = discounted_amount(amount, safe_percent)
    return f"{label} • {format_rub_compact(discounted)} (-{format_percent_compact(safe_percent)})"


async def resolve_plan_discount_preview(hub, user_id: str, family: str) -> dict[str, object] | None:
    monthly_price, weekly_price = plan_family_base_prices(family)
    try:
        _, promo, _ = await hub.promos.calculate_discount(user_id, monthly_price)
    except Exception:
        logger.warning("Failed to calculate plan discount preview for user %s and family %s", user_id, family, exc_info=True)
        return None
    if promo is None:
        return None

    percent = min(max(Decimal(promo.reward_value), Decimal("0")), Decimal("100"))
    if percent <= Decimal("0"):
        return None

    return {
        "code": promo.code,
        "percent": percent,
        "monthly_line": plan_price_line("На месяц", monthly_price, percent=percent),
        "weekly_line": plan_price_line("На неделю", weekly_price, percent=percent),
        "monthly_button": plan_button_price_text("На месяц", monthly_price, percent=percent),
        "weekly_button": plan_button_price_text("На неделю", weekly_price, percent=percent),
    }


def plan_menu_text() -> str:
    return as_list(
        Bold("🌍 Тарифы ALTLINK"),
        as_list(
            Bold("Start"),
            as_marked_list(
                f"💸 От {SINGLE_10GBIT_WEEKLY_PRICE_RUB} ₽ в неделю или {SINGLE_10GBIT_MONTHLY_PRICE_RUB} ₽ в месяц",
                "⚡ Один случайный высокоскоростной сервер",
                "📱 До 2 устройств",
                "∞ Безлимитный трафик на основном сервере",
                "🛡️ Белые списки доступны отдельно: 4 ₽ за 1 ГБ",
                marker="• ",
            ),
            sep="\n",
        ),
        as_list(
            Bold("Pro"),
            as_marked_list(
                f"💸 От {UNLIMITED_WEEKLY_PRICE_RUB} ₽ в неделю или {UNLIMITED_MONTHLY_PRICE_RUB} ₽ в месяц",
                "🚀 Все активные серверы",
                "📱 До 8 устройств",
                "∞ Безлимитный трафик на всех серверах",
                "🌐 Разные локации и серверы со скоростью до 10 Гбит/с",
                "🛡️ Поддержка режима белых списков",
                marker="• ",
            ),
            sep="\n",
        ),
        as_list(
            Bold("Что такое режим белых списков"),
            "Это режим для ситуаций, когда на мобильном интернете работают только отдельные российские сервисы, а часть сайтов и приложений не открывается. ALTLINK помогает вернуть доступ к привычным сервисам!",
            sep="\n",
        ),
        "Подсказка по меткам: ⚡ — высокоскоростной сервер, «БС» — сервер белых списков.",
        "Выберите тариф, а затем срок подключения.",
        sep="\n\n",
    ).as_html()


def plan_family_text(family: str, *, discount_preview: dict[str, object] | None = None) -> str:
    price_lines = (
        [
            discount_preview["monthly_line"],
            discount_preview["weekly_line"],
        ]
        if discount_preview
        else None
    )
    promo_block = (
        as_list(
            Bold(f"🎟 Промокод {discount_preview['code']} активен"),
            f"Скидка {format_percent_compact(Decimal(discount_preview['percent']))} уже включена в цены ниже.",
            sep="\n",
        )
        if discount_preview
        else None
    )
    if family == "10gbit":
        return as_list(
            Bold("Start"),
            as_marked_list(
                f"💸 От {SINGLE_10GBIT_WEEKLY_PRICE_RUB} ₽ в неделю или {SINGLE_10GBIT_MONTHLY_PRICE_RUB} ₽ в месяц",
                "⚡ Один случайный высокоскоростной сервер",
                "📱 До 2 устройств",
                "∞ Безлимитный трафик на основном сервере",
                marker="• ",
            ),
            "📍 В интерфейсе он отмечен ⚡.",
            as_list(
                Bold("Что такое режим белых списков"),
                "Это режим для ситуаций, когда на мобильном интернете работают только отдельные российские сервисы, а часть сайтов и приложений не открывается. ALTLINK помогает вернуть доступ к привычным сервисам!",
                sep="\n",
            ),
            "🛡️ Для Start трафик через белые списки считается отдельно: 4 ₽ за 1 ГБ.",
            as_list(
                Bold("Стоимость"),
                *(price_lines or [
                    f"На месяц: {SINGLE_10GBIT_MONTHLY_PRICE_RUB} ₽",
                    f"На неделю: {SINGLE_10GBIT_WEEKLY_PRICE_RUB} ₽",
                ]),
                sep="\n",
            ),
            *([promo_block] if promo_block else []),
            sep="\n\n",
        ).as_html()

    return as_list(
        Bold("Pro"),
        as_marked_list(
            f"💸 От {UNLIMITED_WEEKLY_PRICE_RUB} ₽ в неделю или {UNLIMITED_MONTHLY_PRICE_RUB} ₽ в месяц",
            "🚀 Все активные серверы",
            "📱 До 8 устройств",
            "∞ Безлимитный трафик на всех серверах",
            "🌐 Разные локации для выбора под ваш маршрут",
            "⚡ Серверы сети рассчитаны на скорость до 10 Гбит/с",
            "🛡️ Поддержка режима белых списков",
            marker="• ",
        ),
        as_list(
            Bold("Что такое режим белых списков"),
            "Это режим для ситуаций, когда на мобильном интернете работают только отдельные российские сервисы, а часть сайтов и приложений не открывается. ALTLINK помогает вернуть доступ к привычным сервисам!",
            sep="\n",
        ),
        as_list(
            Bold("Стоимость"),
            *(price_lines or [
                f"На месяц: {UNLIMITED_MONTHLY_PRICE_RUB} ₽",
                f"На неделю: {UNLIMITED_WEEKLY_PRICE_RUB} ₽",
            ]),
            sep="\n",
        ),
        *([promo_block] if promo_block else []),
        sep="\n\n",
    ).as_html()


def support_text(settings) -> str:
    return (
        "🛟 Поддержка\n\n"
        f"Аккаунт поддержки: {support_username_label(settings)}\n\n"
        "Если VPN не работает, нажмите кнопку ниже и опишите проблему одним сообщением.\n"
        "Если заметили баг или странное поведение сервиса, тоже обязательно сообщите об этом в поддержку."
    )


def normalize_action_text(text: str | None) -> str:
    return " ".join((text or "").replace("\u200b", " ").split()).casefold()


def resolve_main_action(text: str | None) -> str | None:
    return {
        "меню": "menu",
        "поддержка": "support",
        "помощь": "support",
        "сайт": "site",
        "профиль": "profile",
        "баланс": "balance",
        "кошелек": "balance",
        "кошелёк": "balance",
        "подписка": "subscription",
        "серверы": "subscription",
    }.get(normalize_action_text(text))


async def get_access_state(message: Message | CallbackQuery, container: AppContainer, hub=None):
    user = await ensure_user(message.from_user, container, hub)
    consent_ok = bool(user.registration_completed_at and user.consent_accepted_at)
    channel_ok = not container.settings.required_subscription_channel or bool(user.channel_verified_at)
    if not channel_ok:
        channel_ok = await is_channel_member(user.telegram_id, container)
        if channel_ok and hub is not None:
            user = await hub.accounts.mark_channel_verified(user.id)
    if channel_ok and not consent_ok and hub is not None:
        user = await hub.accounts.complete_registration(user.id)
        consent_ok = True
    return user, consent_ok, channel_ok


def needs_promo_onboarding(user, hub) -> bool:
    return not hub.accounts.has_completed_promo_onboarding(user)


async def send_reply_menu(target: Message | CallbackQuery, *, force: bool = False) -> None:
    anchor = target.message if isinstance(target, CallbackQuery) else target
    chat = getattr(anchor, "chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        chat_id = getattr(anchor, "chat_id", None)
    if chat_id is None:
        from_user = getattr(anchor, "from_user", None)
        chat_id = getattr(from_user, "id", None)
    if not force and chat_id in CLIENT_REPLY_MENU_READY:
        return
    await anchor.answer("\u2060", reply_markup=main_menu())
    if chat_id is not None:
        CLIENT_REPLY_MENU_READY.add(chat_id)


async def show_pending_access_steps(
    target: Message | CallbackQuery,
    container: AppContainer,
    *,
    consent_ok: bool,
    channel_ok: bool,
) -> None:
    anchor = target.message if isinstance(target, CallbackQuery) else target
    if not (consent_ok and channel_ok):
        legal_url = agreement_url(container.settings)
        await send_card_with_optional_media(
            anchor,
            channel_subscription_text(
                consent_ok=consent_ok,
                channel_ok=channel_ok,
                settings=container.settings,
                legal_url=legal_url,
            ),
            primary_markup=channel_actions(
                container.settings.required_subscription_channel_url or None,
                agreement_url=legal_url,
            ).as_markup(),
            media_section="onboarding",
            force_new_message=True,
        )
    if isinstance(target, CallbackQuery):
        await target.answer()


async def show_promo_onboarding_step(target: Message | CallbackQuery) -> None:
    await send_card_with_optional_media(
        target,
        promo_onboarding_text(),
        primary_markup=promo_onboarding_actions().as_markup(),
        media_section="onboarding",
        force_new_message=not isinstance(target, CallbackQuery),
    )


async def ensure_client_access(message: Message | CallbackQuery, container: AppContainer, hub=None):
    if await client_maintenance_active(container, message.from_user.id, hub):
        await show_technical_maintenance(message, container)
        return None
    user, consent_ok, channel_ok = await get_access_state(message, container, hub)
    if channel_ok and consent_ok and hub is not None and needs_promo_onboarding(user, hub):
        await show_promo_onboarding_step(message)
        return None
    if channel_ok and consent_ok:
        return user

    await show_pending_access_steps(
        message,
        container,
        consent_ok=consent_ok,
        channel_ok=channel_ok,
    )
    return None


async def sync_visible_trial_state(hub, user_id: str) -> None:
    billing = getattr(hub, "billing", None)
    if billing is None:
        return
    sync_trial_state = getattr(billing, "sync_user_trial_state", None)
    if sync_trial_state is None:
        return
    try:
        await sync_trial_state(user_id)
    except Exception:
        # The screen should still open even if the background state sync
        # temporarily fails; the next billing pass will retry it.
        return


async def show_home(target: Message | CallbackQuery, container: AppContainer, hub) -> None:
    user = await ensure_user(target.from_user, container, hub)
    await sync_visible_trial_state(hub, user.id)
    subscription = await hub.accounts.get_current_subscription(user.id)
    latest_subscription = await hub.accounts.get_latest_subscription(user.id) if subscription is None else subscription
    show_trial = await hub.accounts.can_offer_trial(user.id)
    await send_home_card(
        target,
        container,
        user=user,
        subscription=subscription,
        show_trial=show_trial,
        latest_subscription=latest_subscription,
        as_new_message=not isinstance(target, CallbackQuery),
    )


async def show_profile(target: Message | CallbackQuery, container: AppContainer, hub) -> None:
    user = await ensure_user(target.from_user, container, hub)
    await sync_visible_trial_state(hub, user.id)
    subscription = await hub.accounts.get_current_subscription(user.id)
    portal_url = await create_portal_autologin_url(hub, container.settings, user.id)
    await send_card_with_optional_media(
        target,
        profile_text(user, subscription, container.settings),
        primary_markup=profile_actions(
            agreement_url=agreement_url(container.settings),
            privacy_url=privacy_url(container.settings),
            share_url=referral_share_vpn_url(container.settings, getattr(user, "referral_code", None)),
            portal_url=portal_url,
        ).as_markup(),
        media_section="profile",
        force_new_message=not isinstance(target, CallbackQuery),
    )


async def show_subscription(target: Message | CallbackQuery, container: AppContainer, hub) -> None:
    user = await ensure_user(target.from_user, container, hub)
    await sync_visible_trial_state(hub, user.id)
    await hub.billing.refresh_subscription_traffic(user.id)
    bundle = await hub.accounts.get_subscription_bundle(user.id)
    user_servers = await hub.catalog.get_user_servers(user.id)
    subscription = bundle.get("subscription")
    latest_subscription = await hub.accounts.get_latest_subscription(user.id) if subscription is None else subscription
    activity_summary = await hub.online.get_user_activity_summary(user.id) if getattr(hub, "online", None) else None
    await send_card_with_optional_media(
        target,
        subscription_text(
            bundle,
            user_servers,
            container.settings,
            latest_subscription=latest_subscription,
            activity_summary=activity_summary,
        ),
        primary_markup=subscription_markup(subscription),
        media_section="subscription",
        force_new_message=not isinstance(target, CallbackQuery),
    )


async def show_support(target: Message | CallbackQuery, container: AppContainer, hub) -> None:
    await ensure_user(target.from_user, container, hub)
    await send_card_with_optional_media(
        target,
        support_text(container.settings),
        primary_markup=support_actions(support_profile_url(container.settings)).as_markup(),
        media_section="support",
        force_new_message=not isinstance(target, CallbackQuery),
    )


async def show_balance(target: Message | CallbackQuery, container: AppContainer, hub) -> None:
    user = await ensure_user(target.from_user, container, hub)
    requests = await hub.topups.list_requests(user_id=user.id)
    pending_requests = len([item for item in requests if str(item.status) == "new"])
    _, pending_promo, _ = await hub.promos.calculate_discount(user.id, Decimal("100"))
    configured_provider = hub.topups.configured_provider()
    resolved_provider = hub.topups.resolved_provider()
    missing_settings = hub.topups.yookassa_missing_settings()
    promo_line = ""
    response_parse_mode = None
    if pending_promo is not None:
        promo_line = (
            f"Активный промокод: <code>{pending_promo.code}</code> • "
            f"скидка {format_percent_compact(Decimal(pending_promo.reward_value))} на следующий тариф\n"
        )
        response_parse_mode = "HTML"
    await send_card_with_optional_media(
        target,
        (
            "💳 Баланс\n\n"
            f"На счёте: {Decimal(user.balance_rub):.2f} ₽\n"
            f"Платежей в истории: {len(requests)}\n"
            f"Ожидают подтверждения: {pending_requests}\n"
            f"Ваш реферальный код: {getattr(user, 'referral_code', 'будет создан позже')}\n"
            f"{promo_line}\n"
            f"{balance_topup_status_text(configured_provider=configured_provider, resolved_provider=resolved_provider, missing_settings=missing_settings)}\n"
            "Промокод можно ввести кнопкой ниже, а реферальную ссылку открыть отдельно."
        ),
        primary_markup=balance_actions().as_markup(),
        media_section="balance",
        force_new_message=not isinstance(target, CallbackQuery),
        parse_mode=response_parse_mode,
    )


def build_home_markup(
    *,
    settings,
    show_trial: bool,
    referral_code: str | None = None,
    allow_share: bool = True,
    portal_url: str | None = None,
):
    share_url = referral_share_vpn_url(settings, referral_code) if allow_share else None
    return menu_actions(show_trial=show_trial, share_url=share_url, portal_url=portal_url).as_markup()


async def send_home_card(
    target: Message | CallbackQuery,
    container: AppContainer,
    *,
    user,
    subscription,
    show_trial: bool,
    latest_subscription=None,
    as_new_message: bool = False,
) -> None:
    text = home_text(user, subscription, container.settings, latest_subscription=latest_subscription)
    async with container.hub() as inner_hub:
        portal_url = await create_portal_autologin_url(inner_hub, container.settings, user.id)
    primary_markup = build_home_markup(
        settings=container.settings,
        show_trial=show_trial,
        referral_code=getattr(user, "referral_code", None),
        allow_share=True,
        portal_url=portal_url,
    )
    fallback_markup = build_home_markup(
        settings=container.settings,
        show_trial=show_trial,
        referral_code=getattr(user, "referral_code", None),
        allow_share=False,
        portal_url=portal_url,
    )
    await send_card_with_optional_media(
        target,
        text,
        primary_markup=primary_markup,
        fallback_markup=fallback_markup,
        media_section="home",
        force_new_message=as_new_message,
    )


async def perform_main_action(action: str, message: Message, container: AppContainer, hub) -> None:
    if action == "menu":
        await show_home(message, container, hub)
        return
    if action == "support":
        await show_support(message, container, hub)
        return
    if action == "profile":
        await show_profile(message, container, hub)
        return
    if action == "balance":
        await show_balance(message, container, hub)
        return
    if action == "subscription":
        await show_subscription(message, container, hub)
        return
    await site_link(message, container)


async def route_message_action(message: Message, state: FSMContext, container: AppContainer) -> bool:
    action = resolve_main_action(message.text)
    if action is None:
        return False

    await state.clear()
    if action == "site":
        if message.chat.id not in CLIENT_REPLY_MENU_READY:
            await send_reply_menu(message)
        await site_link(message, container)
        return True

    async with container.hub() as hub:
        user = await ensure_client_access(message, container, hub)
        if user is None:
            return True
        if message.chat.id not in CLIENT_REPLY_MENU_READY:
            await send_reply_menu(message)
        await perform_main_action(action, message, container, hub)
    return True


@router.message(CommandStart())
async def start(message: Message, container: AppContainer):
    async with container.hub() as hub:
        if await client_maintenance_active(container, message.from_user.id, hub):
            await show_technical_maintenance(message, container)
            return
        provisional_user = await ensure_user(message.from_user, container, hub)
        start_payload = (message.text or "").split(maxsplit=1)
        if len(start_payload) > 1 and start_payload[1].startswith("login_"):
            token = start_payload[1].removeprefix("login_").strip()
            attempt = await hub.portal_auth.get_login_attempt(token)
            status_name = hub.portal_auth.login_attempt_status(attempt)
            if attempt is None:
                await answer_or_edit(message, "Попытка входа не найдена. Вернитесь на сайт и начните вход заново.")
                return
            if status_name == "expired":
                await answer_or_edit(
                    message,
                    "Эта попытка входа уже истекла. Вернитесь на сайт и создайте новую попытку входа.",
                )
                return
            if status_name == "completed":
                await answer_or_edit(
                    message,
                    "Эта попытка входа уже использована. Если нужно, откройте сайт и начните вход заново.",
                )
                return
            if status_name == "canceled":
                await answer_or_edit(
                    message,
                    "Эта попытка входа уже отменена. Вернитесь на сайт и создайте новую попытку входа.",
                )
                return
            if status_name == "approved" and attempt.approved_user_id == provisional_user.id:
                await answer_or_edit(
                    message,
                    portal_login_confirmed_text(container.settings),
                    reply_markup=portal_login_complete_actions(
                        portal_login_resume_url(container.settings, token),
                    ).as_markup(),
                )
                return
            if status_name == "approved" and attempt.approved_user_id != provisional_user.id:
                await answer_or_edit(
                    message,
                    "Эта попытка входа уже подтверждена другим Telegram-аккаунтом. Вернитесь на сайт и создайте новую попытку входа.",
                )
                return
            await answer_or_edit(
                message,
                portal_login_request_text(container.settings),
                reply_markup=portal_login_actions(token).as_markup(),
            )
            return
        if len(start_payload) > 1 and start_payload[1].startswith("ref_"):
            await hub.accounts.bind_referrer(provisional_user.id, start_payload[1].removeprefix("ref_"))
        user = await ensure_client_access(message, container, hub)
        if user is None:
            return
        subscription = await hub.accounts.get_current_subscription(user.id)
        latest_subscription = await hub.accounts.get_latest_subscription(user.id) if subscription is None else subscription
        show_trial = await hub.accounts.can_offer_trial(user.id)
    await send_reply_menu(message, force=True)
    await send_home_card(
        message,
        container,
        user=user,
        subscription=subscription,
        show_trial=show_trial,
        latest_subscription=latest_subscription,
        as_new_message=True,
    )


@router.callback_query(F.data.startswith("client:portal_login_confirm:"))
async def portal_login_confirm(callback: CallbackQuery, container: AppContainer):
    token = (callback.data or "").split(":", 3)[-1].strip()
    async with container.hub() as hub:
        if await client_maintenance_active(container, callback.from_user.id, hub):
            await show_technical_maintenance(callback, container)
            return
        user = await ensure_user(callback.from_user, container, hub)
        try:
            await hub.portal_auth.approve_login_attempt(token, user.id)
        except (ConflictError, NotFoundError) as exc:
            await answer_or_edit(callback, str(exc))
            return
    open_url = portal_login_resume_url(container.settings, token)
    text = portal_login_confirmed_text(container.settings)
    reply_markup = portal_login_complete_actions(open_url).as_markup()
    if await try_edit_tracked_client_card(callback, text, reply_markup=reply_markup, media_file=None):
        if open_url:
            await callback.answer(url=open_url)
        else:
            await callback.answer()
        return
    try:
        result = await callback.message.edit_text(
            text,
            reply_markup=reply_markup,
        )
        remember_client_card(result, has_media=False)
    except TelegramBadRequest:
        result = await callback.message.answer(
            text,
            reply_markup=reply_markup,
        )
        remember_client_card(result, has_media=False)
    if open_url:
        await callback.answer(url=open_url)
    else:
        await callback.answer()


@router.callback_query(F.data.startswith("client:portal_login_cancel:"))
async def portal_login_cancel(callback: CallbackQuery, container: AppContainer):
    token = (callback.data or "").split(":", 3)[-1].strip()
    async with container.hub() as hub:
        try:
            await hub.portal_auth.cancel_login_attempt(token)
        except (ConflictError, NotFoundError) as exc:
            await answer_or_edit(callback, str(exc))
            return
    await answer_or_edit(callback, portal_login_canceled_text(container.settings))


@router.message(F.text.func(lambda value: resolve_main_action(value) == "menu"))
async def menu_reply(message: Message, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_client_access(message, container, hub)
        if user is None:
            return
        await perform_main_action("menu", message, container, hub)


@router.message(F.text.func(lambda value: resolve_main_action(value) == "support"))
async def support_reply(message: Message, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_client_access(message, container, hub)
        if user is None:
            return
        await perform_main_action("support", message, container, hub)


@router.message(F.text.func(lambda value: resolve_main_action(value) == "site"))
async def site_link(message: Message, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_client_access(message, container, hub)
        if user is None:
            return
        portal_url = await create_portal_autologin_url(hub, container.settings, user.id)
    result = await message.answer(
        "🌐 Сайт ALTLINK\n\n"
        f"Главная страница: {site_public_url(container.settings) or 'ещё не настроена'}\n"
        f"Личный кабинет: {portal_public_url(container.settings) or 'ещё не настроен'}\n\n"
        "На сайте можно посмотреть тарифы, подключение и войти в кабинет через Telegram.",
        reply_markup=site_actions(
            site_public_url(container.settings),
            portal_url,
        ).as_markup(),
    )
    remember_client_card(result, has_media=False)


@router.message(F.text.func(lambda value: resolve_main_action(value) == "profile"))
async def legacy_profile(message: Message, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_client_access(message, container, hub)
        if user is None:
            return
        await perform_main_action("profile", message, container, hub)


@router.message(F.text.func(lambda value: resolve_main_action(value) == "balance"))
async def legacy_balance(message: Message, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_client_access(message, container, hub)
        if user is None:
            return
        await perform_main_action("balance", message, container, hub)


@router.message(F.text.func(lambda value: resolve_main_action(value) == "subscription"))
async def legacy_subscription(message: Message, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_client_access(message, container, hub)
        if user is None:
            return
        await perform_main_action("subscription", message, container, hub)


@router.callback_query(F.data == "client:home")
async def home_callback(callback: CallbackQuery, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_client_access(callback, container, hub)
        if user is None:
            return
        await show_home(callback, container, hub)


@router.callback_query(F.data == "client:show_agreement")
async def show_agreement(callback: CallbackQuery, container: AppContainer):
    async with container.hub() as hub:
        if await client_maintenance_active(container, callback.from_user.id, hub):
            await show_technical_maintenance(callback, container)
            return
        user = await ensure_user(callback.from_user, container, hub)
        legal_url = agreement_url(container.settings)
        await answer_or_edit(
            callback,
            agreement_text(
                consent_accepted=bool(user.registration_completed_at and user.consent_accepted_at),
                agreement_link_available=bool(legal_url),
            ),
            reply_markup=agreement_actions(
                consent_accepted=bool(user.registration_completed_at and user.consent_accepted_at),
                agreement_url=legal_url,
            ).as_markup(),
        )


@router.callback_query(F.data == "client:check_channel")
async def check_channel(callback: CallbackQuery, container: AppContainer):
    async with container.hub() as hub:
        if await client_maintenance_active(container, callback.from_user.id, hub):
            await show_technical_maintenance(callback, container)
            return
        user, consent_ok, channel_ok = await get_access_state(callback, container, hub)
        subscription = await hub.accounts.get_current_subscription(user.id) if (consent_ok and channel_ok) else None
        show_trial = await hub.accounts.can_offer_trial(user.id) if (consent_ok and channel_ok) else False
    if not (consent_ok and channel_ok):
        await answer_or_edit(
            callback,
            channel_subscription_text(
                consent_ok=consent_ok,
                channel_ok=channel_ok,
                settings=container.settings,
                legal_url=agreement_url(container.settings),
            ),
            reply_markup=channel_actions(
                container.settings.required_subscription_channel_url or None,
                agreement_url=agreement_url(container.settings),
            ).as_markup(),
        )
        return
    if needs_promo_onboarding(user, hub):
        await show_promo_onboarding_step(callback)
        return
    await send_reply_menu(callback)
    await send_home_card(
        callback,
        container,
        user=user,
        subscription=subscription,
        show_trial=show_trial,
        as_new_message=False,
    )


@router.callback_query(F.data == "client:complete_registration")
async def complete_registration(callback: CallbackQuery, container: AppContainer):
    async with container.hub() as hub:
        if await client_maintenance_active(container, callback.from_user.id, hub):
            await show_technical_maintenance(callback, container)
            return
        user = await ensure_user(callback.from_user, container, hub)
        await hub.accounts.complete_registration(user.id)
        refreshed, consent_ok, channel_ok = await get_access_state(callback, container, hub)
        subscription = await hub.accounts.get_current_subscription(refreshed.id) if (consent_ok and channel_ok) else None
        show_trial = await hub.accounts.can_offer_trial(refreshed.id) if (consent_ok and channel_ok) else False
    await answer_or_edit(
        callback,
        agreement_text(consent_accepted=True, agreement_link_available=bool(agreement_url(container.settings))),
        reply_markup=agreement_actions(
            consent_accepted=True,
            agreement_url=agreement_url(container.settings),
        ).as_markup(),
    )
    if not channel_ok:
        await send_card_with_optional_media(
            callback,
            channel_subscription_text(
                consent_ok=consent_ok,
                channel_ok=False,
                settings=container.settings,
                legal_url=agreement_url(container.settings),
            ),
            primary_markup=channel_actions(
                container.settings.required_subscription_channel_url or None,
                agreement_url=agreement_url(container.settings),
            ).as_markup(),
            media_section="onboarding",
        )
        return
    if not consent_ok:
        return
    if needs_promo_onboarding(refreshed, hub):
        await show_promo_onboarding_step(callback)
        return
    await send_reply_menu(callback)
    await send_home_card(
        callback,
        container,
        user=refreshed,
        subscription=subscription,
        show_trial=show_trial,
        as_new_message=False,
    )


@router.callback_query(F.data == "client:balance")
async def balance_callback(callback: CallbackQuery, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_client_access(callback, container, hub)
        if user is None:
            return
        await show_balance(callback, container, hub)


@router.callback_query(F.data == "client:profile")
async def profile_callback(callback: CallbackQuery, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_client_access(callback, container, hub)
        if user is None:
            return
        await show_profile(callback, container, hub)


@router.callback_query(F.data == "client:subscription")
async def subscription_callback(callback: CallbackQuery, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_client_access(callback, container, hub)
        if user is None:
            return
        await show_subscription(callback, container, hub)


@router.callback_query(F.data == "client:support")
async def support_callback(callback: CallbackQuery, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_client_access(callback, container, hub)
        if user is None:
            return
        await show_support(callback, container, hub)


@router.callback_query(F.data == "client:support_issue")
async def support_issue_prompt(callback: CallbackQuery, state: FSMContext, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_client_access(callback, container, hub)
        if user is None:
            return
    await state.set_state(SupportStates.waiting_for_issue)
    await answer_or_edit(
        callback,
        "Опишите проблему одним сообщением.\n\n"
        "Например: когда перестал работать VPN, на каком устройстве это происходит и что уже пробовали сделать."
    )


@router.message(SupportStates.waiting_for_issue)
async def support_issue_submit(message: Message, state: FSMContext, container: AppContainer):
    if await route_message_action(message, state, container):
        return

    async with container.hub() as hub:
        user = await ensure_client_access(message, container, hub)
        if user is None:
            return
        try:
            item = await hub.support.create_request(user_id=user.id, message=message.text or "")
        except ConflictError as exc:
            await answer_or_edit(message, str(exc))
            return
        admin_telegram_ids = await hub.accounts.list_admin_telegram_ids()
        await state.clear()
    await notify_admins_about_support_request(
        container,
        user=user,
        request_id=item.id,
        message=item.message,
        admin_telegram_ids=admin_telegram_ids,
    )
    await answer_or_edit(
        message,
        "Запрос в поддержку создан.\n\n"
        f"Номер запроса: {item.id}\n"
        "Мы передали сообщение администраторам. Если нужно, дополнительно напишите в аккаунт поддержки.",
        reply_markup=support_actions(support_profile_url(container.settings)).as_markup(),
    )


@router.callback_query(F.data == "client:topup_menu")
async def topup_menu(callback: CallbackQuery, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_client_access(callback, container, hub)
        if user is None:
            return
    await answer_or_edit(
        callback,
        "Выберите сумму пополнения.",
        reply_markup=topup_actions().as_markup(),
    )


@router.callback_query(F.data.startswith("client:topup_amount:"))
async def topup_amount(callback: CallbackQuery, container: AppContainer):
    amount = Decimal(callback.data.split(":")[-1])
    async with container.hub() as hub:
        user = await ensure_client_access(callback, container, hub)
        if user is None:
            return
    await continue_topup_flow(callback, container, user=user, amount=amount)


@router.callback_query(F.data == "client:topup_custom")
async def topup_custom(callback: CallbackQuery, state: FSMContext, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_client_access(callback, container, hub)
        if user is None:
            return
    await state.set_state(TopupStates.waiting_for_amount)
    await answer_or_edit(
        callback,
        f"Введите сумму пополнения в рублях, например: 350\n\nМинимальная сумма — {MIN_TOPUP_AMOUNT_RUB:.0f} ₽.",
    )


@router.message(TopupStates.waiting_for_amount)
async def topup_custom_value(message: Message, state: FSMContext, container: AppContainer):
    if await route_message_action(message, state, container):
        return

    try:
        amount = Decimal((message.text or "").replace(",", "."))
    except (InvalidOperation, AttributeError):
        await answer_or_edit(message, "Не удалось распознать сумму. Попробуйте ещё раз.")
        return
    if amount < MIN_TOPUP_AMOUNT_RUB:
        await answer_or_edit(message, f"Минимальная сумма пополнения — {MIN_TOPUP_AMOUNT_RUB:.0f} ₽.")
        return

    async with container.hub() as hub:
        user = await ensure_client_access(message, container, hub)
        if user is None:
            return
    await state.clear()
    await continue_topup_flow(message, container, user=user, amount=amount)


@router.callback_query(F.data.startswith("client:topup_confirm_amount:"))
async def topup_confirm_amount(callback: CallbackQuery, container: AppContainer):
    await topup_menu(callback, container)


@router.callback_query(F.data.startswith("client:topup_provider_menu:"))
async def topup_provider_menu(callback: CallbackQuery, container: AppContainer):
    amount_token = callback.data.split(":")[-1]
    amount = parse_topup_amount_token(amount_token)
    async with container.hub() as hub:
        user = await ensure_client_access(callback, container, hub)
        if user is None:
            return
    await continue_topup_flow(callback, container, user=user, amount=amount)


@router.callback_query(F.data.startswith("client:topup_provider:"))
async def topup_provider_select(callback: CallbackQuery, container: AppContainer):
    _, _, provider_code, amount_token = callback.data.split(":", 3)
    amount = parse_topup_amount_token(amount_token)
    async with container.hub() as hub:
        user = await ensure_client_access(callback, container, hub)
        if user is None:
            return
        configured_provider = hub.topups.configured_provider()
        resolved_provider = hub.topups.resolved_provider()
        available_providers = available_topup_provider_codes(configured_provider, resolved_provider)
        if provider_code not in available_providers:
            await callback.answer("Этот способ оплаты сейчас недоступен.", show_alert=True)
            return
        try:
            checkout = await hub.topups.create_checkout(user.id, amount, provider_code=provider_code)
        except ConflictError as exc:
            await answer_or_edit(callback, str(exc), reply_markup=balance_actions().as_markup())
            return
        admin_telegram_ids = (
            await hub.accounts.list_admin_telegram_ids()
            if checkout.provider == "manual"
            else []
        )
    await handle_topup_checkout(
        callback,
        container,
        user=user,
        amount=amount,
        checkout=checkout,
        admin_telegram_ids=admin_telegram_ids,
    )


@router.callback_query(F.data.startswith("client:topup_check:"))
async def topup_check(callback: CallbackQuery, container: AppContainer):
    request_id = callback.data.split(":")[-1]
    async with container.hub() as hub:
        user = await ensure_client_access(callback, container, hub)
        if user is None:
            return
        request = await hub.topups.get_request(request_id)
        if request.user_id != user.id:
            await callback.answer("Проверять можно только свои платежи.", show_alert=True)
            return
        snapshot = await hub.topups.check_checkout_status(request_id)
    if snapshot.is_paid:
        text = (
            "✅ Оплата подтверждена.\n\n"
            f"Сумма: {Decimal(snapshot.request.amount_rub):.2f} ₽\n"
            f"Номер заявки: {snapshot.request.id}\n"
            "Баланс обновлён автоматически."
        )
        markup = balance_actions().as_markup()
    elif snapshot.provider == "yookassa":
        text = (
            "Статус оплаты\n\n"
            f"Сумма: {Decimal(snapshot.request.amount_rub):.2f} ₽\n"
            f"Номер заявки: {snapshot.request.id}\n"
            f"Статус: {topup_status_label(snapshot.external_status)}\n\n"
            "Если оплата уже завершена, подождите несколько секунд и нажмите проверку ещё раз."
        )
        markup = topup_checkout_actions(
            payment_url=snapshot.payment_url,
            request_id=snapshot.request.id,
            can_check=not snapshot.is_final,
        ).as_markup()
    else:
        text = (
            "Статус оплаты\n\n"
            f"Сумма: {Decimal(snapshot.request.amount_rub):.2f} ₽\n"
            f"Номер заявки: {snapshot.request.id}\n"
            f"Режим: {topup_provider_label(snapshot.provider)}\n"
            f"Статус: {topup_status_label(snapshot.external_status)}"
        )
        markup = balance_actions().as_markup()
    await answer_or_edit(callback, text, reply_markup=markup)


@router.callback_query(F.data == "client:my_topups")
async def my_topups(callback: CallbackQuery, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_client_access(callback, container, hub)
        if user is None:
            return
        requests = await hub.topups.list_requests(user_id=user.id)
        provider = hub.topups.resolved_provider()
    if not requests:
        text = "История платежей\n\nПлатежей пока нет."
        markup = balance_actions().as_markup()
    else:
        text = "История платежей\n\n" + "\n".join(
            [
                f"• {Decimal(item.amount_rub):.2f} ₽ — {topup_status_label(str(item.status))} — {item.created_at:%d.%m %H:%M}"
                for item in requests[:10]
            ]
        )
        latest_pending = next((item for item in requests if str(item.status) == "new"), None)
        if (getattr(latest_pending, "provider_code", "") or provider) == "yookassa" and latest_pending is not None:
            markup = topup_checkout_actions(
                payment_url=getattr(latest_pending, "external_payment_url", None),
                request_id=latest_pending.id,
                can_check=True,
            ).as_markup()
        else:
            markup = balance_actions().as_markup()
    await answer_or_edit(callback, text, reply_markup=markup)


@router.callback_query(F.data == "client:promo_prompt")
async def promo_prompt(callback: CallbackQuery, state: FSMContext, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_client_access(callback, container, hub)
        if user is None:
            return
    await state.update_data(promo_source="balance")
    await state.set_state(PromoStates.waiting_for_code)
    await answer_or_edit(callback, promo_code_prompt_text())


@router.callback_query(F.data == "client:onboarding_promo_prompt")
async def onboarding_promo_prompt(callback: CallbackQuery, state: FSMContext, container: AppContainer):
    async with container.hub() as hub:
        user, consent_ok, channel_ok = await get_access_state(callback, container, hub)
        if not (consent_ok and channel_ok):
            await show_pending_access_steps(callback, container, consent_ok=consent_ok, channel_ok=channel_ok)
            return
        if not needs_promo_onboarding(user, hub):
            subscription = await hub.accounts.get_current_subscription(user.id)
            latest_subscription = await hub.accounts.get_latest_subscription(user.id) if subscription is None else subscription
            show_trial = await hub.accounts.can_offer_trial(user.id)
            await send_reply_menu(callback)
            await send_home_card(
                callback,
                container,
                user=user,
                subscription=subscription,
                show_trial=show_trial,
                latest_subscription=latest_subscription,
                as_new_message=False,
            )
            return
    await state.update_data(promo_source="onboarding")
    await state.set_state(PromoStates.waiting_for_code)
    await answer_or_edit(
        callback,
        promo_code_prompt_text(onboarding=True),
        reply_markup=promo_onboarding_skip_actions().as_markup(),
    )


@router.callback_query(F.data == "client:onboarding_promo_skip")
async def onboarding_promo_skip(callback: CallbackQuery, state: FSMContext, container: AppContainer):
    async with container.hub() as hub:
        user, consent_ok, channel_ok = await get_access_state(callback, container, hub)
        if not (consent_ok and channel_ok):
            await show_pending_access_steps(callback, container, consent_ok=consent_ok, channel_ok=channel_ok)
            return
        await hub.accounts.mark_promo_onboarding_completed(user.id)
        refreshed = await hub.accounts.get_user(user.id)
        subscription = await hub.accounts.get_current_subscription(refreshed.id)
        latest_subscription = await hub.accounts.get_latest_subscription(refreshed.id) if subscription is None else subscription
        show_trial = await hub.accounts.can_offer_trial(refreshed.id)
    await state.clear()
    await send_reply_menu(callback)
    await send_home_card(
        callback,
        container,
        user=refreshed,
        subscription=subscription,
        show_trial=show_trial,
        latest_subscription=latest_subscription,
        as_new_message=False,
    )


@router.message(PromoStates.waiting_for_code)
async def promo_submit(message: Message, state: FSMContext, container: AppContainer):
    if await route_message_action(message, state, container):
        return

    state_data = await state.get_data()
    promo_source = state_data.get("promo_source")

    async with container.hub() as hub:
        if promo_source == "onboarding":
            user, consent_ok, channel_ok = await get_access_state(message, container, hub)
            if not (consent_ok and channel_ok):
                await state.clear()
                await show_pending_access_steps(message, container, consent_ok=consent_ok, channel_ok=channel_ok)
                return
        else:
            user = await ensure_client_access(message, container, hub)
            if user is None:
                return
        try:
            promo, _, result_text = await hub.promos.redeem_code(user.id, message.text or "")
        except ConflictError as exc:
            if promo_source == "onboarding":
                await answer_or_edit(
                    message,
                    f"{exc}\n\n{promo_code_prompt_text(onboarding=True)}",
                    reply_markup=promo_onboarding_skip_actions().as_markup(),
                )
            else:
                await answer_or_edit(message, str(exc), reply_markup=balance_actions().as_markup())
            return
        response_parse_mode = None
        if "следующей покупке тарифа" in result_text and promo is not None:
            result_text = (
                f"Промокод <code>{promo.code}</code> активирован. "
                f"Скидка {Decimal(promo.reward_value):.2f}% применится при следующей покупке тарифа.\n\n"
                f"🎟 В разделе «Подписка» цены уже будут показаны со скидкой по коду <code>{promo.code}</code>."
            )
            response_parse_mode = "HTML"
        if promo_source == "onboarding":
            await hub.accounts.mark_promo_onboarding_completed(user.id)
            refreshed = await hub.accounts.get_user(user.id)
            subscription = await hub.accounts.get_current_subscription(refreshed.id)
            latest_subscription = await hub.accounts.get_latest_subscription(refreshed.id) if subscription is None else subscription
            show_trial = await hub.accounts.can_offer_trial(refreshed.id)

    await state.clear()
    if promo_source == "onboarding":
        await answer_or_edit(message, result_text, parse_mode=response_parse_mode)
        await send_reply_menu(message, force=True)
        await send_home_card(
            message,
            container,
            user=refreshed,
            subscription=subscription,
            show_trial=show_trial,
            latest_subscription=latest_subscription,
            as_new_message=True,
        )
        return
    await answer_or_edit(
        message,
        result_text,
        reply_markup=balance_actions().as_markup(),
        parse_mode=response_parse_mode,
    )


@router.callback_query(F.data == "client:referral")
async def referral_info(callback: CallbackQuery, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_client_access(callback, container, hub)
        if user is None:
            return
    share_url = referral_share_vpn_url(container.settings, getattr(user, "referral_code", None))
    text = (
        "Рефералка\n\n"
        f"Ваш код: {getattr(user, 'referral_code', 'будет создан позже')}\n"
        "За пользователя, который придёт по вашей ссылке и оформит первый платный тариф, вы получите +100 ₽ на баланс."
    )
    markup = balance_actions().as_markup()
    if share_url:
        await answer_or_edit(callback, f"{text}\n\nСсылка уже подготовлена в кнопке «Поделиться VPN» в меню и профиле.", reply_markup=markup)
    else:
        await answer_or_edit(callback, text, reply_markup=markup)


@router.callback_query(F.data == "client:plan_menu")
async def plan_menu_v2(callback: CallbackQuery, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_client_access(callback, container, hub)
        if user is None:
            return
    await answer_or_edit(
        callback,
        plan_menu_text(),
        reply_markup=plan_actions().as_markup(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("client:plan_family:"))
async def plan_family_menu(callback: CallbackQuery, container: AppContainer):
    family = callback.data.split(":")[-1]
    if family not in {"10gbit", "unlimited"}:
        await callback.answer("Неизвестный тариф.", show_alert=True)
        return

    async with container.hub() as hub:
        user = await ensure_client_access(callback, container, hub)
        if user is None:
            return
        discount_preview = await resolve_plan_discount_preview(hub, user.id, family)

    text = plan_family_text(family, discount_preview=discount_preview)

    await answer_or_edit(
        callback,
        text,
        reply_markup=plan_period_actions(
            family,
            monthly_price_text=(
                str(discount_preview["monthly_button"]) if discount_preview else None
            ),
            weekly_price_text=(
                str(discount_preview["weekly_button"]) if discount_preview else None
            ),
        ).as_markup(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "client:plan_menu_legacy")
async def plan_menu(callback: CallbackQuery, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_client_access(callback, container, hub)
        if user is None:
            return
    await answer_or_edit(
        callback,
        (
            "Тарифы\n\n"
            "10 Гбит:\n"
            "• 69 ₽ в месяц, 2 устройства.\n"
            "• 22.43 ₽ в неделю, в пересчёте на месяц на 30% дороже.\n\n"
            "Безлимит:\n"
            "• 200 ₽ в месяц, 8 устройств.\n"
            "• 65 ₽ в неделю, в пересчёте на месяц на 30% дороже.\n\n"
            "Для тарифа 10 Гбит белые списки считаются отдельно по 4 ₽ за ГБ."
        ),
        reply_markup=plan_actions().as_markup(),
    )
    return
    await answer_or_edit(
        callback,
        (
            "Тарифы\n\n"
            "Один сервер 10 Гбит: 69 ₽ в месяц.\n"
            "Белые списки: 4 ₽ за каждый ГБ отдельно.\n"
            "Безлимит: 200 ₽ в месяц."
        ),
        reply_markup=plan_actions().as_markup(),
    )


@router.callback_query(F.data == "client:trial_activate")
async def trial_activate(callback: CallbackQuery, container: AppContainer):
    subscription = None
    activation_payload = None
    reply_markup = None
    response_parse_mode = None
    async with container.hub() as hub:
        user = await ensure_client_access(callback, container, hub)
        if user is None:
            return
        try:
            subscription = await hub.billing.activate_trial(user.id)
            bundle = await safe_get_subscription_bundle(hub, user.id)
            activation_payload = resolve_subscription_payload(bundle)
            text = (
                "Тестовый период Pro активирован.\n\n"
                f"Доступ ко всем активным серверам будет работать до {subscription.ends_at:%d.%m.%Y %H:%M}.\n"
                f"Лимит устройств: {device_limit_label(subscription.plan)}"
            )
            if not activation_payload:
                text += activation_link_pending_note()
            reply_markup = subscription_link_markup(container.settings, subscription)
        except ConflictError as exc:
            text = str(exc)
        except (NotFoundError, ServiceError) as exc:
            text = str(exc)
            subscription = None
    if subscription is not None and activation_payload:
        caption = trial_activation_caption(subscription, activation_payload)
        try:
            image = render_qr_png(activation_payload)
            await edit_or_send_dynamic_media_card(
                callback,
                image_bytes=image,
                filename="altlink-vpn-activation.png",
                caption=caption,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
            return
        except Exception:
            logger.exception("Failed to send trial activation media card")
            text = caption
            response_parse_mode = "HTML"
    await answer_or_edit(
        callback,
        text,
        reply_markup=reply_markup
        or subscription_actions(
            show_link=False,
            show_traffic=False,
            can_cancel=False,
            auto_renew_disabled=False,
        ).as_markup(),
        parse_mode=response_parse_mode,
    )


@router.callback_query(F.data.startswith("client:activate_plan:"))
async def activate_plan(callback: CallbackQuery, container: AppContainer):
    plan_code = parse_paid_plan_code(callback.data.split(":")[-1])
    if plan_code is None:
        await answer_or_edit(
            callback,
            "Этот тариф больше не поддерживается. Откройте раздел тарифов заново и выберите актуальный вариант.",
            reply_markup=plan_actions().as_markup(),
        )
        return
    current_subscription = None
    activation_payload = None
    reply_markup = None
    response_parse_mode = None
    async with container.hub() as hub:
        user = await ensure_client_access(callback, container, hub)
        if user is None:
            return
        try:
            subscription = await hub.billing.activate_paid_plan(user.id, plan_code, charge_user=True)
            text = (
                f"Тариф «{subscription.plan.name}» активирован.\n\n"
                f"Следующее списание: {subscription.next_billing_at:%d.%m.%Y %H:%M}\n"
                f"Формат списания: {billing_cycle_label(subscription.plan)}\n"
                f"Лимит устройств: {device_limit_label(subscription.plan)}"
            )
            current_subscription = subscription
            bundle = await safe_get_subscription_bundle(hub, user.id)
            activation_payload = resolve_subscription_payload(bundle)
            if not activation_payload:
                text += activation_link_pending_note()
        except ConflictError as exc:
            reply_markup = insufficient_balance_actions().as_markup()
            text = f"{exc}\n\nСначала пополните баланс через раздел «Баланс»."
        except (NotFoundError, ServiceError) as exc:
            text = str(exc)
        if current_subscription is None:
            current_subscription = await hub.accounts.get_current_subscription(user.id)
    if reply_markup is None:
        reply_markup = (
            subscription_link_markup(container.settings, current_subscription)
            if activation_payload
            else subscription_markup(current_subscription)
        )
    if current_subscription is not None and activation_payload:
        caption = activation_success_caption(current_subscription, activation_payload)
        try:
            image = render_qr_png(activation_payload)
            await edit_or_send_dynamic_media_card(
                callback,
                image_bytes=image,
                filename="altlink-vpn-activation.png",
                caption=caption,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
            return
        except Exception:
            logger.exception("Failed to send activation media card for plan %s", plan_code.value)
            text = caption
            response_parse_mode = "HTML"
    await answer_or_edit(callback, text, reply_markup=reply_markup, parse_mode=response_parse_mode)


@router.callback_query(F.data == "client:subscription_cancel")
async def subscription_cancel(callback: CallbackQuery, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_client_access(callback, container, hub)
        if user is None:
            return
        try:
            subscription = await hub.billing.cancel_subscription_renewal(user.id)
            text = (
                "Автопродление отключено.\n\n"
                f"Доступ сохранится до {subscription.ends_at:%d.%m.%Y %H:%M}, после этого подписка завершится."
            )
        except (ConflictError, NotFoundError, ServiceError) as exc:
            text = str(exc)
            subscription = await hub.accounts.get_current_subscription(user.id)
    await answer_or_edit(callback, text, reply_markup=subscription_markup(subscription))


@router.callback_query(F.data == "client:subscription_resume")
async def subscription_resume(callback: CallbackQuery, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_client_access(callback, container, hub)
        if user is None:
            return
        try:
            subscription = await hub.billing.restore_subscription_renewal(user.id)
            text = "Автопродление снова включено."
        except (ConflictError, NotFoundError, ServiceError) as exc:
            text = str(exc)
            subscription = await hub.accounts.get_current_subscription(user.id)
    await answer_or_edit(callback, text, reply_markup=subscription_markup(subscription))


@router.callback_query(F.data == "client:subscription_link")
async def subscription_link(callback: CallbackQuery, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_client_access(callback, container, hub)
        if user is None:
            return
        bundle = await safe_get_subscription_bundle(hub, user.id)
        subscription = bundle.get("subscription") if bundle else await hub.accounts.get_current_subscription(user.id)
        payload = resolve_subscription_payload(bundle)
        if not payload:
            await answer_or_edit(
                callback,
                (
                    "Ссылка пока недоступна. "
                    "Если тариф уже активирован, попробуйте открыть этот раздел чуть позже."
                    if subscription
                    else "Ссылка пока недоступна. Сначала активируйте тестовый период или тариф."
                ),
                reply_markup=subscription_link_markup(container.settings, subscription),
                disable_web_page_preview=True,
            )
            return
        caption = subscription_link_caption(payload)
        try:
            image = render_qr_png(payload)
            await edit_or_send_dynamic_media_card(
                callback,
                image_bytes=image,
                filename="altlink-vpn-qr.png",
                caption=caption,
                reply_markup=subscription_link_markup(container.settings, subscription),
                parse_mode="HTML",
            )
            return
        except Exception:
            logger.exception("Failed to send subscription link media card for user %s", user.id)
    await answer_or_edit(
        callback,
        caption,
        reply_markup=subscription_link_markup(container.settings, subscription),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@router.callback_query(F.data == "client:subscription_qr")
async def subscription_qr(callback: CallbackQuery, container: AppContainer):
    await subscription_link(callback, container)


@router.callback_query(F.data == "client:traffic")
async def traffic(callback: CallbackQuery, container: AppContainer):
    async with container.hub() as hub:
        user = await ensure_client_access(callback, container, hub)
        if user is None:
            return
        subscription = await hub.billing.refresh_subscription_traffic(user.id)
        if not subscription:
            text = "Трафик и списания\n\nУ вас пока нет активной подписки."
        elif not show_metered_usage(subscription):
            text = (
                "Трафик и списания\n\n"
                "Для текущего тарифа этот раздел скрыт, потому что трафик и начисления по белым спискам не актуальны."
            )
        else:
            white_cost = bytes_to_gb_cost(subscription.whitelist_traffic_used_bytes, WHITELIST_GB_PRICE_RUB)
            text = (
                "Трафик и списания\n\n"
                f"Общий трафик: {subscription.traffic_used_bytes / 1024**3:.2f} ГБ\n"
                f"Трафик по белым спискам: {subscription.whitelist_traffic_used_bytes / 1024**3:.2f} ГБ\n"
                f"Начисление к следующему продлению: {white_cost:.2f} ₽\n"
                f"Следующее списание: {subscription.next_billing_at:%d.%m.%Y %H:%M}"
            )
    await answer_or_edit(callback, text, reply_markup=subscription_markup(subscription))
    return
    async with container.hub() as hub:
        user = await ensure_client_access(callback, container, hub)
        if user is None:
            return
        subscription = await hub.accounts.get_current_subscription(user.id)
        if not subscription:
            text = "Трафик и списания\n\nУ вас пока нет активной подписки."
        elif not show_metered_usage(subscription):
            text = (
                "Трафик и списания\n\n"
                "Для текущего тарифа этот раздел скрыт, потому что трафик и начисления по белым спискам не актуальны."
            )
        else:
            white_cost = bytes_to_gb_cost(subscription.whitelist_traffic_used_bytes, WHITELIST_GB_PRICE_RUB)
            text = (
                "Трафик и списания\n\n"
                f"Общий трафик: {subscription.traffic_used_bytes / 1024**3:.2f} ГБ\n"
                f"Трафик по белым спискам: {subscription.whitelist_traffic_used_bytes / 1024**3:.2f} ГБ\n"
                f"Начислено за белые списки: {white_cost:.2f} ₽\n"
                f"Накопленный долг: {Decimal(subscription.accrued_debt_rub):.2f} ₽\n"
                f"Следующее списание: {subscription.next_billing_at:%d.%m.%Y %H:%M}"
            )
    await answer_or_edit(callback, text, reply_markup=subscription_markup(subscription))
