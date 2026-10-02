---
name: llm-providers
description: Слой LLM поверх Anthropic и DeepSeek с выбором провайдера через конфиг. Применяй, когда меняешь промпты, модель, провайдера, таймауты или добавляешь новый вызов модели.
---

# LLM-провайдеры

Код: `app/llm.py`. Почему у модели нет инструментов и как в промпт попадает
письмо — skill `email-actions-safety`.

## Как сейчас

### Интерфейс
```python
class LLM(ABC):
    async def complete(self, system: str, prompt: str) -> str: ...
```
Остальной код зовёт только функции верхнего уровня и о провайдере не знает:

| Функция | Где | Что |
|---|---|---|
| `draft_reply(sender, subject, body, instruction=None, style_rules=())` | «Ответить», «Заново» | черновик + подпись |
| `revise(current, instruction, sender, subject, style_rules=())` | сообщение в треде | новый текст черновика + подпись |
| `summarize(sender, subject, body)` | карточка письма | 1–2 предложения; при ошибке `""` |
| `proofread(text)` | «Свой текст» | только ошибки; свой `PROOFREAD_SYSTEM`; без стиля и подписи; пусто → исключение |
| `extract_style(user_texts, edits, existing)` | после отправки | сырой JSON операций для `style.parse_ops` (`style-memory`) |

Блок стиля собирает `_style_block`.

### Провайдеры
| `LLM_PROVIDER` | Класс | SDK | Модель по умолчанию |
|---|---|---|---|
| `anthropic` | `AnthropicLLM` | `anthropic` 1.8.0, `messages.create` | `claude-haiku-4-5` |
| `deepseek` | `DeepSeekLLM` | `openai` 3.19.2, `chat.completions.create`, `base_url=https://api.deepseek.com` | `deepseek-chat` |

- Выбор один раз: `get_llm()` лениво создаёт синглтон; неизвестное имя → `ValueError`.
- Различия в вызове: у Anthropic `system=` отдельным параметром, ответ — блоки
  `content` с `type == "text"`; у DeepSeek system — первое сообщение,
  ответ — `choices[0].message.content`.
- `ANTHROPIC_BASE_URL` — прокси, если прямой доступ закрыт (или `HTTPS_PROXY`).
- Общие лимиты: `LLM_MAX_TOKENS=1500`, `LLM_TIMEOUT=120` с (таймаут SDK-клиента).
- Контекст ограничен обрезкой входа, а не подсчётом токенов: `_trim(body,
  BODY_CHARS_FOR_LLM=6000)` для черновика, 3000 символов для выжимки.
- `_with_signature` добавляет `SIGNATURE`, если её ещё нет в тексте.

### Tool calling
Не используется ни у одного провайдера — намеренно. Модель возвращает только текст.
Где нужна структура (`extract_style`), просим JSON-массив текстом и разбираем
устойчиво: вырезаем `[...]`, мусор → пустой результат.

### Ошибки
Фоллбека нет. Исключение провайдера ловит вызывающий код в `handlers.py`
и пишет в тред «Модель недоступна…»; `summarize` глотает ошибку сам.

## Правила
- Новый провайдер — класс с `complete()` + запись в `_PROVIDERS` + поля в
  `config.py` и `.env.example`. Импорт SDK — внутри `__init__`, чтобы
  неиспользуемый SDK не был обязателен при старте.
- Не добавлять провайдер-специфичные параметры в функции верхнего уровня.

## Требования и расхождения
- Автоматического фоллбека Anthropic → DeepSeek нет; в README DeepSeek упомянут
  как ручной резерв (сменить `LLM_PROVIDER` и перезапустить). Нужен ли
  автоматический — не решено.
- Лимиты контекста моделей не учитываются: защита только обрезкой по символам.
