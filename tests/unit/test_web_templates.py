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
    assert 'href="/static/style.css?v={{ asset_version }}"' in content
    assert 'src="/media/logo.png"' in content


def test_landing_hero_avoids_heavy_background_logo():
    content = (TEMPLATE_ROOT / "landing.html").read_text(encoding="utf-8")

    assert "url('{{ url_for('media', path='logo without background.png') }}')" not in content
    assert "url('/media/logo without background.png')" not in content
    assert "landing-hero-glow" in content


def test_portal_login_template_uses_bot_confirm_flow_instead_of_widget():
    content = (TEMPLATE_ROOT / "portal_login.html").read_text(encoding="utf-8")

    assert "telegram-widget.js" not in content
    assert "portal-login-layout" in content
    assert "portal-login-main" in content
    assert "portal-login-status" in content
    assert "portal-login-qr-image" in content
    assert "portal-login-reload" in content


def test_portal_dashboard_template_supports_one_tap_copy_for_subscription_link():
    content = (TEMPLATE_ROOT / "portal_dashboard.html").read_text(encoding="utf-8")

    assert "data-copy-root" in content
    assert "data-copy-text" in content
    assert "data-copy-target" in content
    assert "data-copy-button" in content
    assert ">Скопировать<" in content
    assert "data-qr-toggle" in content
    assert "subscription-link-preview" in content


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


def test_portal_dashboard_has_mobile_section_navigation():
    portal = (TEMPLATE_ROOT / "portal_dashboard.html").read_text(encoding="utf-8")

    assert "portal-desktop-nav" in portal
    assert "mobile-bottom-nav" in portal
    assert 'href="#portal-settings"' in portal
    assert 'data-portal-page-link="settings"' in portal
    assert 'id="portal-settings"' in portal
    assert "portal-support-fab" in portal
    assert "portal-share-modal" in portal
    assert "portal-plan-modal" in portal
    assert "<svg viewBox=\"0 0 24 24\"" in portal
    assert "data-portal-page-nav" in portal
    assert "Главная" in portal
    assert "Подписка" in portal
    assert "Баланс" in portal
    assert "theme-switcher" in portal
    assert 'data-theme-choice="light"' in portal
    assert 'data-theme-choice="dark"' in portal
    assert 'data-theme-choice="system"' in portal
    assert "responsive-table portal-device-table" in portal
    assert "responsive-table portal-server-table" in portal
    assert "responsive-table portal-payments-table" in portal
    assert "scrollIntoView" in portal
    assert "access_status" in portal
    assert "payment_status" in portal
    assert "support_status" in portal
    assert 'action="/portal/support"' in portal
    assert 'action="/portal/link/revoke"' in portal
    assert 'href="/portal/vless-keys"' in portal
    assert 'data-promo-form' in portal
    assert 'POST"' in portal or 'method: "POST"' in portal
    assert "Пользовательское соглашение" in portal
    assert "Политика конфиденциальности" in portal
    assert "portal-footer" in portal
    assert "admin_comment" not in portal
    assert "user_comment" not in portal
    for page, section_id in (
        ("home", "portal-home"),
        ("subscription", "portal-subscription"),
        ("balance", "portal-balance"),
    ):
        assert f'data-portal-page-link="{page}"' in portal
        assert f'data-portal-page="{page}"' in portal
        assert f'href="#{section_id}"' in portal
        assert f'id="{section_id}"' in portal
    assert "is-portal-page-hidden" in portal


def test_landing_template_keeps_homepage_copy_compact():
    landing = (TEMPLATE_ROOT / "landing.html").read_text(encoding="utf-8")

    assert "<title>{{ title }}</title>" in landing
    assert '<body class="landing-page">' in landing
    assert "Быстрый и конфиденциальный доступ к сети" in landing
    assert "ALTLINK — быстрый и конфиденциальный доступ к сети!" not in landing
    assert "2 дня теста" in landing
    assert "ссылка и QR-код" in landing
    assert "Подойдут v2raytun, Happ, Throne, Hiddify, Streisand или другое совместимое приложение." in landing
    assert "Happ для iOS" not in landing
    assert "landing_max_device_limit" in landing
    assert "price_label" in landing
    assert "landing-story" not in landing
    assert "Что получает пользователь" not in landing
    assert "landing-feature-card" in landing
    assert "landing-step-card" in landing
    assert "landing-location-carousel" in landing
    assert "landing-location-carousel-shell" in landing
    assert "data-horizontal-scroll" in landing
    assert "data-scroll-prev" in landing
    assert "data-scroll-next" in landing
    assert "scrollBy" in landing
    assert 'addEventListener("wheel"' in landing
    assert "landing-location-card" in landing
    assert "landing-location-card-bottom" in landing
    assert "landing-latency-list" not in landing
    assert "landing-plan-card{% if group.family == 'unlimited' %} is-featured{% endif %}" in landing
    assert "landing-plan-devices" in landing
    assert "{{ landing_account_button_label }}" in landing
    assert "Соглашение" in landing
    assert "Конфиденциальность" in landing
    assert "Конфиденциальность без громких обещаний" in landing
    assert "Мы не продаём данные пользователей третьим лицам" in landing
    assert "Технические данные используются только для работы сервиса" in landing
    assert "полная анонимность" not in landing.lower()
    assert "мы ничего не храним" not in landing.lower()
    assert "абсолютная безопасность" not in landing.lower()
    assert "© 2026 ALTLINK" in landing
    assert "Подключить" in landing
    assert ">Telegram bot<" in landing
    assert "Открыть инструкцию" in landing
    assert "Написать в поддержку" in landing
    assert 'href="{{ support_url }}"' in landing
    assert "landing_location_items" in landing
    assert "country_flag" in landing
    assert "country_name" in landing
    assert "Быстрое соединение" in landing
    assert "Стабильное соединение" in landing
    assert "Временно высокая задержка" in landing


