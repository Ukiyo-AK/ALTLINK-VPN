# ALTLINK VPN

Production-ready система для продажи и управления VPN-бизнесом на базе официального API Remnawave.

В репозитории есть:

- `backend` на FastAPI
- клиентский Telegram-бот `altlink`
- отдельный admin Telegram-бот
- server-rendered web admin panel на FastAPI/Jinja2
- PostgreSQL + Alembic
- scheduler с фоновыми задачами
- Dockerized deployment для одного VPS

## Основные возможности

- регистрация пользователей по `telegram_id`
- тестовый период 2 дня, один раз на пользователя
- два тарифа:
  - безлимит 200 ₽ / 30 дней
  - 50 ГБ 100 ₽ / 30 дней
- внутренний баланс в рублях и история движений
- ручные заявки на пополнение с подтверждением администратором
- grace period 14 дней при нехватке средств
- автоуведомления в Telegram
- sync серверов и inbound'ов из Remnawave
- локальное включение/отключение серверов в системе управления
- получение общей subscription link Remnawave и QR-кода
- web admin panel на русском языке

## Ключевые ограничения Remnawave API

Решение работает только через официальный API Remnawave. При этом честно учтены ограничения API:

- throttle/speed-limit пользователя на 5 Мбит/с в grace period не реализован, потому что официальный API Remnawave не предоставляет безопасный и явный endpoint для этого
- отдельную subscription URL на каждый сервер официальный API не отдает
  - поэтому в клиентском боте используется нативная общая subscription link Remnawave
  - список серверов и их статусы показываются отдельно
- в онлайне `inbound` и device metadata доступны только настолько, насколько их реально возвращают официальные endpoints

## Архитектура

Проект разделен на слои:

- `src/altlink/domain` — enum'ы, константы, policy-функции
- `src/altlink/application` — бизнес-логика и use-case сервисы
- `src/altlink/infrastructure` — SQLAlchemy модели, сессии, Remnawave client
- `src/altlink/presentation` — FastAPI API, web admin panel, Telegram-боты
- `src/altlink/scheduler` — фоновые задачи

## Технологии

- Python 3.12+
- FastAPI
- aiogram 3.x
- SQLAlchemy 2.x + Alembic
- PostgreSQL
- Redis
- APScheduler
- Docker / Docker Compose

## Быстрый старт

1. Скопируйте `.env.example` в `.env` и заполните переменные.
2. Поднимите сервисы:

```bash
docker compose up -d --build
```

3. Выполните миграции:

```bash
docker compose run --rm backend python -m alembic upgrade head
```

4. Засейдите системные данные:

```bash
docker compose run --rm backend python -m altlink.cli seed-defaults
```

5. Создайте первого администратора:

```bash
docker compose run --rm backend python -m altlink.cli create-admin --username admin --telegram-id 123456789 --full-name "Main Admin"
```

После этого:

- backend и web admin panel будут доступны на `http://YOUR_HOST:8000`
- оба бота начнут работать в polling mode

## Документация

- [DEPLOYMENT.md](DEPLOYMENT.md) — подробный деплой на один VPS
- [BOT_USAGE.md](BOT_USAGE.md) — инструкция по клиентскому боту
- [ADMIN_USAGE.md](ADMIN_USAGE.md) — инструкция по admin bot и web panel

## Тесты

Текущий набор включает:

- unit tests для billing / grace / trial / limited plan / notification thresholds
- integration tests для trial activation, topup approval и ухода в grace period

Запуск:

```bash
python -m pytest
```

## Проверка в этой сессии

Локально выполнено:

- `python -m compileall src`
- `python -m pytest tests/unit tests/integration -q`

Результат: `16 passed`
