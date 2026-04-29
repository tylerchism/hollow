"""Enhanced pattern analysis for journal entries.

Capabilities:
  --trend TYPE       Linear trend (slope) for a scored metric over time
  --correlate A B    Pearson correlation between two scored metrics
  --streak TYPE      Consecutive-day streak tracking for an entry type
  --anomalies TYPE   Flag entries whose score deviates >1.5 std devs from mean
  --report           Full report across all tracked metrics

All analytics use Python stdlib only — no extra packages required.

Usage examples:
  python3 analyze_patterns.py --trend sleep --days 14
  python3 analyze_patterns.py --correlate sleep energy --days 30
  python3 analyze_patterns.py --streak sleep --days 30
  python3 analyze_patterns.py --streak energy --min-score 4 --days 30
  python3 analyze_patterns.py --anomalies sleep --days 30
  python3 analyze_patterns.py --report --days 14

Output: JSON to stdout.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

try:
    import zoneinfo
    _TZ = zoneinfo.ZoneInfo("America/Chicago")
except Exception:
    import datetime as _dt_mod
    _TZ = _dt_mod.timezone.utc

HOLLOW_ROOT = Path(__file__).parent.parent.parent
DB_PATH = HOLLOW_ROOT / "data" / "journal" / "journal.db"

# Fall back to the legacy db.py path if the above doesn't exist
_ALT_DB = Path.home() / ".local" / "share" / "hollow-journal" / "journal.db"

SCORED_TYPES = ("sleep", "mood", "energy")
ALL_TYPES = ("sleep", "mood", "energy", "food", "workout", "fast", "note")


def _resolve_db() -> Path:
    if DB_PATH.exists():
        return DB_PATH
    if _ALT_DB.exists():
        return _ALT_DB
    return DB_PATH  # will error naturally if queried


def _today() -> date:
    return datetime.now(tz=_TZ).date()


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def _fetch_entries(
    start: str,
    end: str,
    entry_type: str | None = None,
) -> list[dict]:
    db = _resolve_db()
    if not db.exists():
        return []

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        if entry_type:
            rows = conn.execute(
                "SELECT * FROM entries WHERE date >= ? AND date <= ? AND entry_type = ?"
                " ORDER BY date, timestamp",
                (start, end, entry_type),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM entries WHERE date >= ? AND date <= ? ORDER BY date, timestamp",
                (start, end),
            ).fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        d = dict(row)
        # Normalise score — may be top-level column (db.py schema) or in structured_data
        if "structured_data" in d:
            try:
                sd = json.loads(d.get("structured_data") or "{}")
                if d.get("score") is None and "score" in sd:
                    d["score"] = sd["score"]
                if d.get("hours") is None and "hours" in sd:
                    d["hours"] = sd["hours"]
            except Exception:
                pass
        result.append(d)
    return result


def _date_range(days: int) -> tuple[str, str]:
    today = _today()
    start = today - timedelta(days=days - 1)
    return start.isoformat(), today.isoformat()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_scores(entries: list[dict]) -> list[tuple[str, float]]:
    """Return (date, score) pairs from entries that have a numeric score."""
    pairs = []
    for e in entries:
        score = e.get("score")
        try:
            score = float(score)
        except (TypeError, ValueError):
            continue
        pairs.append((e["date"], score))
    return pairs


def _extract_hours(entries: list[dict]) -> list[tuple[str, float]]:
    """Return (date, hours) pairs from sleep entries."""
    pairs = []
    for e in entries:
        hours = e.get("hours")
        try:
            hours = float(hours)
        except (TypeError, ValueError):
            continue
        pairs.append((e["date"], hours))
    return pairs


def _group_by_date_mean(pairs: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Average multiple same-day values into one (date, mean_score) pair."""
    by_date: dict[str, list[float]] = {}
    for dt, val in pairs:
        by_date.setdefault(dt, []).append(val)
    return [(dt, statistics.mean(vals)) for dt, vals in sorted(by_date.items())]


def _trend_direction(slope: float) -> str:
    if abs(slope) < 0.005:
        return "flat"
    return "improving" if slope > 0 else "declining"


# ---------------------------------------------------------------------------
# Analyses
# ---------------------------------------------------------------------------

