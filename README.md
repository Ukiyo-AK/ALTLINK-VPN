# ALTLINK VPN

ALTLINK VPN — production-ready система для продажи и управления VPN на базе официального API Remnawave.

В одном репозитории собраны:

- FastAPI backend
- клиентский Telegram-бот `altlink`
- отдельный admin Telegram-бот
- server-rendered web admin panel
- пользовательский web portal, синхронизированный с ботом
- PostgreSQL + Alembic
- APScheduler для фоновых задач
- Docker Compose стек для одного VPS

Проект рассчитан на один VPS с 2 GB RAM и сознательно сделан прагматичным:

- без Redis
- без Celery
- без тяжёлого SPA
- long polling для обоих ботов
- единый Python-код, который запускается в разных режимах

## Что умеет система

- регистрация пользователя по `telegram_id`
- обязательная подписка на Telegram-канал перед началом использования
- Telegram Login Widget для входа в пользовательский web portal
- синхронизация сайта и бота через одну БД и один `telegram_id`
- trial на 2 дня
- тариф `Один сервер 10 Гбит` за `69 ₽ / 30 дней`
- тариф `Безлимит` за `200 ₽ / 30 дней`
- ежедневные списания: месячная цена делится на 30 дней
- grace period на 14 дней при нехватке средств
- внутренний рублёвый баланс
- автоматическое пополнение через stub-платёж
- история платежей и транзакций
- автоматическое назначение наименее загруженного сервера `10 Гбит`
- три типа серверов: `10 Гбит`, `Белые списки`, `Обычный`
- для тарифа `69 ₽`: один выделенный `10 Гбит` сервер + доступ к `Белым спискам`
- `Белые списки` тарифицируются отдельно по `4 ₽ / ГБ`
- для тарифа `200 ₽`: доступ ко всем активным серверам
- реальные ограничения доступа через `activeInternalSquads` Remnawave
- subscription link и QR-код для пользователя
- аналитика по статусам, трафику, долгам, выручке и загрузке серверов

## Remnawave

Система работает только через официальный API Remnawave.

Используется официально поддерживаемый механизм `activeInternalSquads` для ограничения доступа пользователя к нужным нодам. За счёт этого:

- тариф `69 ₽` действительно получает один назначенный `10 Гбит` сервер
- whitelist-доступ выдаётся отдельно
- `Безлимит` получает все активные серверы

Официальный backend Remnawave, по которому сверялись контракты:

- https://github.com/remnawave/backend

## Архитектура

```text
src/altlink/
  domain/            # enums, биллинг, уведомления, тарифные правила
  application/       # сервисы и use case'ы
  infrastructure/    # SQLAlchemy модели, Remnawave client
  presentation/      # FastAPI, web routes, Telegram-боты
  scheduler/         # фоновые задачи
```

## Быстрый старт

1. Скопируйте `.env.example` в `.env` и заполните токены.
2. Поднимите контейнеры:

```bash
docker compose up -d --build
```

3. Прогоните миграции:

```bash
docker compose run --rm backend python -m alembic upgrade head
```

4. Заполните базовые тарифы и системные настройки:

```bash
docker compose run --rm backend python -m altlink.cli seed-defaults
```

5. Создайте первого администратора:

```bash
docker compose run --rm backend python -m altlink.cli create-admin --username admin --telegram-id 123456789 --full-name "Main Admin"
```

После этого:

- админка будет доступна на `/admin/login`
- пользовательский кабинет — на `/portal`
- оба бота запустятся в long polling

## Документация

- [DEPLOYMENT.md](DEPLOYMENT.md)
- [BOT_USAGE.md](BOT_USAGE.md)
- [ADMIN_USAGE.md](ADMIN_USAGE.md)

## Тесты

Локально проверено:

```bash
python -m pytest tests/unit tests/integration -q
```

Результат в этой сессии: `11 passed`