def test_theme_bootstrap_defaults_to_light_and_uses_data_theme():
    partial = (TEMPLATE_ROOT / "_theme_bootstrap.html").read_text(encoding="utf-8")
    style = Path("src/altlink/presentation/web/static/style.css").read_text(encoding="utf-8")

    for template_name in ASSET_TEMPLATES:
        content = (TEMPLATE_ROOT / template_name).read_text(encoding="utf-8")
        assert 'data-theme="light"' in content
        assert '{% include "_theme_bootstrap.html" %}' in content

    assert "altlink-theme" in partial
    assert 'const mobileQuery = window.matchMedia ? window.matchMedia("(max-width: 980px)") : null;' in partial
    assert 'const defaultPreference = () => (mobileQuery?.matches ? "system" : "light");' in partial
    assert "return modes.has(saved) ? saved : defaultPreference();" in partial
    assert 'document.documentElement.dataset.theme = theme;' in partial
    assert "prefers-color-scheme" in partial
    assert "@media (prefers-color-scheme: dark)" not in style


def test_admin_dashboard_has_mobile_section_navigation():
    dashboard = (TEMPLATE_ROOT / "dashboard.html").read_text(encoding="utf-8")

    assert 'class="dashboard-section-nav"' in dashboard
    for section_id in (
        "dashboard-overview",
        "dashboard-users",
        "dashboard-load",
        "dashboard-finance",
        "dashboard-plans",
        "dashboard-lists",
    ):
        assert f'href="#{section_id}"' in dashboard
        assert f'id="{section_id}"' in dashboard
    assert 'name="period"' in dashboard
    assert 'name="refresh"' in dashboard
    assert "usersChart" in dashboard
    assert "planSignupsChart" in dashboard
    assert "nodeLoadChart" in dashboard
    assert "hostLoadChart" in dashboard
    assert "trafficChart" in dashboard


def test_admin_users_template_exposes_csrf_protected_node_access_sync():
    users = (TEMPLATE_ROOT / "users.html").read_text(encoding="utf-8")

    assert 'method="post"' in users
    assert 'action="/admin/users/sync-access"' in users
    assert 'name="csrf_token"' in users
    assert "Синхронизировать доступ к нодам" in users
    for field_name in (
        "status",
        "plan",
        "balance_min",
        "last_seen_from",
        "traffic_min",
        "whitelist_traffic_min",
        "node_id",
        "node_traffic_min",
        "next_billing_from",
        "registered_from",
        "devices_min",
        "sort",
        "direction",
        "limit",
    ):
        assert f'name="{field_name}"' in users
    assert "user_page.total" in users
    assert "admin_total_traffic_bytes" in users


def test_web_theme_does_not_use_legacy_yellow_palette():
    style = Path("src/altlink/presentation/web/static/style.css").read_text(encoding="utf-8")
    dashboard = (TEMPLATE_ROOT / "dashboard.html").read_text(encoding="utf-8")
    landing = (TEMPLATE_ROOT / "landing.html").read_text(encoding="utf-8")
    web_theme = f"{style}\n{dashboard}\n{landing}".lower()

    for legacy_token in (
        "gold",
        "/media/background.png",
        "#c79b2f",
        "#d17d26",
        "#087f83",
        "#0876b8",
        "#d95f59",
        "199, 155, 47",
        "209, 125, 38",
        "217, 95, 89",
        "8, 127, 131",
        "5, 127, 140",
        "217, 244, 241",
        "214, 245, 245",
        "246, 191, 56",
    ):
        assert legacy_token not in web_theme
