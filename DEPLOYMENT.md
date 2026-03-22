# DEPLOYMENT

Подробная инструкция по развёртыванию ALTLINK VPN на одном VPS.

## 1. Требования

- Ubuntu 22.04 / 24.04 или другой Linux с Docker
- 2 vCPU+
- 4 GB RAM+
- 20 GB SSD+
- домен или поддомен для web admin panel
- действующий bot token для клиентского бота
- действующий bot token для admin-бота
- официальный `API token` Remnawave
- доступ администратора к Telegram ID

## 2. Установка Docker и Docker Compose

Пример для Ubuntu:

```bash
sudo apt update
sudo apt install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
```

Проверка:

```bash
docker --version
docker compose version
```

## 3. Клонирование проекта

```bash
git clone https://github.com/YOUR_ORG/ALTLINK-VPN.git
cd ALTLINK-VPN
```

## 4. Подготовка `.env`

Скопируйте шаблон:

```bash
cp .env.example .env
```

Обязательно заполните:

- `SECRET_KEY`
- `PUBLIC_BASE_URL`
- `ADMIN_PANEL_BASE_URL`
- `DATABASE_URL`
- `REDIS_URL`
- `REMWAVE_BASE_URL`
- `REMWAVE_API_TOKEN`
- `CLIENT_BOT_TOKEN`
- `ADMIN_BOT_TOKEN`
- `ADMIN_TELEGRAM_IDS`

Рекомендуемый формат:

```env
APP_ENV=production
DEBUG=false
JSON_LOGS=true
SECRET_KEY=replace-with-long-random-secret
PUBLIC_BASE_URL=https://vpn.example.com
ADMIN_PANEL_BASE_URL=https://vpn.example.com
DATABASE_URL=postgresql+asyncpg://altlink:strong-password@postgres:5432/altlink
REDIS_URL=redis://redis:6379/0
REMWAVE_BASE_URL=https://panel.example.com
REMWAVE_API_TOKEN=official-remnawave-token
CLIENT_BOT_TOKEN=123456:client-token
ADMIN_BOT_TOKEN=123456:admin-token
ADMIN_TELEGRAM_IDS=123456789,987654321
TIMEZONE=Europe/Moscow
```

## 5. Подключение Remnawave API token

В панели Remnawave:

1. создайте официальный API token
2. убедитесь, что токен имеет доступ к `users`, `nodes`, `config-profiles`, `subscriptions`, `system`, `bandwidth-stats`
3. укажите:
   - `REMWAVE_BASE_URL=https://your-remnawave-panel`
   - `REMWAVE_API_TOKEN=<token>`

Важно:

- ALTLINK не читает БД Remnawave напрямую
- ALTLINK не парсит web UI Remnawave
- вся интеграция идет только через официальный API

## 6. Запуск стека

```bash
docker compose up -d --build
```

Проверить состояние:

```bash
docker compose ps
```

Сервисы:

- `postgres`
- `redis`
- `backend`
- `scheduler`
- `client-bot`
- `admin-bot`

## 7. Применение миграций

```bash
docker compose run --rm backend python -m alembic upgrade head
```

## 8. Инициализация системных данных

```bash
docker compose run --rm backend python -m altlink.cli seed-defaults
```

Эта команда создаст:

- стандартные тарифы
- системные настройки порогов уведомлений
- базовые operational значения

## 9. Создание первого администратора

```bash
docker compose run --rm backend python -m altlink.cli create-admin --username admin --telegram-id 123456789 --full-name "Main Admin"
```

Пароль будет запрошен интерактивно.

После этого:

- web admin panel использует этот логин/пароль
- admin bot пустит `telegram_id`, если он есть в `admin_users` или в `ADMIN_TELEGRAM_IDS`

## 10. Polling или webhook

В текущей production-конфигурации проект использует `polling`.

Это значит:

- отдельные контейнеры `client-bot` и `admin-bot` постоянно читают Telegram updates
- дополнительные webhook endpoint'ы не требуются
- reverse proxy для Telegram webhook не нужен

Команды запуска polling уже входят в Compose:

```bash
docker compose up -d client-bot admin-bot
```

Если позже понадобится webhook-mode, его лучше добавлять отдельной задачей через backend ingress и TLS reverse proxy. В текущем репозитории рабочим и документированным режимом считается polling.

## 11. Проверка, что всё работает

Проверьте backend:

```bash
curl http://127.0.0.1:8000/health
```

Ожидаемый ответ:

```json
{"status":"ok","app":"ALTLINK VPN","env":"production"}
```

Проверьте web panel:

- откройте `https://your-domain`
- войдите под созданным admin user

Проверьте scheduler:

```bash
docker compose logs -f scheduler
```

Проверьте клиентский бот:

- откройте бот в Telegram
- нажмите `/start`
- убедитесь, что открывается русское меню

Проверьте admin bot:

- откройте admin bot
- убедитесь, что доступ разрешён вашему `telegram_id`

## 12. Первичная эксплуатация после запуска

Рекомендуемый порядок:

1. Синхронизируйте сервера из Remnawave через web panel или CLI:

```bash
docker compose run --rm backend python -m altlink.cli sync-servers
```

2. Откройте раздел `Серверы`
3. Включите локально нужные серверы
4. Проверьте, что Dashboard показывает серверную нагрузку
5. Протестируйте:
   - тестовый период
   - заявку на пополнение
   - подтверждение пополнения
   - активацию тарифа

## 13. Логи

Смотреть все:

```bash
docker compose logs -f
```

Конкретно backend:

```bash
docker compose logs -f backend
```

Конкретно scheduler:

```bash
docker compose logs -f scheduler
```

Конкретно ботов:

```bash
docker compose logs -f client-bot
docker compose logs -f admin-bot
```

## 14. Обновление проекта

```bash
git pull
docker compose build
docker compose up -d
docker compose run --rm backend python -m alembic upgrade head
docker compose run --rm backend python -m altlink.cli seed-defaults
```

После обновления проверьте:

- `/health`
- вход в admin panel
- `docker compose logs -f scheduler`

## 15. Backup PostgreSQL

Создать backup:

```bash
docker compose exec -T postgres pg_dump -U altlink -d altlink > backup_$(date +%F_%H-%M-%S).sql
```

Восстановить:

```bash
cat backup.sql | docker compose exec -T postgres psql -U altlink -d altlink
```

Рекомендуется:

- хранить ежедневные backups вне VPS
- делать backup перед обновлением и миграциями

## 16. Полезные команды

Перезапуск backend:

```bash
docker compose restart backend
```

Перезапуск scheduler:

```bash
docker compose restart scheduler
```

Перезапуск ботов:

```bash
docker compose restart client-bot admin-bot
```

Остановить всё:

```bash
docker compose down
```

## 17. Operational notes

- grace speed limiting до 5 Мбит/с не включается, потому что официальный API Remnawave не предоставляет надёжный endpoint для этого
- per-server отдельная subscription URL не генерируется, потому что официальный API Remnawave отдает нативную общую подписку
- онлайн `inbound` может отображаться как `API не предоставляет`, если Remnawave не возвращает поле явно