def trend_analysis(entry_type: str, days: int) -> dict:
    """Linear trend for a scored metric over a date range."""
    start, end = _date_range(days)
    entries = _fetch_entries(start, end, entry_type)
    pairs = _extract_scores(entries)
    if entry_type == "sleep" and not pairs:
        pairs = _extract_hours(entries)

    daily = _group_by_date_mean(pairs)

    if len(daily) < 2:
        return {
            "type": entry_type,
            "days": days,
            "data_points": len(daily),
            "error": "insufficient data (need ≥2 days with scores)",
        }

    # x = day index (0-based), y = score
    dates = [d for d, _ in daily]
    x = list(range(len(daily)))
    y = [v for _, v in daily]

    reg = statistics.linear_regression(x, y)
    slope = reg.slope
    intercept = reg.intercept

    # R-squared
    y_mean = statistics.mean(y)
    ss_tot = sum((yi - y_mean) ** 2 for yi in y)
    y_pred = [slope * xi + intercept for xi in x]
    ss_res = sum((yi - yp) ** 2 for yi, yp in zip(y, y_pred))
    r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0

    return {
        "type": entry_type,
        "days": days,
        "start": start,
        "end": end,
        "data_points": len(daily),
        "slope_per_day": round(slope, 4),
        "r_squared": round(r_squared, 3),
        "direction": _trend_direction(slope),
        "mean": round(statistics.mean(y), 2),
        "current": round(y[-1], 2),
        "dates_with_data": dates,
    }


def correlation_analysis(type_a: str, type_b: str, days: int) -> dict:
    """Pearson correlation between two scored metrics.

    For each date that has both metrics, uses the day's mean score.
    Optionally shifts type_b by one day to test lead/lag (e.g., sleep → next-day energy).
    """
    start, end = _date_range(days)

    entries_a = _fetch_entries(start, end, type_a)
    entries_b = _fetch_entries(start, end, type_b)

    pairs_a = _group_by_date_mean(_extract_scores(entries_a))
    pairs_b = _group_by_date_mean(_extract_scores(entries_b))

    dict_a = dict(pairs_a)
    dict_b = dict(pairs_b)

    # Same-day overlap
    common_dates = sorted(set(dict_a) & set(dict_b))

    if len(common_dates) < 3:
        return {
            "type_a": type_a,
            "type_b": type_b,
            "days": days,
            "common_dates": len(common_dates),
            "error": "insufficient overlap (need ≥3 dates with both metrics)",
        }

    x = [dict_a[d] for d in common_dates]
    y = [dict_b[d] for d in common_dates]

    r = statistics.correlation(x, y)

    # Interpret
    abs_r = abs(r)
    if abs_r >= 0.7:
        strength = "strong"
    elif abs_r >= 0.4:
        strength = "moderate"
    elif abs_r >= 0.2:
        strength = "weak"
    else:
        strength = "negligible"

    direction_str = "positive" if r > 0 else "negative"
    interpretation = f"{strength} {direction_str} correlation"

    return {
        "type_a": type_a,
        "type_b": type_b,
        "days": days,
        "common_dates": len(common_dates),
        "pearson_r": round(r, 3),
        "interpretation": interpretation,
        "note": f"Higher {type_a} {'predicts higher' if r > 0 else 'predicts lower'} {type_b} on the same day",
    }


def streak_analysis(entry_type: str, days: int, min_score: float | None = None) -> dict:
    """Track streaks: consecutive days with an entry (optionally above min_score).

    Returns current streak, longest streak in the period, and all streak runs.
    """
    start, end = _date_range(days)
    entries = _fetch_entries(start, end, entry_type)

    # Build set of qualifying dates
    qualifying: set[str] = set()
    for e in entries:
        if min_score is not None:
            score = e.get("score")
            try:
                if float(score) >= min_score:
                    qualifying.add(e["date"])
            except (TypeError, ValueError):
                pass
        else:
            qualifying.add(e["date"])

    # Walk date range and find runs
    today = _today()
    start_d = today - timedelta(days=days - 1)
    all_dates = [(start_d + timedelta(days=i)).isoformat() for i in range(days)]

    runs: list[list[str]] = []
    current_run: list[str] = []
    for d in all_dates:
        if d in qualifying:
            current_run.append(d)
        else:
            if current_run:
                runs.append(current_run)
            current_run = []
    if current_run:
        runs.append(current_run)

    longest = max((len(r) for r in runs), default=0)

    # Current streak = last run if it ends today or yesterday
    current_streak = 0
    if runs:
        last_run = runs[-1]
        last_date = date.fromisoformat(last_run[-1])
        if last_date >= today - timedelta(days=1):
            current_streak = len(last_run)

    return {
        "type": entry_type,
        "days_window": days,
        "min_score_filter": min_score,
        "qualifying_days": len(qualifying),
        "current_streak": current_streak,
        "longest_streak_in_window": longest,
        "streak_runs": [{"start": r[0], "end": r[-1], "length": len(r)} for r in runs],
    }


