---
name: pachca-api
description: REST API мессенджера Пачка в этом боте — сообщения, треды, кнопки, события и вебхуки. Применяй, когда меняешь общение бота с Пачкой — отправку, правку или вид сообщений и кнопок, приём событий.
---

# Пачка API

Файлы: `app/pachca.py` (клиент), `app/cards.py` (тексты и кнопки),
`app/events.py` (журнал событий), `app/main.py` (`/webhook`).
Логика черновиков — в skill `draft-review-flow`, проверки прав и
подтверждений — в `email-actions-safety`.

## Как сейчас

### Авторизация и клиент
- База: `PACHCA_BASE_URL` = `https://api.pachca.com/api/shared/v1`.
- Заголовок `Authorization: Bearer <PACHCA_TOKEN>`, один `httpx.AsyncClient`
  на процесс (`pachca = Pachca()`), таймаут 30 с.
- Ответы завёрнуты в `{"data": ...}` — методы клиента возвращают `data`.

### Лимиты и ретраи (`Pachca._request`)
- До 4 попыток. `429` → ждём `Retry-After` (минимум 0,5 с), иначе `2**attempt`.
- `5xx` → пауза `2**attempt` и повтор; `4xx` → `raise_for_status()` сразу.
- В поллере между карточками `asyncio.sleep(0.3)` — под лимит ~4 rps на чат.
- Журнал событий читается раз в `EVENTS_POLL_INTERVAL=3` с (лимит ~5 req/2s).

### Сообщения
```python
await pachca.send_message(entity_id=settings.pachca_channel_id,
                          content=text, buttons=cards.email_buttons(pk))
await pachca.edit_message(message_id, content=..., buttons=[])  # [] убирает кнопки
await pachca.drop_buttons(message_id, content="_Отменено._")     # не бросает ошибок
```
- `POST /messages` с телом `{"message": {entity_id, entity_type, content, buttons?, parent_message_id?}}`.
- `PUT /messages/{id}` — правка; `buttons=None` кнопки не трогает.
- Markdown: `**жирный**`, `_курсив_`.
- Длинный текст режем кусками по 3500 символов, не больше 14000 (`_show_full`).

### Треды
- `POST /messages/{id}/thread` → `{id, chat_id}`; сохраняем оба в `TrackedEmail`.
- Писать в тред: `entity_type="thread"`, `entity_id` = **id треда**, не `chat_id`
  (с `chat_id` будет 404). Входящие сообщения треда приходят с тем же `entity_id`.

### Кнопки
- `buttons` — список рядов `[[{"text", "data"}]]`. Ширина ряда делится поровну,
  подпись не переносится: больше 3 кнопок в ряду обрезаются («Уда…»).
- Лимит `data` — 255 символов. Схема: `mail:<действие>:<pk письма>` и
  `draft:<действие>:<id черновика>`; константы `BTN_*` в `cards.py`.
- Разбор в `handlers.handle_button`: `action, _, raw_id = data.rpartition(":")`.

### Карточка письма (`cards.email_card`)
```
**Тема** (или «(без темы)»)
От: Имя <a@b.ru> · дата
                                   ← если есть
_выжимка от LLM в 1–2 предложения_
                                   ← если есть
превью тела (PREVIEW_CHARS=700)
```
Кнопки: `✉️ Ответить · ↪️ Переслать · 📄 Полностью` / `📥 В архив · 🗑 Удалить`.

### События: два режима (`EVENTS_MODE`)
**polling** (по умолчанию) — `GET /webhooks/events?limit=50`, обработка в обратном
порядке (журнал отдаёт новое сверху), затем `DELETE /webhooks/events/{id}`
**всегда**, даже при ошибке — иначе событие придёт снова. Типы `message_new`,
`button`/`button_click` приводятся к `(type, event)` формата вебхука. Нужна
галочка «Сохранять историю событий» в настройках бота.

**webhook** — `POST /webhook`, проверки по порядку:
1. HMAC-SHA256 от сырого тела ключом `PACHCA_SIGNING_SECRET` против заголовка
   `Pachca-Signature`, `hmac.compare_digest` → иначе 401.
2. `CHECK_SOURCE_IP` → IP == `PACHCA_ALLOWED_IP` (`37.200.70.177`). За Caddy
   выключено: там адрес прокси.
3. `|now - webhook_timestamp| > 60` → 401 (replay).
4. Дедупликация `already_seen(key)`, ключ — sha256 от type/event/id/ts/data.
5. Сразу 200, обработка в `asyncio.create_task(_dispatch(event))`.

Битый JSON — 200 (повторять бессмысленно).

### Свои сообщения
`message_new` с `user_id == BOT_USER_ID` игнорируется; в настройках бота также
включено «Игнорировать свои сообщения».

## Требования и расхождения
- Доставка событий at-least-once в обоих режимах: любой новый обработчик должен
  быть идемпотентным или идти через `already_seen`.
- Кнопку, которая что-то сделала, гасить (`drop_buttons`), чтобы её нельзя было
  нажать повторно.
- Новую кнопку — константой `BTN_*` в `cards.py`, `data` укладывать в 255 символов.
