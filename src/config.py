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
    discord_bot_token: str = ""
    xai_api_key: str = ""

    # Paths
    project_root: Path = field(default_factory=lambda: Path(__file__).parent.parent)
    identity_dir: Path | None = None
    memory_dir: Path = field(default_factory=Path)
    data_dir: Path = field(default_factory=Path)

    # Agent settings
    primary_model: str = "claude-sonnet-4-6"
    user_timezone: str = "America/Chicago"

    # Memory / embedding
    ollama_embed_model: str = "nomic-embed-text"

    # HTTP API
    api_port: int = 18800
    api_host: str = "127.0.0.1"

    # History
    history_char_budget: int = 500_000

    # Discord channel name this agent listens to (default: "tarn")
    discord_channel_name: str = "tarn"

    # Whether to send a startup notification on boot (default: True).
    # Set STARTUP_NOTIFICATION=false to silence startup messages for agents
    # that share a conversation channel (e.g. Flux on #trader-bot).
    startup_notification: bool = True


def load_config(env_path: Path | None = None) -> Config:
    """Load configuration from env vars and .env file.

    Args:
        env_path: Optional path to an agent-specific .env file (e.g.
            agents/tarn/.env).  Loaded *after* the root .env so agent
            values override defaults.  Pass this when --identity-dir is
            known at startup so the Config object is fully populated
            before it is constructed — no post-hoc field patching needed.
    """
    project_root = Path(__file__).parent.parent
    load_dotenv(project_root / ".env")
    if env_path is not None and Path(env_path).exists():
        load_dotenv(env_path, override=True)

    config = Config(
        discord_bot_token=os.getenv("DISCORD_BOT_TOKEN", ""),
        xai_api_key=os.getenv("XAI_API_KEY", ""),
        project_root=project_root,
        memory_dir=Path(os.getenv("MEMORY_DIR", str(project_root / "agent-memory" / "tarn"))),
        data_dir=Path(os.getenv("DATA_DIR", str(project_root / "data"))),
        primary_model=os.getenv("PRIMARY_MODEL", "claude-sonnet-4-6"),
        user_timezone=os.getenv("USER_TIMEZONE", "America/Chicago"),
        ollama_embed_model=os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text"),
        api_port=int(os.getenv("API_PORT", "18800")),
        api_host=os.getenv("API_HOST", "127.0.0.1"),
        history_char_budget=int(os.getenv("HISTORY_CHAR_BUDGET", "500000")),
        discord_channel_name=os.getenv("DISCORD_CHANNEL_NAME", "tarn"),
        startup_notification=os.getenv("STARTUP_NOTIFICATION", "true").strip().lower() not in ("false", "0", "no"),
    )
    return config
