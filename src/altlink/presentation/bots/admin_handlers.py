from __future__ import annotations

from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from altlink.application.services.base import ConflictError
from altlink.application.services.registry import AppContainer
from altlink.domain.enums import BalanceTransactionType, PlanCode, TopupStatus
from altlink.presentation.bots.admin_keyboards import admin_menu, server_actions, topup_actions, user_actions

router = Router(name="admin-router")


class SearchStates(StatesGroup):
    waiting_for_query = State()


class BalanceStates(StatesGroup):
    waiting_for_amount = State()


async def is_admin(telegram_id: int, container: AppContainer) -> bool:
    async with container.hub() as hub:
        return await hub.accounts.can_access_admin_bot(telegram_id)


async def show_user_card(target, user_id: str, container: AppContainer):
    async with container.hub() as hub:
        card = await hub.accounts.user_card(user_id)
        user = card["user"]
        subscription = card["subscription"]
        debt = Decimal("0")
        if subscription and subscription.plan:
            debt = max(Decimal(subscription.plan.price_rub) - Decimal(user.balance_rub), Decimal("0"))
        text = (
            f"Карточка пользователя\n\n"
            f"Telegram ID: {user.telegram_id}\n"
            f"Username: {user.username or 'нет'}\n"
            f"Статус: {user.status}\n"
            f"Баланс: {Decimal(user.balance_rub):.2f} ₽\n"
            f"Тариф: {subscription.plan.name if subscription else 'нет'}\n"
            f"Продление: {subscription.next_billing_at if subscription else '—'}\n"
            f"Задолженность: {debt:.2f} ₽\n"
            f"Трафик: {subscription.traffic_used_bytes if subscription else 0}"
        )
    await target.answer(text, reply_markup=user_actions(user_id).as_markup())


