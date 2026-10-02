---
name: yandex-mail-setup
description: Подключение Яндекс.Почты (и ящиков Яндекс 360) к боту — пароль приложения, хосты, папки, ошибки входа и отправки. Применяй, когда настраиваешь ящик на Яндексе или разбираешь его ошибки IMAP/SMTP.
---

# Яндекс.Почта

Как переменные попадают в MCP-сервер и что он делает с MIME и кодировками — skill
`mail-mcp`. Здесь только специфика Яндекса.

## Как сейчас (в репо)

Пример из `.env.example`:
```bash
EMAIL_ACCOUNT=yandex                         # = MCP_EMAIL_SERVER_ACCOUNT_NAME
MCP_EMAIL_SERVER_ACCOUNT_NAME=yandex
MCP_EMAIL_SERVER_EMAIL_ADDRESS=you@yandex.ru
MCP_EMAIL_SERVER_PASSWORD=                   # пароль приложения
MCP_EMAIL_SERVER_IMAP_HOST=imap.yandex.ru
MCP_EMAIL_SERVER_IMAP_PORT=993
MCP_EMAIL_SERVER_IMAP_SSL=true
MCP_EMAIL_SERVER_IMAP_USER_NAME=you@yandex.ru
MCP_EMAIL_SERVER_SMTP_HOST=smtp.yandex.ru
MCP_EMAIL_SERVER_SMTP_PORT=465
MCP_EMAIL_SERVER_SMTP_SSL=true
MCP_EMAIL_SERVER_SMTP_USER_NAME=you@yandex.ru
MCP_EMAIL_SERVER_SAVE_TO_SENT=true
MCP_EMAIL_SERVER_SENT_FOLDER_NAME=Sent
```
Проверка: `docker compose exec bot mcp-email-server accounts list`.

## Хосты и порты
_Источник: yandex.ru/support/yandex-360/customers/mail/ru/mail-clients/others, проверено 2026-10-02._

| | Хост | Порт | Защита |
|---|---|---|---|
| IMAP | `imap.yandex.ru` (вне России — `imap.ya.ru`) | 993 | SSL |
| SMTP | `smtp.yandex.ru` | 465 (587 — STARTTLS) | SSL |

POP3 Яндекс больше не развивает — только IMAP.

## Пароль приложения
_Источник: тот же раздел + …/mail-clients/mail-clients-troubleshooting, проверено 2026-10-02._

1. Почта → Настройки → «Почтовые программы»: включить
   «С сервера imap.yandex.ru по протоколу IMAP» и
   «Пароли приложений и OAuth-токены».
2. Яндекс ID → Безопасность → Пароли приложений → создать пароль типа
   «Почта (IMAP, POP3, SMTP)».
3. Пароль показывается **один раз** — сразу в `MCP_EMAIL_SERVER_PASSWORD`.

Обычный пароль от аккаунта по IMAP/SMTP не работает.

## Системные папки
_Источник: …/mail-clients/mail-clients-troubleshooting, проверено 2026-10-02._

В интерфейсе: «Входящие», «Отправленные», «Черновики», «Удалённые» (хранятся
30 дней), «Спам» (10 дней).

Английские IMAP-имена в официальной документации **не указаны**. В репо для
отправленных используется `Sent` (`.env.example`). Перед тем как хардкодить имя
папки (например, корзины для `move_emails`), сверяй через `list_mailboxes`
(skill `mail-mcp`) — смотри флаги RFC 6154 (`\Sent`, `\Trash`, `\Junk`,
`\Drafts`), а не имя.

## Ошибки входа и отправки
_Источник: …/mail-clients/mail-clients-troubleshooting, проверено 2026-10-02._

| Сообщение | Причина | Что делать |
|---|---|---|
| IMAP `AUTHENTICATIONFAILED` / «Нет соединения с сервером» | пароль аккаунта вместо пароля приложения; IMAP или «Пароли приложений» выключены; неверный логин | п. «Пароль приложения»; `*_USER_NAME` = полный адрес |
| `Authentication required`, `Send auth command first`, `Sender address rejected: Access denied` | SMTP без авторизации | проверить `SMTP_USER_NAME`/`PASSWORD` |
| `Sender address rejected: not owned by auth user` | `EMAIL_ADDRESS` не совпадает с аккаунтом входа | выровнять `EMAIL_ADDRESS` и `*_USER_NAME` |
| `Bad address mailbox syntax` | битый адрес отправителя | проверить `EMAIL_ADDRESS` |
| `Message rejected under suspicion of SPAM` | антиспам Яндекса | отправить письмо из веб-интерфейса, повторить |
| ошибка сертификата | SSL | оставить `*_VERIFY_SSL=true`, проверить хост |

Точный текст `AUTHENTICATIONFAILED` в документации Яндекса не приведён — смотри
полный ответ сервера в `docker compose logs bot`.

## Требования и расхождения
- В коде бота не должно быть ничего специфичного для Яндекса — только `.env`.
- Имя корзины для будущего «удаления в корзину» не хардкодить — определять по флагу
  `\Trash` (см. `email-actions-safety`).
