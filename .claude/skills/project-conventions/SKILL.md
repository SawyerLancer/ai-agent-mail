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
| `app/db.py` | SQLAlchemy-модели, SQLite, мини-миграция колонок |
| `app/factcheck.py` | что модель добавила от себя → `draft-review-flow` |
| `app/proofdiff.py` | «было → стало» для вычитки → `draft-review-flow` |
| `app/style.py` | память стиля → `style-memory` |

`init_db()` при старте: `create_all` для новых таблиц + `_add_missing_columns()`
для новых колонок в существующих.

### Локальный запуск
Рекомендованный путь — Docker (`deploy-vps`) с `EVENTS_MODE=polling`: публичный
адрес не нужен, работает с ноутбука. Без Docker:
```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
DB_PATH=./data/bot.sqlite3 uvicorn app.main:app --port 8000
```
`./data/` создать заранее (в `.gitignore`).

### Тесты
`tests/` (pytest, `requirements-dev.txt`). CI — `.github/workflows/tests.yml`:
pytest на push и PR, Python 3.13 (бот в Docker — 3.12). Чистые модули (`factcheck`,
`proofdiff`) — без заглушек; БД — настоящая SQLite во временной папке
(`tests/conftest.py`); Пачка, почта и модель — подставные объекты
(`tests/test_draft_flow.py`). Сеть в тестах не трогаем. Запуск в образе бота
(Python 3.12, закреплённые версии):
```bash
docker compose run --rm --no-deps -v "$PWD/tests:/srv/tests:ro" bot sh -c "pip install --user -q pytest==9.1.1 && python -m pytest -q -p no:cacheprovider tests"
```

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
- Нет линтера, форматтера и `pyproject.toml`.
- Миграции — только `db._add_missing_columns()`: добавить колонку со скалярным
  default. Переименование или смена типа — уже нужен Alembic.