@router.message(CommandStart())
async def start(message: Message, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        await message.answer("Доступ к admin bot запрещён.")
        return
    await message.answer(
        "admin altlink bot готов к работе.\n\n"
        "Через него можно подтверждать заявки, искать пользователей, управлять доступом и серверами.",
        reply_markup=admin_menu(),
    )


@router.message(F.text == "Помощь")
async def admin_help(message: Message, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    await message.answer(
        "Помощь по admin bot\n\n"
        "Заявки: подтверждение и отклонение пополнений.\n"
        "Пользователи: поиск по Telegram ID или username.\n"
        "Серверы: sync Remnawave и локальное включение/исключение.\n"
        "Статистика: сводка по базе и трафику.\n"
        "Онлайн: свежий snapshot последних активностей."
    )


@router.message(F.text == "Заявки")
async def topups_screen(message: Message, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    async with container.hub() as hub:
        items = await hub.topups.list_requests(status=TopupStatus.NEW)
        if not items:
            recent = await hub.topups.list_requests()
            text = "Новых заявок нет.\n\nПоследние заявки:\n"
            text += "\n".join([f"• {item.amount_rub} ₽ — {item.status}" for item in recent[:10]]) or "Пусто."
            await message.answer(text)
            return
        for item in items[:10]:
            user = item.user
            await message.answer(
                f"Новая заявка\n\n"
                f"Пользователь: {user.username or user.telegram_id}\n"
                f"Сумма: {item.amount_rub} ₽\n"
                f"Комментарий: {item.user_comment or '—'}",
                reply_markup=topup_actions(item.id).as_markup(),
            )


@router.callback_query(F.data.startswith("admin:topup_approve:"))
async def topup_approve(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        await callback.answer("Нет доступа", show_alert=True)
        return
    request_id = callback.data.split(":")[-1]
    async with container.hub() as hub:
        admin = await hub.accounts.get_admin_by_telegram_id(callback.from_user.id)
        await hub.topups.approve(request_id, admin.id if admin else None)
    await callback.message.edit_text("Заявка подтверждена.")
    await callback.answer()


@router.callback_query(F.data.startswith("admin:topup_reject:"))
async def topup_reject(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        await callback.answer("Нет доступа", show_alert=True)
        return
    request_id = callback.data.split(":")[-1]
    async with container.hub() as hub:
        admin = await hub.accounts.get_admin_by_telegram_id(callback.from_user.id)
        await hub.topups.reject(request_id, admin.id if admin else None)
    await callback.message.edit_text("Заявка отклонена.")
    await callback.answer()


@router.message(F.text == "Пользователи")
async def users_prompt(message: Message, state: FSMContext, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    await state.set_state(SearchStates.waiting_for_query)
    await message.answer("Введите Telegram ID или username пользователя.")


@router.message(SearchStates.waiting_for_query)
async def users_search(message: Message, state: FSMContext, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    async with container.hub() as hub:
        if message.text.isdigit():
            user = await hub.accounts.get_user_by_telegram_id(int(message.text))
            if user is None:
                await message.answer("Пользователь не найден.")
                return
            await show_user_card(message, user.id, container)
        else:
            users = await hub.accounts.list_users(message.text)
            if not users:
                await message.answer("Пользователь не найден.")
                return
            for user in users[:5]:
                await show_user_card(message, user.id, container)
    await state.clear()


@router.callback_query(F.data.startswith("admin:user_trial:"))
async def user_trial(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    user_id = callback.data.split(":")[-1]
    async with container.hub() as hub:
        try:
            await hub.billing.activate_trial(user_id)
            text = "Тестовый период выдан."
        except ConflictError as exc:
            text = str(exc)
    await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:user_activate:"))
async def user_activate(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    user_id = callback.data.split(":")[-1]
    async with container.hub() as hub:
        await hub.billing.reactivate_user(user_id)
    await callback.message.answer("Пользователь активирован.")
    await callback.answer()


@router.callback_query(F.data.startswith("admin:user_deactivate:"))
async def user_deactivate(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    user_id = callback.data.split(":")[-1]
    async with container.hub() as hub:
        await hub.billing.deactivate_user(user_id)
    await callback.message.answer("Пользователь деактивирован.")
    await callback.answer()


@router.callback_query(F.data.startswith("admin:user_plan:"))
async def user_plan(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    _, _, _, user_id, plan_code = callback.data.split(":")
    async with container.hub() as hub:
        admin = await hub.accounts.get_admin_by_telegram_id(callback.from_user.id)
        try:
            await hub.billing.activate_paid_plan(
                user_id,
                PlanCode(plan_code),
                charge_user=False,
                admin_id=admin.id if admin else None,
            )
            text = "Тариф применён без списания."
        except ConflictError as exc:
            text = str(exc)
    await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:user_balance:"))
async def user_balance_prompt(callback: CallbackQuery, state: FSMContext, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    user_id = callback.data.split(":")[-1]
    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(user_id=user_id)
    await callback.message.answer(
        "Введите сумму корректировки. Можно со знаком минус, например: -50 или 250"
    )
    await callback.answer()


@router.message(BalanceStates.waiting_for_amount)
async def user_balance_apply(message: Message, state: FSMContext, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    data = await state.get_data()
    user_id = data["user_id"]
    try:
        amount = Decimal(message.text.replace(",", "."))
    except (InvalidOperation, AttributeError):
        await message.answer("Не удалось распознать сумму. Попробуйте ещё раз.")
        return
    async with container.hub() as hub:
        admin = await hub.accounts.get_admin_by_telegram_id(message.from_user.id)
        await hub.accounts.adjust_balance(
            user_id=user_id,
            amount_rub=amount,
            transaction_type=BalanceTransactionType.MANUAL_ADJUSTMENT,
            description="Ручная корректировка через admin bot",
            admin_id=admin.id if admin else None,
        )
    await state.clear()
    await message.answer("Баланс обновлён.")


@router.message(F.text == "Серверы")
async def servers(message: Message, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    async with container.hub() as hub:
        servers = await hub.catalog.list_servers()
        await message.answer("Серверы\n\nДля синхронизации нажмите /start в web panel или используйте кнопку ниже.")
        await message.answer("Синхронизация серверов", reply_markup=server_actions("sync", True).as_markup())
        for server in servers[:10]:
            await message.answer(
                f"{server.name}\n"
                f"Адрес: {server.address}\n"
                f"Локально: {'включён' if server.is_available else 'выключен'}\n"
                f"Статус: {'online' if server.is_connected else 'offline'}\n"
                f"Клиенты: {server.current_clients}/{server.max_clients or 'n/a'}\n"
                f"Нагрузка: {server.load_percent}%",
                reply_markup=server_actions(server.id, server.is_available).as_markup(),
            )


@router.callback_query(F.data == "admin:server_toggle:sync:0")
async def sync_servers(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    async with container.hub() as hub:
        servers = await hub.catalog.sync_servers()
    await callback.message.answer(f"Синхронизация завершена. Серверов: {len(servers)}")
    await callback.answer()


@router.callback_query(F.data.startswith("admin:server_toggle:"))
async def toggle_server(callback: CallbackQuery, container: AppContainer):
    if not await is_admin(callback.from_user.id, container):
        return
    _, _, _, server_id, flag = callback.data.split(":")
    if server_id == "sync":
        await sync_servers(callback, container)
        return
    async with container.hub() as hub:
        await hub.catalog.set_server_availability(server_id, flag == "1")
    await callback.message.answer("Локальный статус сервера обновлён.")
    await callback.answer()


@router.message(F.text == "Статистика")
async def statistics(message: Message, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    async with container.hub() as hub:
        overview = await hub.dashboard.overview()
    text = (
        "Статистика\n\n"
        f"Активные: {overview['active_users']}\n"
        f"Grace: {overview['grace_users']}\n"
        f"Заблокированные: {overview['blocked_users']}\n"
        f"Тестовые: {overview['trial_users']}\n"
        f"Новые заявки: {overview['new_topups']}\n"
        f"Суммарный трафик: {overview['total_traffic_bytes']}"
    )
    if overview["top_users"]:
        text += "\n\nТоп по трафику:\n"
        text += "\n".join(
            [
                f"• {item.user.username or item.user.telegram_id}: {item.traffic_used_bytes}"
                for item in overview["top_users"][:5]
            ]
        )
    await message.answer(text)


@router.message(F.text == "Онлайн")
async def online(message: Message, container: AppContainer):
    if not await is_admin(message.from_user.id, container):
        return
    async with container.hub() as hub:
        await hub.online.refresh_online_cache(detailed=True)
        records = await hub.online.list_online(only_online=False)
    if not records:
        await message.answer("Данных по online-сессиям пока нет.")
        return
    lines = []
    for item in records[:15]:
        user_label = item.user.username if item.user and item.user.username else (
            item.user.telegram_id if item.user else "n/a"
        )
        server_label = item.server.name if item.server else "n/a"
        lines.append(
            f"• {user_label} | {server_label} | {item.remote_ip or 'n/a'} | "
            f"{'online' if item.is_online else 'offline'} | {item.last_activity_at or 'n/a'}"
        )
    await message.answer("Онлайн клиенты\n\n" + "\n".join(lines))
