"""Configuration loading for Hollow.

Precedence: env vars > .env file > defaults.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Config:
    # API Keys
    telegram_bot_token: str = ""
    discord_bot_token: str = ""
    xai_api_key: str = ""

    # Paths
    project_root: Path = field(default_factory=lambda: Path(__file__).parent.parent)
    identity_dir: Path | None = None
    memory_dir: Path = field(default_factory=Path)
    data_dir: Path = field(default_factory=Path)

    # Agent settings
    primary_model: str = "claude-sonnet-4-6"
    telegram_allowed_users: list[int] = field(default_factory=list)
    heartbeat_chat_id: int = 0
    user_timezone: str = "America/Chicago"

    # Memory / embedding
    ollama_embed_model: str = "nomic-embed-text"

    # HTTP API
    api_port: int = 18800
    api_host: str = "127.0.0.1"

    # History
    history_char_budget: int = 500_000


def load_config() -> Config:
    """Load configuration from env vars and .env file."""
    project_root = Path(__file__).parent.parent
    load_dotenv(project_root / ".env")

    config = Config(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        discord_bot_token=os.getenv("DISCORD_BOT_TOKEN", ""),
        xai_api_key=os.getenv("XAI_API_KEY", ""),
        project_root=project_root,
        memory_dir=Path(os.getenv("MEMORY_DIR", str(project_root / "agent-memory" / "tarn"))),
        data_dir=Path(os.getenv("DATA_DIR", str(project_root / "data"))),
        primary_model=os.getenv("PRIMARY_MODEL", "claude-sonnet-4-6"),
        telegram_allowed_users=[
            int(uid.strip())
            for uid in os.getenv("TELEGRAM_ALLOWED_USERS", "").split(",")
            if uid.strip()
        ],
        heartbeat_chat_id=int(os.getenv("HEARTBEAT_CHAT_ID", "0")),
        user_timezone=os.getenv("USER_TIMEZONE", "America/Chicago"),
        ollama_embed_model=os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text"),
        api_port=int(os.getenv("API_PORT", "18800")),
        api_host=os.getenv("API_HOST", "127.0.0.1"),
        history_char_budget=int(os.getenv("HISTORY_CHAR_BUDGET", "500000")),
    )
    return config
