# DEPLOYMENT

Подробная инструкция по развёртыванию ALTLINK VPN на одном VPS.

## 1. Требования

- VPS с Linux
- 2 GB RAM достаточно для первой production-версии
- Docker и Docker Compose plugin
- домен или IP для доступа к web panel
- токены двух Telegram-ботов
- `base URL` и `API token` от Remnawave

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

Перезайдите в сессию после добавления в группу `docker`.

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
- `REMNAWAVE_BASE_URL`
- `REMNAWAVE_API_TOKEN`
- `BACKEND_PUBLIC_URL`

Для Docker Compose по умолчанию используется:

```env
DATABASE_URL=postgresql+asyncpg://altlink:altlink@postgres:5432/altlink
```

Пример ключевых настроек:

```env
BACKEND_PUBLIC_URL=https://vpn.example.com
CLIENT_BOT_TOKEN=123456:AAAA...
ADMIN_BOT_TOKEN=654321:BBBB...
ADMIN_ALLOWED_TELEGRAM_IDS=123456789
REMNAWAVE_BASE_URL=https://panel.example.com
REMNAWAVE_API_TOKEN=rw_token_here
```

Если Remnawave использует отдельный публичный домен для subscription links, заполните:

```env
REMNAWAVE_SUBSCRIPTION_BASE_URL=https://sub.example.com
```

## 5. Запуск контейнеров

```bash
docker compose up -d --build
```

Проверка статуса:

```bash
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

Команда создаст:

- системные настройки
- тарифы `trial`, `unlimited`, `limited_50gb`

## 8. Создание первого администратора

```bash
docker compose run --rm backend python -m altlink.cli create-admin --username admin --telegram-id 123456789 --full-name "Main Admin"
```

Пароль команда запросит интерактивно.

После этого:

- web panel: `http://YOUR_HOST:8000/admin/login`
- admin bot будет пускать Telegram ID, который есть в `ADMIN_ALLOWED_TELEGRAM_IDS` и/или привязан к `admin_users`

## 9. Webhook или polling

В этой версии система использует только long polling.

Ничего настраивать для webhook не нужно.

Преимущества этого варианта:

- проще deploy
- меньше внешних зависимостей
- удобно для одного VPS

## 10. Проверка работы

Healthcheck backend:

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

## 11. Обновление проекта

```bash
git pull
docker compose up -d --build
docker compose run --rm backend python -m alembic upgrade head
```

Если менялись системные сиды:

```bash
docker compose run --rm backend python -m altlink.cli seed-defaults
```

## 12. Backup PostgreSQL

Резервная копия:

```bash
docker compose exec postgres pg_dump -U altlink -d altlink > backup_$(date +%F_%H-%M-%S).sql
```

Восстановление:

```bash
cat backup.sql | docker compose exec -T postgres psql -U altlink -d altlink
```

## 13. Просмотр логов

```bash
docker compose logs -f backend
docker compose logs -f client-bot
docker compose logs -f admin-bot
docker compose logs -f scheduler
docker compose logs -f postgres
```

## 14. Что делать после запуска

1. Войдите в web panel.
2. Выполните синхронизацию серверов из Remnawave.
3. Убедитесь, что нужные серверы локально включены в продажу.
4. При необходимости задайте `max_clients` для корректного расчёта нагрузки.
5. Протестируйте:
   создание пользователя через client bot
   trial
   заявку на пополнение
   подтверждение заявки через admin bot или web panel
   выдачу subscription link и QR

## 15. Практические замечания

- если вы используете nginx/Caddy перед FastAPI, проксируйте `:8000`
- если хотите HTTPS, лучше закрывать его на reverse proxy
- если Remnawave временно недоступен, ALTLINK не будет выдумывать обходные методы и продолжит работать только в пределах локальной информации и безопасных fallback’ов

