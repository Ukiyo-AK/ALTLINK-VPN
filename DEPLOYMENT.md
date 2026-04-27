# DEPLOYMENT

Подробная инструкция по развёртыванию ALTLINK VPN на одном VPS.

## 1. Требования

- Linux VPS
- 2 GB RAM достаточно для первой production-версии
- Docker + Docker Compose plugin
- домен или IP для доступа к сайту
- токены двух Telegram-ботов
- `base URL` и `API token` от Remnawave
- Telegram-канал, на который пользователь должен подписаться

## 2. Установка Docker

Пример для Ubuntu:

```bash
sudo apt update
sudo apt install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo \"$VERSION_CODENAME\") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER
```

Перезайдите в shell после добавления пользователя в группу `docker`.

## 3. Подготовка проекта

```bash
git clone <YOUR_REPO_URL> altlink-vpn
cd altlink-vpn
cp .env.example .env
```

## 4. Заполнение `.env`

Обязательные переменные:

- `SECRET_KEY`
- `SESSION_SECRET_KEY`
- `ADMIN_API_KEY`
- `DATABASE_URL`
- `CLIENT_BOT_TOKEN`
- `ADMIN_BOT_TOKEN`
- `ADMIN_ALLOWED_TELEGRAM_IDS`
- `CLIENT_BOT_NAME`
- `REMNAWAVE_BASE_URL`
- `REMNAWAVE_API_TOKEN`
- `BACKEND_PUBLIC_URL`
- `REQUIRED_SUBSCRIPTION_CHANNEL`
- `REQUIRED_SUBSCRIPTION_CHANNEL_URL`

Используйте `DATABASE_URL` PostgreSQL для Docker Compose:

```env
DATABASE_URL=postgresql+asyncpg://altlink:altlink@postgres:5432/altlink
```

Production-конфигурация теперь рассчитана на PostgreSQL. Если оставить `sqlite`, приложение не запустится в `ENVIRONMENT=production`.

Пример ключевых настроек:

```env
BACKEND_PUBLIC_URL=https://vpn.example.com
CLIENT_BOT_TOKEN=123456:AAAA...
ADMIN_BOT_TOKEN=654321:BBBB...
CLIENT_BOT_NAME=altlink
ADMIN_ALLOWED_TELEGRAM_IDS=123456789
REQUIRED_SUBSCRIPTION_CHANNEL=@altlink_channel
REQUIRED_SUBSCRIPTION_CHANNEL_URL=https://t.me/altlink_channel
REMNAWAVE_BASE_URL=https://panel.example.com
REMNAWAVE_API_TOKEN=rw_token_here
```

Если Remnawave использует отдельный публичный домен для subscription links:

```env
REMNAWAVE_SUBSCRIPTION_BASE_URL=https://sub.example.com
```

## 5. Запуск контейнеров

```bash
docker compose up -d --build
docker compose ps
```

## 6. Миграции

```bash
docker compose run --rm backend python -m alembic upgrade head
```

## 7. Базовые данные

```bash
docker compose run --rm backend python -m altlink.cli seed-defaults
```

Команда создаст базовые тарифы:

- `trial`
- `single_10gbit`
- `unlimited`

## 8. Создание первого администратора

```bash
docker compose run --rm backend python -m altlink.cli create-admin --username admin --telegram-id 123456789 --full-name "Main Admin"
```

Пароль команда запросит интерактивно.

## 9. Webhook или polling

В этой версии используются только long polling и фоновые cron-like задачи через APScheduler.

Webhook настраивать не нужно.

## 10. Проверка работы

Health endpoints:

```bash
curl http://127.0.0.1:8000/health/live
curl http://127.0.0.1:8000/health/ready
```

Ожидаемо:

- `/health/live` -> `200`
- `/health/ready` -> `200`, если БД доступна и Remnawave отвечает

Проверка логов:

```bash
docker compose logs -f backend
docker compose logs -f client-bot
docker compose logs -f admin-bot
docker compose logs -f scheduler
```

## 11. Что открыть после запуска

- админка: `https://YOUR_HOST/admin/login`
- пользовательский кабинет: `https://YOUR_HOST/portal`
- бот: `https://t.me/<CLIENT_BOT_NAME>`

## 12. Как работает верификация на сайте

Для входа в пользовательский кабинет используется Telegram Login Widget.

Почему выбран именно он:

- это официальный способ подтверждения личности через Telegram
- сайт и бот получают один и тот же `telegram_id`
- не нужно отдельное хранение паролей пользователя
- синхронизация бота и сайта получается естественной

Что нужно сделать для production:

1. Убедиться, что у бота есть корректный username.
2. Указать `BACKEND_PUBLIC_URL` с правильным доменом.
3. Настроить разрешённый домен у бота через BotFather для Telegram Login Widget.

## 13. Сценарий первичной настройки

1. Зайдите в `/admin/login`.
2. Выполните sync серверов из Remnawave.
3. Назначьте каждому серверу тип:
   - `ten_gbit`
   - `whitelist`
   - `regular`
4. При необходимости задайте `max_clients`.
5. Проверьте пользовательский flow:
   - вход в бот
   - подписка на канал
   - trial
   - вход на сайт через Telegram
   - auto-stub пополнение
   - активация тарифа
   - получение subscription link и QR

## 14. Обновление проекта

```bash
git pull
docker compose up -d --build
docker compose run --rm backend python -m alembic upgrade head
docker compose run --rm backend python -m altlink.cli seed-defaults
```

## 15. Backup PostgreSQL

Резервная копия:

```bash
docker compose exec postgres pg_dump -U altlink -d altlink > backup_$(date +%F_%H-%M-%S).sql
```

Восстановление:

```bash
cat backup.sql | docker compose exec -T postgres psql -U altlink -d altlink
```

## 16. Практические замечания

- для HTTPS лучше поставить nginx или Caddy перед FastAPI
- если вы обновляетесь со старой beta-схемы, перед миграцией стоит сделать backup
- эта версия меняет продуктовую модель тарифов и доступа, поэтому после обновления разумно прогнать `seed-defaults` и проверить типы серверов в админке
