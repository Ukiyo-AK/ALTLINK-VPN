# ALTLINK External API v1

Внешний API предназначен для серверных интеграций с другими сервисами владельца ALTLINK. API-клиенты и их разрешения создаются в админ-панели:

`Админ-панель -> API`

## Базовый URL

```text
https://altlink.online/api/external/v1
```

Версия закреплена в URL. Несовместимые изменения будут опубликованы под новым префиксом, например `/api/external/v2`.

## Авторизация

В каждый запрос необходимо передавать персональный ключ:

```http
X-API-Key: altlink_префикс_секрет
```

Пример:

```bash
curl "https://altlink.online/api/external/v1/users" \
  -H "X-API-Key: $ALTLINK_API_KEY"
```

Ключ:

- показывается только один раз после создания или перевыпуска;
- хранится в базе только в виде SHA-256 хеша;
- нельзя передавать в query string;
- нельзя размещать во frontend-коде или мобильном приложении;
- следует хранить в secret/environment-переменной backend-сервиса.

При утечке ключ нужно немедленно отключить или перевыпустить в админ-панели.

## Разрешения

Ответ содержит только поля, разрешённые scopes конкретного API-клиента.

| Scope | Данные |
|---|---|
| `users.telegram_id` | Telegram ID |
| `users.status` | Статус аккаунта и `access_active` |
| `users.plan` | Текущий тариф |
| `users.profile` | Username, имя, язык, регистрация и последняя активность |
| `users.balance` | Баланс в рублях |
| `users.subscription` | Статус и даты подписки, автопродление, лимит устройств |
| `users.traffic` | Общий и whitelist-трафик |
| `users.devices` | Количество устройств |
| `users.referrals` | Реферальный код и состояние награды |

Для сервиса, который бесплатен действующим пользователям ALTLINK, достаточно:

```text
users.telegram_id
users.status
users.plan
```

## Проверка API-клиента

```http
GET /client
```

Ответ:

```json
{
  "id": "api-client-uuid",
  "name": "Новый бесплатный сервис",
  "scopes": [
    "users.plan",
    "users.status",
    "users.telegram_id"
  ],
  "expires_at": null
}
```

Секрет ключа никогда не возвращается.

## Список пользователей

```http
GET /users
```

Query-параметры:

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `active_only` | boolean | `true` | Только пользователи с действующим доступом |
| `plan_code` | string | отсутствует | Фильтр по текущему тарифу |
| `limit` | integer | `100` | Размер страницы от 1 до 200 |
| `offset` | integer | `0` | Смещение |

Под действующим доступом понимается:

- активная платная подписка, срок которой не истёк;
- активный тестовый период, срок которого не истёк;
- действующий льготный период.

Заблокированный или отключённый аккаунт не считается активным, даже если в базе
ещё осталась неистёкшая запись подписки.

Пример:

```http
GET /users?active_only=true&limit=100&offset=0
X-API-Key: altlink_префикс_секрет
```

Ответ для клиента с тремя основными scopes:

```json
{
  "items": [
    {
      "id": "36c9e70d-0000-0000-0000-000000000000",
      "updated_at": "2026-07-30T12:00:00Z",
      "telegram_id": 123456789,
      "status": "active",
      "access_active": true,
      "plan": {
        "code": "unlimited",
        "name": "Pro • ежемесячно",
        "is_trial": false,
        "period_days": 30
      }
    }
  ],
  "meta": {
    "limit": 100,
    "offset": 0,
    "count": 1,
    "has_more": false,
    "next_offset": null,
    "active_only": true,
    "granted_fields": [
      "users.plan",
      "users.status",
      "users.telegram_id"
    ]
  }
}
```

Для определения права на бесплатный сервис используйте `access_active`, а не только строковый `status`.

### Пагинация

Если `meta.has_more` равно `true`, выполните следующий запрос с `offset`, равным `meta.next_offset`.

Не следует устанавливать `active_only=false`, если интеграции не нужны бывшие и незарегистрированные пользователи.

## Один пользователь

```http
GET /users/{user_id}
```

Возвращает тот же набор полей, ограниченный scopes API-клиента.

## Поиск по Telegram ID

```http
GET /users/by-telegram/{telegram_id}
```

Это рекомендуемый endpoint для проверки пользователя при входе в другой сервис. Он требует scope `users.telegram_id`.

Для принятия решения о бесплатном доступе ключу также следует выдать:

- `users.status` для поля `access_active`;
- `users.plan` для информации о тарифе.

Пример:

```bash
curl "https://altlink.online/api/external/v1/users/by-telegram/123456789" \
  -H "X-API-Key: $ALTLINK_API_KEY"
```

## Формат данных

- Даты передаются в ISO 8601 и UTC.
- Денежные значения передаются строками с двумя знаками после запятой.
- Трафик передаётся в байтах.
- Поля без разрешения отсутствуют в ответе.
- Разрешённые, но отсутствующие данные могут быть `null` или исключены из JSON.

## HTTP-ошибки

| HTTP | Описание |
|---|---|
| `401` | Ключ отсутствует, неверен, отключён, отозван или истёк |
| `403` | Нет разрешения на запрошенную группу данных |
| `404` | Объект не найден |
| `422` | Ошибка query-параметров |
| `500` | Внутренняя ошибка |

Стандартный ответ:

```json
{
  "detail": "Описание ошибки"
}
```

## Python

```python
import os

import httpx


def load_active_altlink_users() -> list[dict]:
    base_url = "https://altlink.online/api/external/v1"
    headers = {"X-API-Key": os.environ["ALTLINK_API_KEY"]}
    users: list[dict] = []
    offset = 0

    with httpx.Client(timeout=10) as client:
        while True:
            response = client.get(
                f"{base_url}/users",
                headers=headers,
                params={
                    "active_only": "true",
                    "limit": 200,
                    "offset": offset,
                },
            )
            response.raise_for_status()
            payload = response.json()
            users.extend(payload["items"])
            if not payload["meta"]["has_more"]:
                break
            offset = payload["meta"]["next_offset"]

    return users
```

## Рекомендуемая интеграция бесплатного сервиса

1. Создать отдельного API-клиента.
2. Выдать только `users.telegram_id`, `users.status`, `users.plan`.
3. Хранить ключ только на backend нового сервиса.
4. При входе пользователя найти запись по Telegram ID среди активных пользователей.
5. Проверить `access_active == true`.
6. При необходимости использовать `plan.code` для разных уровней доступа.
7. Периодически обновлять локальный кеш и не запрашивать весь список на каждое действие пользователя.

Интерактивная OpenAPI-схема доступна по адресу:

`https://altlink.online/docs#/external-api`
