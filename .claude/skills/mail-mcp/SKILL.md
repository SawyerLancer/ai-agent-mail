---
name: mail-mcp
description: Работа с почтой через open-source MCP-сервер mcp-email-server (IMAP/SMTP, без вендор-лока) — подключение, инструменты, обрывы сессии. Применяй при любой операции с ящиком, ошибках или зависаниях MCP и подключении нового ящика.
---

# Почта через MCP

Единственная точка — `app/mail.py` (`mail = MailClient()`). Своего IMAP/SMTP-кода
в проекте нет и быть не должно. Настройки конкретно Яндекса — skill
`yandex-mail-setup`; правила, какие операции можно вызывать и когда, —
`email-actions-safety`.

## Как сейчас

### Сервер и конфиг
- Пакет `mcp-email-server==1.6.2` (BSD-3-Clause), ставится в тот же образ,
  запускается дочерним процессом: `MCP_COMMAND=mcp-email-server`, `MCP_ARGS=["stdio"]`.
- Ящик задаётся переменными `MCP_EMAIL_SERVER_*` (`ACCOUNT_NAME`, `EMAIL_ADDRESS`,
  `PASSWORD`, `IMAP_HOST/PORT/SSL/START_SSL/VERIFY_SSL/USER_NAME`, такие же
  `SMTP_*`, `SAVE_TO_SENT`, `SENT_FOLDER_NAME`). Полный список — `.env.example`.
- `EMAIL_ACCOUNT` бота **обязан совпадать** с `MCP_EMAIL_SERVER_ACCOUNT_NAME`.
- `stdio_client` пробрасывает ребёнку только HOME/PATH, поэтому `_server_env()`
  явно добавляет все `MCP_EMAIL_SERVER_*` — иначе сервер стартует без аккаунта.
- Проверка: `docker compose exec bot mcp-email-server accounts list`.

### Инструменты, которые вызывает бот
`email_id` — UID строкой (`_eid(uid)`, схема `^[1-9][0-9]*$`).

| Метод `mail.*` | Инструмент MCP | Аргументы |
|---|---|---|
| `list_new(since_uid)` | `list_emails_metadata` | `account_name, mailbox, page, page_size=POLL_PAGE_SIZE, order="desc"` — листает, пока страница целиком новая (до 20) |
| `get_body(uid)` | `get_emails_content` | `account_name, mailbox, email_ids=[id], max_body_length, mark_as_read=False` |
| `send(...)` | `send_email` | `account_name, recipients, subject, body, in_reply_to?, references?` — **без повтора** |
| `forward(...)` | `forward_email` | `account_name, email_id, source_mailbox, recipients, body, include_attachments=True` — **без повтора** |
| `find_sent(to, since, subject?)` | `list_emails_metadata` | `mailbox=<\Sent>, to_address, since (ISO, с зоной), page_size=10, order="desc"`; тема сравнивается без регистра и лишних пробелов |
| `delete(uid)` | `move_emails` | `account_name, email_ids, source_mailbox, destination_mailbox=<\Trash>` |
| `_trash_mailbox()` | `list_mailboxes` | `account_name` → папка с флагом `\Trash`, кэш |
| `archive(uid)` | `archive_emails` | `account_name, mailbox, email_ids` — папка по флагу `\Archive` |
| `mark_read(uid)` | `mark_emails_as_read` | `account_name, mailbox, email_ids` |

Есть в сервере, но бот не вызывает: `delete_emails` (UID EXPUNGE —
безвозвратно, не использовать), `set_email_flags`, `save_to_mailbox`,
`download_attachment`, `list_available_accounts`.

Кодировки заголовков, MIME, вложения и сохранение в «Отправленные» делает сервер —
в боте их не обрабатываем.

### Разбор ответа (`MailClient.call`)
- `isError` → `RuntimeError(f"MCP {tool}: ...")`.
- Сначала `structuredContent`, иначе текст → `json.loads`, иначе строка как есть.
- Список писем достаём `_extract_list(data, ("emails","items","results","data"))` —
  не завязываемся на имя поля.
- Пакетные мутации при частичном успехе возвращают строку `"... result [...]"`
  с per-ID статусами succeeded/failed/unknown; сервер сам их не повторяет.

### Сессия и обрывы
- Транспортом владеет отдельная задача `_runner` (anyio требует входить и выходить
  из cancel scope в одной задаче). Остальные только вызывают `call()`.
- `asyncio.Lock` на все вызовы: stdio не выдерживает параллельных запросов.
- Каждый вызов обёрнут в `wait_for(..., MCP_TIMEOUT=120)`. Без него подвисший IMAP
  держит лок вечно и останавливает поллинг и кнопки.
- Таймаут или исключение → `_close()` и **одна** повторная попытка с новой сессией;
  вторая неудача пробрасывается наверх. `call(..., retry=False)` — без повтора:
  так вызываются `send`/`forward`, иначе после таймаута письмо ушло бы дважды.
- `isError` в ответе → `MailToolError` («сервер сказал нет», операция не выполнена).
  Таймаут (`asyncio.TimeoutError`) и прочие исключения — исход неизвестен.
- Служебные папки — `_special_mailbox(flag)` по флагам RFC 6154 (`\trash`,
  `\sent`) через `list_mailboxes`, кэш на время жизни процесса.
- `send_email` не принимает свои заголовки (Message-ID задать нельзя) — проверено
  по исходникам 1.6.2.
- `_close()` ждёт задачу 10 с, затем `cancel()`.

```python
data = await mail.call("list_mailboxes", {"account_name": settings.email_account})
```

## Требования и расхождения
- Без вендор-лока: только IMAP/SMTP через MCP. Никаких Gmail API / Graph / API Яндекса,
  никаких хардкодов хостов в коде — всё через `MCP_EMAIL_SERVER_*`.
- Новая операция — новым методом `MailClient`, а не прямым `mail.call` из хендлеров.
- Любую новую мутацию с внешним эффектом (отправка) вызывать с `retry=False`.
