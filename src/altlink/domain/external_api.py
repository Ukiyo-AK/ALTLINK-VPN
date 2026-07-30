from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExternalApiScopeDefinition:
    key: str
    label: str
    description: str
    category: str


EXTERNAL_API_SCOPE_DEFINITIONS = (
    ExternalApiScopeDefinition(
        key="users.telegram_id",
        label="Telegram ID",
        description="Telegram ID пользователя.",
        category="Пользователи",
    ),
    ExternalApiScopeDefinition(
        key="users.status",
        label="Статус доступа",
        description="Статус аккаунта и признак действующего доступа.",
        category="Пользователи",
    ),
    ExternalApiScopeDefinition(
        key="users.plan",
        label="Тариф",
        description="Код, название и тип текущего тарифа.",
        category="Пользователи",
    ),
    ExternalApiScopeDefinition(
        key="users.profile",
        label="Профиль",
        description="Username, имя, язык, дата регистрации и последняя активность.",
        category="Пользователи",
    ),
    ExternalApiScopeDefinition(
        key="users.balance",
        label="Баланс",
        description="Текущий баланс пользователя в рублях.",
        category="Пользователи",
    ),
    ExternalApiScopeDefinition(
        key="users.subscription",
        label="Подписка",
        description="Статус, даты, автопродление и лимит устройств текущей подписки.",
        category="Пользователи",
    ),
    ExternalApiScopeDefinition(
        key="users.traffic",
        label="Трафик",
        description="Использованный обычный и whitelist-трафик текущей подписки.",
        category="Пользователи",
    ),
    ExternalApiScopeDefinition(
        key="users.devices",
        label="Устройства",
        description="Количество обнаруженных устройств и время последней проверки.",
        category="Пользователи",
    ),
    ExternalApiScopeDefinition(
        key="users.referrals",
        label="Реферальные данные",
        description="Реферальный код и состояние начисления награды.",
        category="Пользователи",
    ),
)

EXTERNAL_API_SCOPES = frozenset(item.key for item in EXTERNAL_API_SCOPE_DEFINITIONS)
EXTERNAL_API_USER_SCOPES = frozenset(
    scope for scope in EXTERNAL_API_SCOPES if scope.startswith("users.")
)
EXTERNAL_API_RECOMMENDED_SCOPES = frozenset(
    {
        "users.telegram_id",
        "users.status",
        "users.plan",
    }
)


def normalize_external_api_scopes(scopes: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    return sorted({str(scope).strip() for scope in scopes if str(scope).strip()})

