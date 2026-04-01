"""Log a single journal entry to data/journal/journal.db.

Usage:
    python3 log_entry.py --type food --raw "had eggs for breakfast" \
        --data '{"meal_type": "breakfast", "items": ["eggs"]}' \
        --tags '["breakfast"]' \
        --timestamp "2026-03-31T08:30:00-05:00"

All arguments except --type and --raw are optional.
If --timestamp is omitted, the current local time is used.
"""

import argparse
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    import zoneinfo
    _TZ = zoneinfo.ZoneInfo("America/Chicago")
except Exception:
    _TZ = timezone.utc

HOLLOW_ROOT = Path(__file__).parent.parent.parent
DB_PATH = HOLLOW_ROOT / "data" / "journal" / "journal.db"


def _now_local() -> datetime:
    return datetime.now(tz=_TZ)


def _parse_timestamp(ts_str: str) -> datetime:
    """Parse an ISO 8601 timestamp string."""
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
    ):
        try:
            dt = datetime.strptime(ts_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_TZ)
            return dt
        except ValueError:
            continue
    raise ValueError(f"Cannot parse timestamp: {ts_str!r}")


def log_entry(
    entry_type: str,
    raw_text: str,
    structured_data: dict | None = None,
    tags: list | None = None,
    timestamp: datetime | None = None,
) -> str:
    """Insert an entry into journal.db. Returns the new entry ID."""
    if timestamp is None:
        timestamp = _now_local()

    ts_str = timestamp.isoformat()
    date_str = timestamp.date().isoformat()
    entry_id = str(uuid.uuid4())

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            """
            INSERT INTO entries (id, timestamp, date, entry_type, raw_text, structured_data, tags)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry_id,
                ts_str,
                date_str,
                entry_type,
                raw_text,
                json.dumps(structured_data or {}),
                json.dumps(tags or []),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return entry_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Log a journal entry")
    parser.add_argument("--type", required=True, dest="entry_type",
                        help="Entry type: food, sleep, mood, energy, workout, fast, note")
    parser.add_argument("--raw", required=True, help="Raw text of the entry")
    parser.add_argument("--data", default="{}", help="JSON structured data")
    parser.add_argument("--tags", default="[]", help="JSON array of tags")
    parser.add_argument("--timestamp", default=None, help="ISO 8601 timestamp (default: now)")
    args = parser.parse_args()

    try:
        structured_data = json.loads(args.data)
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON in --data: {e}")
        raise SystemExit(1)

    try:
        tags = json.loads(args.tags)
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON in --tags: {e}")
        raise SystemExit(1)

    ts = None
    if args.timestamp:
        try:
            ts = _parse_timestamp(args.timestamp)
        except ValueError as e:
            print(f"Error: {e}")
            raise SystemExit(1)

    entry_id = log_entry(
        entry_type=args.entry_type,
        raw_text=args.raw,
        structured_data=structured_data,
        tags=tags,
        timestamp=ts,
    )
    print(json.dumps({"id": entry_id, "status": "logged"}))


if __name__ == "__main__":
    main()
