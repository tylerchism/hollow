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

---

## Pattern Analysis

Enhanced analytics beyond simple averaging. Uses `analyze_patterns.py` — stdlib only, no extra packages.

### Trend Detection

Is a metric improving or declining over time?

```bash
python3 /home/tchism/git/hollow/agents/journal/analyze_patterns.py \
  --trend sleep --days 14
```

Returns slope (positive = improving), R-squared, and a plain-language direction ("improving", "declining", "flat").

```bash
python3 /home/tchism/git/hollow/agents/journal/analyze_patterns.py \
  --trend energy --days 30
```

### Correlation Analysis

Does one metric predict another?

```bash
python3 /home/tchism/git/hollow/agents/journal/analyze_patterns.py \
  --correlate sleep energy --days 30
```

Computes Pearson correlation between sleep quality scores and next-day energy scores. Returns coefficient and interpretation.

```bash
python3 /home/tchism/git/hollow/agents/journal/analyze_patterns.py \
  --correlate sleep mood --days 21
```

### Streak Tracking

Consecutive days meeting a threshold.

```bash
# Consecutive days with any sleep entry logged
python3 /home/tchism/git/hollow/agents/journal/analyze_patterns.py \
  --streak sleep --days 30

# Consecutive days with energy score >= 4
python3 /home/tchism/git/hollow/agents/journal/analyze_patterns.py \
  --streak energy --min-score 4 --days 30
```

### Anomaly Detection

Entries that deviate from the period baseline.

```bash
python3 /home/tchism/git/hollow/agents/journal/analyze_patterns.py \
  --anomalies sleep --days 30
```

Returns entries whose score is >1.5 std devs from the period mean, with the deviation and date.

### Full Report

```bash
python3 /home/tchism/git/hollow/agents/journal/analyze_patterns.py \
  --report --days 14
```

Runs all analyses (trends, correlations, streaks, anomalies) for all tracked metrics and returns a structured JSON report.

---

## CM Corpus Retrieval

Cross-reference Tyler's health patterns with Chris Masterjohn's nutrient science.

```bash
bin/retrieve --person chris-masterjohn --query "sleep quality and magnesium" --top-k 3
```

```bash
bin/retrieve --person chris-masterjohn --query "methylation nutrient cofactors" --top-k 5
```

```bash
bin/retrieve --person chris-masterjohn --query "fat soluble vitamins absorption" --top-k 3
```

Returns JSON with ranked chunks from CM's corpus (4,408 chunks). Never synthesizes — returns exact ingested text.

### Workflow for CM-grounded Pattern Analysis

1. Identify a pattern in the journal data (e.g., "energy consistently low the day after high-fat days")
2. Form a query that targets the mechanism: `bin/retrieve --person chris-masterjohn --query "fat digestion bile acids energy"`
3. Pull top 2-3 chunks
4. Synthesize: state the pattern from Tyler's data, then layer in CM's framework
5. Flag if CM's corpus doesn't have relevant coverage — don't hallucinate a connection
