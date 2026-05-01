# Proposed Changes

## [APPLIED] Bench — Add to agent roster and document Tarn relationship
- **Date:** 2026-05-01
- **MC Task:** u1xX35y9YQd6wIGZjZE9A
- **Target file:** `agent-memory/tarn/agents.md`
- **Reason:** `agents/bench/` exists on disk (port 18802, `hail bench`) but is undocumented in agents.md. Structural gap detected by agent_upgrades cron.

### Change 1 — Roster table (between Sap and AGENT_ROSTER_END)
Add:
```
| Bench | `hail bench` / `bin/delegate-to-bench` | Long-task testing executor and delegation validator |
```

### Change 2 — New section after Sap–Journal section
Add `## Bench — Relationship to Tarn` covering:
- Port 18802, Discord `#bench`, `data/bench/hollow.db`
- How Tarn reaches it: `bin/post-to-bench "task"` (webhook posts appear as human input)
- Execution patterns: simple (direct), complex 3+ tool calls (background agent)
- Routing rule: fire-and-done via `bin/delegate-to-bench` — Tarn delegates and moves on; Bench posts results to #bench
- Purpose: stress-tests Tarn's long-task delegation infrastructure from the receiving end
