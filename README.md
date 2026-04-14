# ALTLINK VPN

ALTLINK VPN — production-ready система для продажи и управления VPN на базе официального API Remnawave. В одном репозитории собраны:

- FastAPI backend
- client Telegram-бот `altlink`
- отдельный admin Telegram-бот
- server-rendered web admin panel на FastAPI + Jinja2
- PostgreSQL + Alembic
- APScheduler для фоновых задач
- Docker Compose стек для одного VPS

Проект сознательно сделан прагматичным и лёгким для VPS с 2 GB RAM:

- без Redis
- без Celery
- без тяжёлого SPA
- long polling для обоих ботов
- единый Python-код, который запускается в разных режимах

## Что умеет система

- регистрация пользователей по `telegram_id`
- trial на 2 дня, один раз на пользователя
- тариф `Безлимит` за `200 ₽ / 30 дней`
- тариф `50 ГБ` за `100 ₽ / 30 дней`
- внутренний рублёвый баланс
- история движений баланса
- ручные заявки на пополнение с подтверждением админом
- grace period на 14 дней при нехватке денег
- автоматические уведомления пользователю
- синхронизация серверов и inbound'ов из Remnawave
- локальное включение и исключение серверов из продаж
- общая subscription link Remnawave и QR-код для клиента
- web admin panel на русском языке

## Важные ограничения API Remnawave

Система работает только через официальный API Remnawave. В коде и документации честно учтены его ограничения:

- отдельный безопасный speed-limit на 5 Мбит/с для grace mode не включён, потому что в официальном API нет явного и стабильного endpoint для этого сценария
- per-server subscription URL через официальный API не гарантирован, поэтому клиент получает нативную общую subscription link Remnawave и отдельный список доступных серверов
- загрузка сервера считается по пользователям, которыми управляет ALTLINK, и соотносится с локально задаваемым `max_clients`
  это практичный и честный fallback, потому что Remnawave не отдаёт “бизнесовую вместимость сервера” как отдельный контракт API

Контракты API сверялись по официальному backend-репозиторию Remnawave:

- https://github.com/remnawave/backend

## Архитектура

```text
src/altlink/
  domain/            # enums, биллинг, правила уведомлений
  application/       # use cases и сервисы
  infrastructure/    # SQLAlchemy модели, Remnawave client
  presentation/      # FastAPI, web panel, Telegram-боты
  scheduler/         # фоновые задачи
```

## Быстрый старт

1. Скопируйте `.env.example` в `.env` и заполните секреты.
2. Поднимите стек:

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

- web admin panel будет доступна на `http://YOUR_HOST:8000/admin/login`
- backend health endpoints будут доступны на `/health/live` и `/health/ready`
- оба Telegram-бота начнут работать в long polling режиме

## Полезные команды

```bash
make install
make migrate
make seed
make create-admin
make run-backend
make run-client-bot
make run-admin-bot
make run-scheduler
make test
```

## Документация

- [DEPLOYMENT.md](DEPLOYMENT.md) — развёртывание на один VPS
- [BOT_USAGE.md](BOT_USAGE.md) — как пользоваться клиентским ботом
- [ADMIN_USAGE.md](ADMIN_USAGE.md) — как пользоваться admin bot и web admin panel

## Тесты

Локально проверено:

```bash
python -m pytest tests/unit tests/integration -q
```

Результат в этой сессии: `11 passed`

