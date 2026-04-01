"""Query journal entries from data/journal/journal.db.

Usage:
    # Today's entries
    python3 query_entries.py --today

    # Yesterday's entries
    python3 query_entries.py --yesterday

    # Last N days
    python3 query_entries.py --days 7

    # Date range
    python3 query_entries.py --start 2026-03-25 --end 2026-03-31

    # Filter by type
    python3 query_entries.py --days 7 --type sleep

    # Summary mode (adds computed stats)
    python3 query_entries.py --days 7 --summary

Outputs JSON.
"""

import argparse
import json
import sqlite3
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import zoneinfo
    _TZ = zoneinfo.ZoneInfo("America/Chicago")
except Exception:
    import datetime as _dt_mod
    _TZ = _dt_mod.timezone.utc

HOLLOW_ROOT = Path(__file__).parent.parent.parent
DB_PATH = HOLLOW_ROOT / "data" / "journal" / "journal.db"


def _today_local() -> date:
    return datetime.now(tz=_TZ).date()


def get_entries(
    start_date: str,
    end_date: str,
    entry_type: str | None = None,
) -> list[dict]:
    """Return entries between start_date and end_date (inclusive, YYYY-MM-DD)."""
    if not DB_PATH.exists():
        return []

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        if entry_type:
            cursor = conn.execute(
                "SELECT * FROM entries WHERE date >= ? AND date <= ? AND entry_type = ? ORDER BY timestamp",
                (start_date, end_date, entry_type),
            )
        else:
            cursor = conn.execute(
                "SELECT * FROM entries WHERE date >= ? AND date <= ? ORDER BY timestamp",
                (start_date, end_date),
            )
        rows = cursor.fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        d = dict(row)
        # Parse JSON fields for readability
        try:
            d["structured_data"] = json.loads(d["structured_data"] or "{}")
        except Exception:
            pass
        try:
            d["tags"] = json.loads(d["tags"] or "[]")
        except Exception:
            pass
        result.append(d)

    return result


def compute_summary(entries: list[dict]) -> dict:
    """Compute aggregate statistics over a list of entries."""
    by_type: dict[str, list] = {}
    for e in entries:
        by_type.setdefault(e["entry_type"], []).append(e)

    summary: dict = {"total_entries": len(entries), "by_type": {}}

    for etype, items in by_type.items():
        info: dict = {"count": len(items)}

        # Extract scores if present
        scores = []
        for item in items:
            sd = item.get("structured_data") or {}
            if isinstance(sd, dict) and "score" in sd and sd["score"] is not None:
                try:
                    scores.append(float(sd["score"]))
                except (TypeError, ValueError):
                    pass

        if scores:
            info["score_avg"] = round(statistics.mean(scores), 2)
            info["score_min"] = min(scores)
            info["score_max"] = max(scores)

        # Extract hours for sleep
        if etype == "sleep":
            hours_list = []
            for item in items:
                sd = item.get("structured_data") or {}
                if isinstance(sd, dict) and "hours" in sd and sd["hours"] is not None:
                    try:
                        hours_list.append(float(sd["hours"]))
                    except (TypeError, ValueError):
                        pass
            if hours_list:
                info["hours_avg"] = round(statistics.mean(hours_list), 1)
                info["hours_total"] = round(sum(hours_list), 1)

        summary["by_type"][etype] = info

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Query journal entries")
    parser.add_argument("--today", action="store_true", help="Today's entries")
    parser.add_argument("--yesterday", action="store_true", help="Yesterday's entries")
    parser.add_argument("--days", type=int, default=None, help="Last N days")
    parser.add_argument("--start", default=None, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD")
    parser.add_argument("--type", default=None, dest="entry_type", help="Filter by entry type")
    parser.add_argument("--summary", action="store_true", help="Include aggregate stats")
    args = parser.parse_args()

    today = _today_local()

    if args.today:
        start = end = today.isoformat()
    elif args.yesterday:
        yesterday = today - timedelta(days=1)
        start = end = yesterday.isoformat()
    elif args.days is not None:
        start = (today - timedelta(days=args.days - 1)).isoformat()
        end = today.isoformat()
    elif args.start and args.end:
        start = args.start
        end = args.end
    elif args.start:
        start = args.start
        end = today.isoformat()
    else:
        # Default: today
        start = end = today.isoformat()

    entries = get_entries(start, end, entry_type=args.entry_type)

    output: dict = {
        "query": {"start": start, "end": end, "entry_type": args.entry_type},
        "count": len(entries),
        "entries": entries,
    }

    if args.summary and entries:
        output["summary"] = compute_summary(entries)

    print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    main()
