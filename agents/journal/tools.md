# Journal Tools

## Database

The journal database lives at `/home/tchism/git/hollow/data/journal/journal.db`.

Initialize it if it doesn't exist:

```bash
python3 /home/tchism/git/hollow/agents/journal/init_db.py
```

## Logging an Entry

```bash
python3 /home/tchism/git/hollow/agents/journal/log_entry.py \
  --type food \
  --raw "had eggs and coffee for breakfast" \
  --data '{"meal_type": "breakfast", "items": ["eggs", "coffee"]}' \
  --tags '["breakfast"]' \
  --timestamp "2026-03-31T08:30:00-05:00"
```

## Querying Entries

By date range:
```bash
python3 /home/tchism/git/hollow/agents/journal/query_entries.py --start 2026-03-25 --end 2026-03-31
```

By type and date range:
```bash
python3 /home/tchism/git/hollow/agents/journal/query_entries.py --type sleep --start 2026-03-25 --end 2026-03-31
```

Today:
```bash
python3 /home/tchism/git/hollow/agents/journal/query_entries.py --today
```

Yesterday:
```bash
python3 /home/tchism/git/hollow/agents/journal/query_entries.py --yesterday
```

Last N days:
```bash
python3 /home/tchism/git/hollow/agents/journal/query_entries.py --days 7
```

## Workflow for Logging

1. Parse the user's message to identify entry type(s), timestamps, and structured data
2. For relative time references ("yesterday", "this morning"), compute the actual timestamp
3. Call `log_entry.py` once per distinct entry in the message
4. Confirm back to the user in one brief line per entry

## Workflow for Queries

1. Determine the time range from the user's question
2. Call `query_entries.py` with appropriate filters
3. Analyze the returned JSON and respond with real numbers and patterns
4. Never invent data — if entries are sparse, say so

## Weekly Summary

```bash
python3 /home/tchism/git/hollow/agents/journal/query_entries.py --days 7 --summary
```

Returns all entry types for the past 7 days. Compute averages for mood/energy/sleep scores, list food entries by day, note any fasting windows.
