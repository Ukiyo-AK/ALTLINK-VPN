from __future__ import annotations

from pathlib import Path

import pytest


TEMPLATE_ROOT = Path("src/altlink/presentation/web/templates")
ASSET_TEMPLATES = [
    "base.html",
    "landing.html",
    "legal_agreement.html",
    "legal_privacy.html",
    "login.html",
    "portal_dashboard.html",
    "portal_help.html",
    "portal_login.html",
]


@pytest.mark.parametrize("template_name", ASSET_TEMPLATES)
def test_web_templates_use_relative_asset_paths(template_name: str):
    content = (TEMPLATE_ROOT / template_name).read_text(encoding="utf-8")

    assert "{{ url_for('static', path='style.css') }}" not in content
    assert "{{ url_for('media', path='logo.png') }}" not in content
    assert 'rel="icon"' in content
    assert 'href="/static/style.css"' in content
    assert 'src="/media/logo.png"' in content


def test_landing_hero_background_uses_relative_media_path():
    content = (TEMPLATE_ROOT / "landing.html").read_text(encoding="utf-8")

    assert "url('{{ url_for('media', path='logo without background.png') }}')" not in content
    assert "url('/media/logo without background.png')" in content


def test_portal_login_template_uses_bot_confirm_flow_instead_of_widget():
    content = (TEMPLATE_ROOT / "portal_login.html").read_text(encoding="utf-8")

    assert "telegram-widget.js" not in content
    assert "portal-login-status" in content
    assert "portal-login-qr-image" in content


def test_portal_dashboard_template_supports_one_tap_copy_for_subscription_link():
    content = (TEMPLATE_ROOT / "portal_dashboard.html").read_text(encoding="utf-8")

    assert "data-copy-root" in content
    assert "data-copy-target" in content
    assert "data-copy-button" not in content
    assert ">Скопировать ссылку<" not in content


def test_public_templates_keep_personal_account_action_only_where_needed():
    landing = (TEMPLATE_ROOT / "landing.html").read_text(encoding="utf-8")
    portal_login = (TEMPLATE_ROOT / "portal_login.html").read_text(encoding="utf-8")

    assert "landing-topbar-actions" in landing
    assert 'href="{{ portal_login_url }}"' in landing
    assert "login-page-topbar" not in portal_login
    assert "page-topbar-actions" not in portal_login


def test_landing_support_handle_uses_dedicated_mobile_friendly_class():
    content = (TEMPLATE_ROOT / "landing.html").read_text(encoding="utf-8")

    assert 'class="landing-support-handle"' in content


def test_dashboard_template_reads_plan_mix_values_from_dict_keys():
    content = (TEMPLATE_ROOT / "dashboard.html").read_text(encoding="utf-8")

    assert "charts.plan_mix.values[0]" not in content
    assert 'charts["plan_mix"]["values"][0]' in content
    assert 'charts["plan_mix"]["values"][1]' in content


def test_traffic_template_handles_rows_without_active_plan():
    content = (TEMPLATE_ROOT / "traffic.html").read_text(encoding="utf-8")

    assert "{{ subscription.plan.name }}" not in content
    assert "{{ subscription.plan.name if subscription.plan else '—' }}" in content


def test_legal_templates_mark_document_body_for_aggressive_wrapping():
    agreement = (TEMPLATE_ROOT / "legal_agreement.html").read_text(encoding="utf-8")
    privacy = (TEMPLATE_ROOT / "legal_privacy.html").read_text(encoding="utf-8")

    assert "legal-document-body" in agreement
    assert "legal-document-body" in privacy


def test_portal_and_admin_user_templates_show_hwid_devices():
    portal = (TEMPLATE_ROOT / "portal_dashboard.html").read_text(encoding="utf-8")
    admin = (TEMPLATE_ROOT / "user_detail.html").read_text(encoding="utf-8")

    assert 'action="/portal/devices/delete"' in portal
    assert "portal_devices" in portal
    assert "devices_error" in admin


def test_web_theme_does_not_use_legacy_yellow_palette():
    style = Path("src/altlink/presentation/web/static/style.css").read_text(encoding="utf-8")
    dashboard = (TEMPLATE_ROOT / "dashboard.html").read_text(encoding="utf-8")
    web_theme = f"{style}\n{dashboard}".lower()

    for legacy_token in (
        "gold",
        "background.png",
        "#c79b2f",
        "#d17d26",
        "199, 155, 47",
        "209, 125, 38",
        "217, 95, 89",
        "246, 191, 56",
    ):
        assert legacy_token not in web_theme
