from __future__ import annotations

from types import SimpleNamespace

from altlink.presentation.bots.admin_keyboards import (
    server_actions,
    system_logs_actions,
    user_actions,
    user_delete_confirmation_actions,
    user_lookup_actions,
    user_subscription_actions,
)


def inline_buttons(markup) -> list[dict]:
    return [button.model_dump(exclude_none=True) for row in markup.inline_keyboard for button in row]


def test_user_actions_styles_destructive_and_primary_buttons():
    buttons = inline_buttons(user_actions("user-1").as_markup())
    delete_button = next(button for button in buttons if button["text"] == "Удалить аккаунт")
    subscription_button = next(button for button in buttons if button["text"] == "Подписка и статус")

    assert delete_button["style"] == "danger"
    assert subscription_button["style"] == "primary"


def test_user_subscription_actions_keep_old_controls_in_subsection():
    buttons = inline_buttons(user_subscription_actions("user-1").as_markup())
    deactivate = next(button for button in buttons if button["text"] == "Деактивировать")
    activate = next(button for button in buttons if button["text"] == "Активировать")
    trial = next(button for button in buttons if button["text"] == "Тест на 2 дня")

    assert deactivate["style"] == "danger"
    assert activate["style"] == "success"
    assert trial["style"] == "primary"


def test_user_action_callbacks_fit_telegram_limit():
    buttons = inline_buttons(user_subscription_actions("609237c1-7ffb-4d76-9861-a14b7ddc8a6a").as_markup())
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

    assert remove_button["style"] == "danger"
    assert wl_button["style"] == "success"
    assert ten_gbit_button["style"] == "primary"


def test_user_lookup_actions_builds_open_buttons():
    items = [SimpleNamespace(id="u1", username="demo", telegram_id=12345)]
    buttons = inline_buttons(user_lookup_actions(items).as_markup())
    assert buttons[0]["callback_data"] == "adm:uo:u1"


def test_system_logs_actions_exposes_refresh_button():
    buttons = inline_buttons(system_logs_actions().as_markup())
    assert buttons == [
        {
            "text": "Обновить журнал",
            "callback_data": "admin:logs:refresh",
            "style": "primary",
        }
    ]
