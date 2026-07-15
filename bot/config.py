import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Telegram
    api_id: int = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash: str = os.getenv("TELEGRAM_API_HASH", "")
    bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")

    # OpenAI-compatible API
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "http://0.0.0.0/v1")
    openai_model: str = os.getenv("OPENAI_MODEL", "")
    openai_max_tokens: int = int(os.getenv("OPENAI_MAX_TOKENS", "4096"))
    openai_system_prompt: str = os.getenv(
        "OPENAI_SYSTEM_PROMPT", "You are a helpful assistant."
    )

    # MongoDB
    mongodb_uri: str = os.getenv("MONGODB_URI", "mongodb://mongo:27017")
    mongodb_db: str = os.getenv("MONGODB_DB", "ggqtbot")

    # Bot settings
    admin_user_ids: list[int] = field(default_factory=list)
    group_trigger_keyword: str = os.getenv("GROUP_TRIGGER_KEYWORD", "/chat")
    max_conversation_age_minutes: int = int(
        os.getenv("MAX_CONVERSATION_AGE_MINUTES", "180")
    )
    max_history_size: int = int(os.getenv("MAX_HISTORY_SIZE", "30"))
    stream_update_interval: float = float(
        os.getenv("STREAM_UPDATE_INTERVAL", "1.5")
    )
    sticker_pack: str = os.getenv("STICKER_PACK", "Sexycatstickers")

    def __post_init__(self):
        raw = os.getenv("ADMIN_USER_IDS", "")
        if raw:
            self.admin_user_ids = [
                int(uid.strip()) for uid in raw.split(",") if uid.strip()
            ]
        else:
            self.admin_user_ids = []