def anomaly_analysis(entry_type: str, days: int, threshold_stdevs: float = 1.5) -> dict:
    """Flag entries whose score deviates more than threshold_stdevs from the period mean."""
    start, end = _date_range(days)
    entries = _fetch_entries(start, end, entry_type)
    pairs = _extract_scores(entries)

    if len(pairs) < 3:
        return {
            "type": entry_type,
            "days": days,
            "data_points": len(pairs),
            "error": "insufficient data (need ≥3 scored entries)",
        }

    scores = [s for _, s in pairs]
    mean = statistics.mean(scores)
    try:
        stdev = statistics.stdev(scores)
    except statistics.StatisticsError:
        stdev = 0.0

    if stdev == 0:
        return {
            "type": entry_type,
            "days": days,
            "data_points": len(pairs),
            "mean": round(mean, 2),
            "stdev": 0.0,
            "anomalies": [],
            "note": "No variance in scores — all entries identical",
        }

    anomalies = []
    for entry in entries:
        score = entry.get("score")
        try:
            score_f = float(score)
        except (TypeError, ValueError):
            continue
        deviation = (score_f - mean) / stdev
        if abs(deviation) >= threshold_stdevs:
            anomalies.append({
                "date": entry["date"],
                "timestamp": entry.get("timestamp"),
                "score": score_f,
                "deviation_stdevs": round(deviation, 2),
                "direction": "high" if deviation > 0 else "low",
                "notes": entry.get("notes"),
            })

    return {
        "type": entry_type,
        "days": days,
        "data_points": len(pairs),
        "mean": round(mean, 2),
        "stdev": round(stdev, 2),
        "threshold_stdevs": threshold_stdevs,
        "anomaly_count": len(anomalies),
        "anomalies": anomalies,
    }


def full_report(days: int) -> dict:
    """Run all analyses for all scored metrics and return a structured report."""
    report: dict = {
        "days_window": days,
        "generated": datetime.now(tz=_TZ).isoformat(),
        "trends": {},
        "correlations": {},
        "streaks": {},
        "anomalies": {},
    }

    for t in SCORED_TYPES:
        report["trends"][t] = trend_analysis(t, days)
        report["streaks"][t] = streak_analysis(t, days)
        report["anomalies"][t] = anomaly_analysis(t, days)

    # Key correlations
    for a, b in [("sleep", "energy"), ("sleep", "mood"), ("mood", "energy")]:
        key = f"{a}_x_{b}"
        report["correlations"][key] = correlation_analysis(a, b, days)

    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Journal pattern analysis")
    parser.add_argument("--days", type=int, default=14, help="Analysis window in days (default: 14)")

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--trend", metavar="TYPE", help="Trend analysis for a scored metric")
    group.add_argument("--correlate", nargs=2, metavar=("TYPE_A", "TYPE_B"),
                       help="Correlation between two scored metrics")
    group.add_argument("--streak", metavar="TYPE", help="Streak tracking for an entry type")
    group.add_argument("--anomalies", metavar="TYPE", help="Anomaly detection for a scored metric")
    group.add_argument("--report", action="store_true", help="Full report across all metrics")

    parser.add_argument("--min-score", type=float, default=None,
                        help="Minimum score threshold for --streak")
    parser.add_argument("--threshold", type=float, default=1.5,
                        help="Std-dev threshold for --anomalies (default: 1.5)")

    args = parser.parse_args()

    if args.trend:
        result = trend_analysis(args.trend, args.days)
    elif args.correlate:
        result = correlation_analysis(args.correlate[0], args.correlate[1], args.days)
    elif args.streak:
        result = streak_analysis(args.streak, args.days, args.min_score)
    elif args.anomalies:
        result = anomaly_analysis(args.anomalies, args.days, args.threshold)
    elif args.report:
        result = full_report(args.days)
    else:
        result = {"error": "No analysis mode specified"}

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
