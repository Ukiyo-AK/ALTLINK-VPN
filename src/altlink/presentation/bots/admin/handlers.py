from __future__ import annotations

from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import desc, select

from altlink.application.services import (
    AccountService,
    AdminAuthService,
    BillingService,
    DashboardService,
    ServerService,
    SubscriptionService,
)
from altlink.domain.enums import TopupRequestStatus
from altlink.infrastructure.db.models import TopupRequest, User
from altlink.presentation.bots.common.context import BotContext, open_session
from altlink.presentation.bots.common.keyboards import (
    admin_main_keyboard,
    admin_topup_detail_inline,
    admin_topups_inline,
    admin_user_actions_inline,
)

router = Router()


class AdminStates(StatesGroup):
    waiting_user_query = State()
    waiting_balance_amount = State()


async def _is_admin(message_or_callback, bot_context: BotContext) -> bool:
    async with open_session(bot_context) as session:
        service = AdminAuthService(session, bot_context.settings, bot_context.remnawave)
        return await service.is_admin_telegram_id(message_or_callback.from_user.id)


async def _admin_or_deny(message_or_callback, bot_context: BotContext) -> bool:
    allowed = await _is_admin(message_or_callback, bot_context)
    if not allowed:
        target = message_or_callback.message if hasattr(message_or_callback, "message") else message_or_callback
        await target.answer("У вас нет доступа к админ-боту.")
        return False
    return True


@router.message(CommandStart())
async def start(message: Message, bot_context: BotContext) -> None:
    if not await _admin_or_deny(message, bot_context):
        return
    await message.answer(
        "Админ-бот ALTLINK готов к работе.\n"
        "Здесь можно обрабатывать пополнения, искать пользователей, управлять серверами и смотреть статистику.",
        reply_markup=admin_main_keyboard(),
    )


@router.message(F.text == "Помощь")
async def help_screen(message: Message, bot_context: BotContext) -> None:
    if not await _admin_or_deny(message, bot_context):
        return
    await message.answer(
        "Основные сценарии:\n"
        "• «Заявки» — открыть новые пополнения и подтвердить/отклонить.\n"
        "• «Пользователь» — найти клиента по Telegram ID или username.\n"
        "• «Серверы» — увидеть список серверов и синхронизировать их из Remnawave.\n"
        "• «Статистика» — сводка по статусам, трафику и онлайну."
    )


@router.message(F.text == "Заявки")
async def topups(message: Message, bot_context: BotContext) -> None:
    if not await _admin_or_deny(message, bot_context):
        return
    async with open_session(bot_context) as session:
        topups = (
            await session.execute(
                select(TopupRequest)
                .where(TopupRequest.status == TopupRequestStatus.NEW)
                .order_by(desc(TopupRequest.created_at))
                .limit(20)
            )
        ).scalars().all()
        if not topups:
            await message.answer("Новых заявок нет.")
            return
        items = [(item.id, f"{item.amount_rub:.2f} ₽ • user {item.user_id}") for item in topups]
        await message.answer("Новые заявки на пополнение:", reply_markup=admin_topups_inline(items))


@router.callback_query(F.data.startswith("admin:topup:approve:"))
async def approve_topup(callback: CallbackQuery, bot_context: BotContext) -> None:
    if not await _admin_or_deny(callback, bot_context):
        return
    topup_id = callback.data.split(":")[-1]
    async with open_session(bot_context) as session:
        admin_service = AdminAuthService(session, bot_context.settings, bot_context.remnawave)
        admin = await admin_service.get_admin_by_telegram_id(callback.from_user.id)
        topup = await session.get(TopupRequest, topup_id)
        if topup and admin:
            await BillingService(session, bot_context.settings, bot_context.remnawave).approve_topup_request(
                topup, admin
            )
            text = "Пополнение подтверждено."
        else:
            text = "Не удалось обработать заявку."
    await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:topup:reject:"))
