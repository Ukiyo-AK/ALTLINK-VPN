from __future__ import annotations

from types import SimpleNamespace

from altlink.presentation.bots.admin_keyboards import (
    BROADCAST_AUDIENCE_PREFIX,
    BROADCAST_PROMO_OPEN,
    BROADCAST_PROMO_PAGE_PREFIX,
    SERVER_DELETE_CONFIRM_PREFIX,
    SERVER_DELETE_PREFIX,
    SERVER_OPEN_PREFIX,
    USER_START_SERVER_ASSIGN_PREFIX,
    USER_START_SERVERS_PREFIX,
    USERS_SYNC_NODE_ACCESS,
    broadcast_media_actions,
    broadcast_promo_picker_actions,
    broadcast_preview_actions,
    payment_browser_actions,
    payment_request_actions,
    server_actions,
    server_delete_confirmation_actions,
    system_logs_actions,
    user_actions,
    user_device_detail_actions,
    user_devices_actions,
    user_delete_confirmation_actions,
    user_lookup_actions,
    user_start_server_actions,
    user_subscription_actions,
)


def test_payment_browser_actions_hide_manual_resolution_for_automatic_topups():
    buttons = inline_buttons(
        payment_browser_actions(
            request_id="req-1",
            status="new",
            index=0,
            total=1,
            allow_manual_resolution=False,
        ).as_markup()
    )
    assert len(buttons) == 1
    assert buttons[0]["callback_data"] == "adm:pf:req-1"
    assert buttons[0]["style"] == "primary"

def test_payment_request_actions_keep_manual_buttons_for_support_topups():
    buttons = inline_buttons(payment_request_actions("req-2", "new").as_markup())
    callback_data = [button["callback_data"] for button in buttons]
    assert callback_data == ["adm:pa:req-2", "adm:pr:req-2"]

def inline_buttons(markup) -> list[dict]:
    return [button.model_dump(exclude_none=True) for row in markup.inline_keyboard for button in row]


def test_broadcast_preview_actions_exposes_audience_filters():
    buttons = inline_buttons(broadcast_preview_actions("pro").as_markup())
    callback_data = [button["callback_data"] for button in buttons if "callback_data" in button]
    labels = [button["text"] for button in buttons]

    assert f"{BROADCAST_AUDIENCE_PREFIX}:all" in callback_data
    assert f"{BROADCAST_AUDIENCE_PREFIX}:trial" in callback_data
    assert f"{BROADCAST_AUDIENCE_PREFIX}:blocked" in callback_data
    assert f"{BROADCAST_AUDIENCE_PREFIX}:start" in callback_data
    assert f"{BROADCAST_AUDIENCE_PREFIX}:pro" in callback_data
    assert f"{BROADCAST_AUDIENCE_PREFIX}:single_10gbit" in callback_data
    assert f"{BROADCAST_AUDIENCE_PREFIX}:unlimited_weekly" in callback_data
    assert "✓ Все Pro" in labels
    assert "Отправить выбранным" in labels


def test_broadcast_actions_allow_selecting_and_paging_promo_codes():
    promo_id = "609237c1-7ffb-4d76-9861-a14b7ddc8a6a"
    media_buttons = inline_buttons(broadcast_media_actions().as_markup())
    preview_buttons = inline_buttons(broadcast_preview_actions("all", promo_code="MAIL10").as_markup())
    picker_buttons = inline_buttons(
        broadcast_promo_picker_actions(
            [SimpleNamespace(id=promo_id, code="MAIL10")],
            page=1,
            total_pages=3,
            selected_promo_id=promo_id,
        ).as_markup()
    )

    assert any(button.get("callback_data") == BROADCAST_PROMO_OPEN for button in media_buttons)
    assert any(button["text"] == "Промокод: MAIL10" for button in preview_buttons)
    assert any(button["text"] == "✓ MAIL10" for button in picker_buttons)
    assert f"{BROADCAST_PROMO_PAGE_PREFIX}:0" in {button.get("callback_data") for button in picker_buttons}
    assert f"{BROADCAST_PROMO_PAGE_PREFIX}:2" in {button.get("callback_data") for button in picker_buttons}
    assert all(len(button.get("callback_data", "").encode("utf-8")) <= 64 for button in picker_buttons)


def test_user_actions_styles_destructive_and_primary_buttons():
    buttons = inline_buttons(user_actions("user-1").as_markup())
    delete_button = next(button for button in buttons if button["text"] == "Удалить аккаунт")
    subscription_button = next(button for button in buttons if button["text"] == "Подписка и статус")

    assert delete_button["style"] == "danger"
    assert subscription_button["style"] == "primary"


def test_start_server_action_is_only_shown_for_start_user():
    regular_buttons = inline_buttons(user_actions("user-1").as_markup())
    start_buttons = inline_buttons(
        user_actions("user-1", can_reassign_start_server=True).as_markup()
    )

    assert not any(button["text"] == "Переназначить Start-сервер" for button in regular_buttons)
    button = next(button for button in start_buttons if button["text"] == "Переназначить Start-сервер")
    assert button["callback_data"] == f"{USER_START_SERVERS_PREFIX}:0:user-1"


