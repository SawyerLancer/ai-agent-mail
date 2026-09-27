"""Конфигурация из переменных окружения."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Пачка ---
    pachca_token: str
    pachca_channel_id: int              # чат/канал, куда падают письма
    pachca_signing_secret: str = ""      # нужен только в режиме webhook
    bot_user_id: int = 0                  # чтобы не реагировать на свои сообщения
    pachca_base_url: str = "https://api.pachca.com/api/shared/v1"
    pachca_allowed_ip: str = "37.200.70.177"
    check_source_ip: bool = False       # включать, только если нет прокси/CDN перед ботом
    # id сотрудников, которым разрешено управлять почтой (пусто = всем в чате)
    allowed_user_ids: list[int] = []
    # Как получать события: webhook (нужен публичный HTTPS) или polling
    # (журнал событий бота, работает без публичного адреса).
    events_mode: str = "polling"          # polling | webhook
    events_poll_interval: int = 3         # секунд; лимит чтения журнала ~5 req/2s

    # --- LLM ---
    llm_provider: str = "anthropic"          # anthropic | deepseek
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5"
    anthropic_base_url: str = ""             # для прокси; пусто = дефолт SDK
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"
    deepseek_base_url: str = "https://api.deepseek.com"
    llm_max_tokens: int = 1500
    signature: str = ""                      # подпись в конце письма

    # --- Почта (через MCP) ---
    mcp_command: str = "mcp-email-server"
    mcp_args: list[str] = ["stdio"]
    email_account: str                       # account_name в mcp-email-server
    mailbox: str = "INBOX"
    poll_interval: int = 60                  # секунд
    poll_page_size: int = 50
    preview_chars: int = 700                 # сколько текста письма показывать в чате
    body_chars_for_llm: int = 6000           # сколько отдаём модели

    # --- Прочее ---
    db_path: str = "/data/bot.sqlite3"
    log_level: str = "INFO"


settings = Settings()  # type: ignore[call-arg]
