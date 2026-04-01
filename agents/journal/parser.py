"""Message parsing logic for /log commands."""

import json
import re
import uuid
from datetime import datetime, timezone

import zoneinfo

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MEAL_TYPES = {"breakfast", "lunch", "dinner", "snack"}
ENTRY_TYPES = {"food", "mood", "energy", "sleep", "social", "event", "note"}

# Keyword heuristics for free-text fallback
_TYPE_KEYWORDS: dict[str, list[str]] = {
    "food": ["ate", "eat", "eating", "food", "meal", "breakfast", "lunch", "dinner",
             "snack", "drink", "drank", "coffee", "tea", "water"],
    "mood": ["mood", "feeling", "felt", "happy", "sad", "anxious", "stressed",
             "calm", "excited", "depressed", "great", "bad", "okay"],
    "energy": ["energy", "tired", "fatigue", "fatigued", "exhausted", "wired",
               "lethargic", "alert", "sluggish", "energized"],
    "sleep": ["sleep", "slept", "nap", "napped", "insomnia", "woke", "bedtime"],
    "social": ["met", "meet", "talked", "called", "hung out", "visited", "friend",
               "family", "date", "party", "gathering"],
    "event": ["went", "attended", "appointment", "meeting", "workout", "exercise",
              "ran", "gym", "class", "event"],
}


def _local_tz() -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo("America/Chicago")
    except Exception:
        return zoneinfo.ZoneInfo("UTC")


def _now_local() -> datetime:
    return datetime.now(tz=_local_tz())


def _make_id() -> str:
    return str(uuid.uuid4())


def _guess_type(text: str) -> str:
    text_lower = text.lower()
    scores: dict[str, int] = {t: 0 for t in _TYPE_KEYWORDS}
    for entry_type, keywords in _TYPE_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                scores[entry_type] += 1
    best = max(scores, key=lambda t: scores[t])
    if scores[best] == 0:
        return "note"
    return best


# ---------------------------------------------------------------------------
# Public parse function
# ---------------------------------------------------------------------------

def parse_message(message: str) -> tuple[dict, str]:
    """
    Parse a /log message into an entry dict and a confirmation string.

    Returns (entry_dict, confirmation_text).
    Raises ValueError on unrecognisable input.
    """
    # Strip leading slash-log prefix variations
    text = message.strip()
    if text.lower().startswith("/log"):
        text = text[4:].strip()

    now = _now_local()
    date_str = now.date().isoformat()
    ts_str = now.isoformat()
    entry_id = _make_id()

    # Shared base
    base = {
        "id": entry_id,
        "date": date_str,
        "timestamp": ts_str,
        "score": None,
        "hours": None,
        "meal_type": None,
        "tags": "[]",
        "notes": None,
        "entities": "{}",
        "sentiment": None,
    }

    tokens = text.split()
    if not tokens:
        # Empty /log → note
        base["entry_type"] = "note"
        base["notes"] = ""
        return base, "Logged. Note saved."

    cmd = tokens[0].lower()

    # ------------------------------------------------------------------
    # /log food [meal_type] [items...]
    # ------------------------------------------------------------------
    if cmd == "food":
        rest = tokens[1:]
        meal_type = None
        if rest and rest[0].lower() in MEAL_TYPES:
            meal_type = rest[0].lower()
            rest = rest[1:]
        items = rest
        items_str = ", ".join(items) if items else ""
        base["entry_type"] = "food"
        base["meal_type"] = meal_type
        base["notes"] = items_str if items_str else None
        base["tags"] = json.dumps(items)
        entities: dict = {}
        if items:
            entities["items"] = items
        base["entities"] = json.dumps(entities)

        time_str = now.strftime("%-I:%M%p").lower()
        meal_label = meal_type.capitalize() if meal_type else "Meal"
        food_emoji = {"breakfast": "🍳", "lunch": "🥗", "dinner": "🍽️", "snack": "🍎"}.get(meal_type or "", "🍴")
        items_display = ", ".join(items) if items else "(no items)"
        confirmation = f"Logged. {food_emoji} {meal_label}: {items_display} @ {time_str}"
        return base, confirmation

    # ------------------------------------------------------------------
    # /log mood [1-5] [optional notes]
    # ------------------------------------------------------------------
    if cmd == "mood":
        rest = tokens[1:]
        score = None
        notes = None
        if rest and rest[0].isdigit() and 1 <= int(rest[0]) <= 5:
            score = int(rest[0])
            notes = " ".join(rest[1:]) or None
        else:
            notes = " ".join(rest) or None
        base["entry_type"] = "mood"
        base["score"] = score
        base["notes"] = notes
        score_display = f" {score}/5" if score is not None else ""
        notes_display = f" — {notes}" if notes else ""
        confirmation = f"Logged. 😌 Mood{score_display}{notes_display}"
        return base, confirmation

    # ------------------------------------------------------------------
    # /log energy [1-5] [optional notes]
    # ------------------------------------------------------------------
    if cmd == "energy":
        rest = tokens[1:]
        score = None
        notes = None
        if rest and rest[0].isdigit() and 1 <= int(rest[0]) <= 5:
            score = int(rest[0])
            notes = " ".join(rest[1:]) or None
        else:
            notes = " ".join(rest) or None
        base["entry_type"] = "energy"
        base["score"] = score
        base["notes"] = notes
        score_display = f" {score}/5" if score is not None else ""
        notes_display = f" — {notes}" if notes else ""
        confirmation = f"Logged. ⚡ Energy{score_display}{notes_display}"
        return base, confirmation

    # ------------------------------------------------------------------
    # /log sleep [hours] [quality 1-5]
    # ------------------------------------------------------------------
    if cmd == "sleep":
        rest = tokens[1:]
        hours = None
        score = None
        notes_parts = []
        for tok in rest:
            if hours is None:
                try:
                    hours = float(tok)
                    continue
                except ValueError:
                    pass
            if score is None and tok.isdigit() and 1 <= int(tok) <= 5:
                score = int(tok)
                continue
            notes_parts.append(tok)
        notes = " ".join(notes_parts) or None
        base["entry_type"] = "sleep"
        base["hours"] = hours
        base["score"] = score
        base["notes"] = notes
        hours_display = f" {hours}h" if hours is not None else ""
        quality_display = f" quality {score}/5" if score is not None else ""
        confirmation = f"Logged. 😴 Sleep{hours_display}{quality_display}"
        return base, confirmation

    # ------------------------------------------------------------------
    # Free-text fallback — heuristic type detection
    # ------------------------------------------------------------------
    full_text = text  # original remainder after /log
    guessed_type = _guess_type(full_text)
    # If cmd matches a known entry type exactly, honour it
    if cmd in ENTRY_TYPES:
        guessed_type = cmd
        full_text = " ".join(tokens[1:])

    base["entry_type"] = guessed_type
    base["notes"] = full_text if full_text else None
    confirmation = f"Logged. 📝 {guessed_type.capitalize()}: {full_text}" if full_text else f"Logged. 📝 {guessed_type.capitalize()}"
    return base, confirmation