async def reject_topup(callback: CallbackQuery, bot_context: BotContext) -> None:
    if not await _admin_or_deny(callback, bot_context):
        return
    topup_id = callback.data.split(":")[-1]
    async with open_session(bot_context) as session:
        admin_service = AdminAuthService(session, bot_context.settings, bot_context.remnawave)
        admin = await admin_service.get_admin_by_telegram_id(callback.from_user.id)
        topup = await session.get(TopupRequest, topup_id)
        if topup and admin:
            await BillingService(session, bot_context.settings, bot_context.remnawave).reject_topup_request(
                topup, admin
            )
            text = "Пополнение отклонено."
        else:
            text = "Не удалось обработать заявку."
    await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:topup:"))
async def topup_detail(callback: CallbackQuery, bot_context: BotContext) -> None:
    if not await _admin_or_deny(callback, bot_context):
        return
    topup_id = callback.data.split(":")[-1]
    async with open_session(bot_context) as session:
        topup = await session.get(TopupRequest, topup_id)
        if topup is None:
            await callback.message.answer("Заявка не найдена.")
        else:
            await callback.message.answer(
                f"Заявка {topup.id}\n"
                f"User ID: {topup.user_id}\n"
                f"Сумма: {topup.amount_rub:.2f} ₽\n"
                f"Статус: {topup.status.value}",
                reply_markup=admin_topup_detail_inline(topup.id) if topup.status.value == "new" else None,
            )
    await callback.answer()


@router.message(F.text == "Пользователь")
async def ask_user_search(message: Message, state: FSMContext, bot_context: BotContext) -> None:
    if not await _admin_or_deny(message, bot_context):
        return
    await state.set_state(AdminStates.waiting_user_query)
    await message.answer("Отправьте Telegram ID или username пользователя.")


@router.message(AdminStates.waiting_user_query)
async def user_search(message: Message, state: FSMContext, bot_context: BotContext) -> None:
    if not await _admin_or_deny(message, bot_context):
        return
    query = message.text.strip()
    async with open_session(bot_context) as session:
        service = DashboardService(session, bot_context.settings, bot_context.remnawave)
        users = await service.search_users(query)
        if not users:
            await message.answer("Пользователь не найден.")
        else:
            user = users[0]
            account = AccountService(session, bot_context.settings, bot_context.remnawave)
            summary = await account.get_profile_summary(user)
            lines = [
                f"Пользователь {user.telegram_id}",
                f"Username: {user.telegram_username or '—'}",
                f"Статус: {user.status.value}",
                f"Баланс: {user.balance_rub:.2f} ₽",
                f"Тариф: {summary['plan'].name_ru if summary['plan'] else 'нет'}",
            ]
            if summary["subscription"] and summary["subscription"].next_billing_at:
                lines.append(
                    f"Продление: {summary['subscription'].next_billing_at:%d.%m.%Y %H:%M}"
                )
            text = "\n".join(lines)
            await message.answer(text or "Карточка открыта.", reply_markup=admin_user_actions_inline(user.id))
    await state.clear()


@router.callback_query(F.data.startswith("admin:user:trial:"))
async def user_trial(callback: CallbackQuery, bot_context: BotContext) -> None:
    if not await _admin_or_deny(callback, bot_context):
        return
    user_id = callback.data.split(":")[-1]
    async with open_session(bot_context) as session:
        admin = await AdminAuthService(session, bot_context.settings, bot_context.remnawave).get_admin_by_telegram_id(callback.from_user.id)
        user = await session.get(User, user_id)
        if user and admin:
            try:
                await SubscriptionService(session, bot_context.settings, bot_context.remnawave).activate_trial(user, admin_user_id=admin.id)
                text = "Тест выдан."
            except Exception as exc:  # noqa: BLE001
                text = str(exc)
        else:
            text = "Не удалось найти пользователя."
    await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:user:plan:"))
async def user_activate_plan(callback: CallbackQuery, bot_context: BotContext) -> None:
    if not await _admin_or_deny(callback, bot_context):
        return
    _, _, _, user_id, plan_code = callback.data.split(":", 4)
    async with open_session(bot_context) as session:
        user = await session.get(User, user_id)
        if user:
            await SubscriptionService(session, bot_context.settings, bot_context.remnawave).manual_set_active(user, plan_code)
            text = "Тариф активирован вручную."
        else:
            text = "Пользователь не найден."
    await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:user:balance:"))
