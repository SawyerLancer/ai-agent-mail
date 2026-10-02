---
name: project-conventions
description: Структура репозитория, локальный запуск, стиль кода и формат коммитов проекта. Применяй, когда пишешь новый код, заводишь файл или делаешь коммит.
---

# Соглашения проекта

## Как сейчас

### Структура
| Файл | Роль |
|---|---|
| `app/main.py` | FastAPI, lifespan, планировщик, `/webhook`, `/healthz` |
| `app/config.py` | `Settings` (pydantic-settings) из `.env` |
| `app/poller.py` | поллинг почты → `mail-polling` |
| `app/events.py` | журнал событий Пачки → `pachca-api` |
| `app/handlers.py` | кнопки и сообщения → `draft-review-flow` |
| `app/cards.py` | тексты и кнопки → `pachca-api` |
| `app/pachca.py` | клиент Пачки → `pachca-api` |
| `app/mail.py` | MCP-клиент почты → `mail-mcp` |
| `app/llm.py` | LLM → `llm-providers` |
| `app/db.py` | SQLAlchemy-модели и SQLite |

Миграций нет: `Base.metadata.create_all` при старте. Новая колонка в существующей
таблице сама не появится.

### Локальный запуск
Рекомендованный путь — Docker (`deploy-vps`) с `EVENTS_MODE=polling`: публичный
адрес не нужен, работает с ноутбука. Без Docker:
```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
DB_PATH=./data/bot.sqlite3 uvicorn app.main:app --port 8000
```
`./data/` создать заранее (в `.gitignore`).

### Стиль кода
- Python 3.12, `from __future__ import annotations`, аннотации везде,
  `dict[str, Any]`, `X | None`.
- Async: httpx, SDK LLM, MCP. Синхронная SQLAlchemy короткими сессиями
  `with SessionLocal() as s:` — не держать сессию через `await` сети.
- Модульные синглтоны: `settings`, `pachca`, `mail`, `get_llm()`.
- Докстринги и комментарии **на русском**, объясняют «почему» (реальный инцидент,
  ограничение API), а не «что».
- Широкий `except Exception` только с `# noqa: BLE001 - <причина>` и логом
  `exc_info=True`; джобы и фоновые задачи не должны падать насовсем.
- Логи на русском, `log = logging.getLogger(__name__)`.
- Конфиг — только через `Settings` + строка с комментарием в `.env.example`.
- Зависимости пинятся точно (`==`) в `requirements.txt`.

### Коммиты
- Заголовок на русском, суть изменения или результат, без префиксов
  (`feat:` и т. п.): «Кнопки карточки — в два ряда, иначе подписи обрезаются».
- Тело: почему так, что наблюдалось вживую, что проверено; списки через `-`.
- В конце `Co-Authored-By:` при участии Claude.
- Задачи — GitHub issues.

## Требования и расхождения
- Нет тестов, линтера, форматтера и `pyproject.toml`; проверка — запуск на живом
  ящике и в чате.
- Нет миграций схемы БД — изменение моделей ломает существующие базы
  (касается плана `draft-review-flow`: `awaiting_text`/`expired` не требуют
  колонок, а вот новые поля потребуют).