def test_start_server_picker_paginates_and_keeps_callbacks_within_telegram_limit():
    user_id = "609237c1-7ffb-4d76-9861-a14b7ddc8a6a"
    servers = [
        SimpleNamespace(
            id=f"00000000-0000-0000-0000-{index:012d}",
            name=f"Start server {index}",
            country_code="RU",
        )
        for index in range(8)
    ]
    buttons = inline_buttons(
        user_start_server_actions(
            user_id,
            servers,
            current_server_id=servers[6].id,
            page=1,
        ).as_markup()
    )
    callbacks = [button["callback_data"] for button in buttons if "callback_data" in button]

    assert any(callback.startswith(f"{USER_START_SERVER_ASSIGN_PREFIX}:1:") for callback in callbacks)
    assert f"{USER_START_SERVERS_PREFIX}:0:{user_id}" in callbacks
    assert all(len(callback.encode("utf-8")) <= 64 for callback in callbacks)


def test_user_subscription_actions_keep_old_controls_in_subsection():
    buttons = inline_buttons(user_subscription_actions("user-1").as_markup())
    deactivate = next(button for button in buttons if button["text"] == "Деактивировать")
    activate = next(button for button in buttons if button["text"] == "Активировать")
    trial = next(button for button in buttons if button["text"] == "Тест на 2 дня")

    assert deactivate["style"] == "danger"
    assert activate["style"] == "success"
    assert trial["style"] == "primary"


def test_user_action_callbacks_fit_telegram_limit():
    user_id = "609237c1-7ffb-4d76-9861-a14b7ddc8a6a"
    buttons = inline_buttons(user_subscription_actions(user_id).as_markup())
    buttons += inline_buttons(user_actions(user_id).as_markup())
    callback_data = [button["callback_data"] for button in buttons if "callback_data" in button]

    assert callback_data
    assert all(len(item.encode("utf-8")) <= 64 for item in callback_data)


def test_delete_confirmation_actions_are_destructive():
    buttons = inline_buttons(user_delete_confirmation_actions("u1").as_markup())
    assert buttons[0]["style"] == "danger"
    assert buttons[0]["callback_data"] == "adm:xc:u1"


def test_server_actions_styles_toggle_and_types():
    active_buttons = inline_buttons(server_actions("server-1", True).as_markup())
    remove_button = next(button for button in active_buttons if button["text"] == "Убрать из локальной системы")
    wl_button = next(button for button in active_buttons if button["text"] == "WL")
    ten_gbit_button = next(button for button in active_buttons if button["text"] == "⚡ Start")
    delete_button = next(button for button in active_buttons if button["callback_data"] == f"{SERVER_DELETE_PREFIX}:server-1")

    assert remove_button["style"] == "danger"
    assert wl_button["style"] == "success"
    assert ten_gbit_button["style"] == "primary"
    assert delete_button["style"] == "danger"


def test_server_delete_confirmation_actions_are_safe_and_destructive():
    buttons = inline_buttons(server_delete_confirmation_actions("server-1").as_markup())

    assert buttons[0]["style"] == "danger"
    assert buttons[0]["callback_data"] == f"{SERVER_DELETE_CONFIRM_PREFIX}:server-1"
    assert buttons[1]["callback_data"] == f"{SERVER_OPEN_PREFIX}:server-1"


def test_user_lookup_actions_builds_open_buttons():
    items = [SimpleNamespace(id="u1", username="demo", telegram_id=12345)]
    buttons = inline_buttons(user_lookup_actions(items).as_markup())
    assert buttons[0]["callback_data"] == "adm:uo:u1"


def test_user_lookup_actions_can_include_node_access_sync_button():
    buttons = inline_buttons(user_lookup_actions([], include_node_sync=True).as_markup())
    assert buttons == [
        {
            "text": "Синхронизировать доступ к нодам",
            "callback_data": USERS_SYNC_NODE_ACCESS,
            "style": "success",
        }
    ]


def test_system_logs_actions_exposes_refresh_button():
    buttons = inline_buttons(system_logs_actions().as_markup())
    assert buttons == [
        {
            "text": "Обновить журнал",
            "callback_data": "admin:logs:refresh",
            "style": "primary",
        }
    ]


def test_user_devices_actions_paginate_and_fit_telegram_limit():
    user_id = "609237c1-7ffb-4d76-9861-a14b7ddc8a6a"
    devices = [{"name": f"Device {index}"} for index in range(10)]
    buttons = inline_buttons(user_devices_actions(user_id, devices, page=1, page_size=6).as_markup())
    buttons += inline_buttons(user_device_detail_actions(user_id, page=1).as_markup())

    device_buttons = [item for item in buttons if item.get("callback_data", "").startswith("adm:do:")]
    assert len(device_buttons) == 4
    assert all(len(item["callback_data"].encode("utf-8")) <= 64 for item in buttons)