async def user_balance_start(callback: CallbackQuery, state: FSMContext, bot_context: BotContext) -> None:
    if not await _admin_or_deny(callback, bot_context):
        return
    user_id = callback.data.split(":")[-1]
    await state.set_state(AdminStates.waiting_balance_amount)
    await state.update_data(user_id=user_id)
    await callback.message.answer(
        "Введите сумму корректировки одним сообщением.\n"
        "Можно использовать отрицательное число, например -50."
    )
    await callback.answer()


@router.message(AdminStates.waiting_balance_amount)
async def user_balance_finish(message: Message, state: FSMContext, bot_context: BotContext) -> None:
    if not await _admin_or_deny(message, bot_context):
        return
    data = await state.get_data()
    user_id = data.get("user_id")
    try:
        amount = Decimal(message.text.replace(",", "."))
    except Exception:  # noqa: BLE001
        await message.answer("Не удалось распознать сумму. Отправьте число, например 150 или -50.")
        return
    async with open_session(bot_context) as session:
        admin = await AdminAuthService(session, bot_context.settings, bot_context.remnawave).get_admin_by_telegram_id(message.from_user.id)
        user = await session.get(User, user_id)
        if user and admin:
            await BillingService(session, bot_context.settings, bot_context.remnawave).adjust_balance(
                user,
                admin,
                amount_rub=amount,
                comment="Корректировка через админ-бот",
            )
            text = f"Баланс обновлен. Новое значение: {user.balance_rub:.2f} ₽."
        else:
            text = "Не удалось обновить баланс."
    await state.clear()
    await message.answer(text)


@router.callback_query(F.data.startswith("admin:user:block:"))
async def user_block(callback: CallbackQuery, bot_context: BotContext) -> None:
    if not await _admin_or_deny(callback, bot_context):
        return
    user_id = callback.data.split(":")[-1]
    async with open_session(bot_context) as session:
        user = await session.get(User, user_id)
        if user:
            await SubscriptionService(session, bot_context.settings, bot_context.remnawave).manual_deactivate(user)
            text = "Пользователь заблокирован."
        else:
            text = "Пользователь не найден."
    await callback.message.answer(text)
    await callback.answer()


@router.message(F.text == "Серверы")
async def servers(message: Message, bot_context: BotContext) -> None:
    if not await _admin_or_deny(message, bot_context):
        return
    async with open_session(bot_context) as session:
        servers = await ServerService(session, bot_context.settings, bot_context.remnawave).list_managed_servers()
        if not servers:
            await message.answer("Локальный список пуст. Синхронизирую сервера из Remnawave...")
            servers = await ServerService(session, bot_context.settings, bot_context.remnawave).sync_from_remnawave()
        lines = [
            f"{server.name}: {'online' if server.is_connected else 'offline'}, нагрузка {server.load_percent}%, клиентов {server.current_clients_count}/{server.max_clients_count}"
            for server in servers[:20]
        ]
        await message.answer("Серверы:\n" + ("\n".join(lines) if lines else "нет данных"))


@router.message(F.text == "Статистика")
async def statistics(message: Message, bot_context: BotContext) -> None:
    if not await _admin_or_deny(message, bot_context):
        return
    async with open_session(bot_context) as session:
        dashboard = await DashboardService(session, bot_context.settings, bot_context.remnawave).get_dashboard()
        lines = [
            "Сводка",
            f"Активные: {dashboard['counts']['active']}",
            f"Grace: {dashboard['counts']['grace']}",
            f"Заблокированные: {dashboard['counts']['blocked']}",
            f"Тестовые: {dashboard['counts']['trial']}",
            f"Новые заявки: {dashboard['counts']['new_topups']}",
            f"Онлайн: {dashboard['counts']['online']}",
            f"Задолженности: {dashboard['debts_rub']:.2f} ₽",
        ]
        if dashboard["top_users"]:
            lines.append("")
            lines.append("Топ по трафику:")
            for user, subscription in dashboard["top_users"][:5]:
                lines.append(
                    f"• {user.telegram_username or user.telegram_id}: {subscription.traffic_used_bytes_cache} байт"
                )
        await message.answer("\n".join(lines))
