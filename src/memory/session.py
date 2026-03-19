"""Session logging — daily markdown logs of conversations."""

import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


class SessionLogger:
    """Append-only daily session logs in markdown format."""

    def __init__(self, sessions_dir: Path, timezone_name: str = "America/Chicago"):
        self.sessions_dir = sessions_dir
        self.tz = ZoneInfo(timezone_name)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def _now(self) -> datetime:
        return datetime.now(self.tz)

    def _today_path(self) -> Path:
        return self.sessions_dir / f"{self._now().strftime('%Y-%m-%d')}.md"

    def log_message(self, role: str, content: str) -> None:
        """Append a message to today's session log."""
        path = self._today_path()
        now = self._now()
        is_new = not path.exists()

        with open(path, "a", encoding="utf-8") as f:
            if is_new:
                f.write(f"# Session Log - {now.strftime('%Y-%m-%d')}\n")

            if role == "user":
                f.write(f"\n## {now.strftime('%H:%M')}\n")

            display_role = role.capitalize()
            f.write(f"\n**{display_role}:** {content}\n")

    def get_today_log(self) -> str | None:
        """Read today's session log, if it exists."""
        path = self._today_path()
        if path.exists():
            return path.read_text(encoding="utf-8")
        return None

    def get_recent_logs(self, days: int = 2) -> list[tuple[str, str]]:
        """Get recent session logs as (date_string, content) tuples."""
        results = []
        today = self._now().date()

        for i in range(days):
            date = today - timedelta(days=i)
            date_str = date.strftime("%Y-%m-%d")
            path = self.sessions_dir / f"{date_str}.md"
            if path.exists():
                content = path.read_text(encoding="utf-8")
                results.append((date_str, content))

        return results
