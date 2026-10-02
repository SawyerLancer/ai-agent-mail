---
name: deploy-vps
description: Выкатка бота на VPS через docker compose — .env на сервере, режим webhook с Caddy, логи, рестарт, обновление и health-check. Применяй, когда разворачиваешь, обновляешь или чинишь бота на сервере.
---

# Деплой на VPS

Файлы: `Dockerfile`, `docker-compose.yml`, `Caddyfile`, `.env.example`.
Настройки ящика — `yandex-mail-setup` / `mail-mcp`; режимы событий — `pachca-api`.

## Как сейчас

### Образ
`python:3.12-slim`, `pip install -r requirements.txt` (в том числе
`mcp-email-server` — он запускается внутри того же контейнера дочерним процессом),
пользователь `bot` (uid 10001), `uvicorn app.main:app --port 8000 --proxy-headers`.

### Сервисы
- `bot` — `restart: unless-stopped`, `env_file: .env`, том `bot-data:/data`
  (SQLite: точка поллинга, письма, черновики). Порт 8000 только `expose`, наружу
  не публикуется.
- `caddy` — только в профиле `webhook`: TLS по `DOMAIN`, наружу открыт лишь
  `/webhook`, всё остальное 404.

### .env на сервере
`cp .env.example .env`, заполнить. В git и в образ не попадает (`.gitignore`,
`.dockerignore`). Обязательные: `PACHCA_TOKEN`, `PACHCA_CHANNEL_ID`,
`BOT_USER_ID`, `EMAIL_ACCOUNT`, ключ выбранного LLM, `MCP_EMAIL_SERVER_*`.
Для webhook ещё `DOMAIN`, `PACHCA_SIGNING_SECRET`, `CHECK_SOURCE_IP=false`.

### Команды
```bash
docker compose up -d --build                      # EVENTS_MODE=polling
docker compose --profile webhook up -d --build    # EVENTS_MODE=webhook
docker compose logs -f bot
docker compose restart bot                         # после правки .env
docker compose exec bot mcp-email-server accounts list
```
Обновление версии: `git pull` и та же команда `up -d --build` (отдельного
скрипта нет). Данные на томе переживают пересборку.

### Health-check
- `GET /healthz` → `{"ok": true, "provider": "<LLM_PROVIDER>"}`.
- Docker healthcheck дёргает его каждые 30 с (timeout 5 с, 3 попытки).
- Проверяет только, что жив FastAPI, — не MCP, не IMAP и не Пачку. Живость
  почты видна по логам: «новых писем: N», «сбой в проходе поллинга».

### Логи
Формат `время уровень модуль: сообщение`, уровень `LOG_LEVEL`; `httpx` и
`apscheduler.executors.default` приглушены до WARNING.

## Требования и расхождения
- Только docker compose; варианта с systemd в репо нет.
- Скрипта обновления и отката нет; откат — `git checkout <коммит>` + `up -d --build`.
- `/healthz` не отражает состояние MCP-сессии и поллинга.
- Бэкапа тома `bot-data` нет.
