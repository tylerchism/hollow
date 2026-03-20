# Briar — Memory

## System Architecture Review (2026-03-20)

Tarn requested a full eval of the hollow agent system after a cleanup session. Key findings I flagged:

**Risks identified:**
- Context is fire-and-forget: hail sends only a task string, no prior conversation. Sub-agents operate blind unless they've accumulated context under a stable chat_id from prior calls.
- Truncation is silent: agent.py catches stream errors after partial output without retry or warning. Tyler can receive mid-sentence responses.
- Tarn is a single point of failure with no hang detection — systemd handles crashes but not infinite loops or stuck Claude calls.
- Sub-agents always-on but idle: ~95MB RAM each for agents that might get one hail per day.
- chat_id "_main" hardcoded in hail: all calls to a given agent share one thread regardless of topic — context window fills with unrelated prior work.

**Fixes proposed (Tarn's tier system):**
- Tier 1 (done): killed duplicate Tarn poller, killed second_brain process, restarted stale sub-agents via systemd, cleaned junk DB rows.
- Tier 2 (open): stable chat_ids per agent per user (`<agent>_<tyler_chat_id>`), context injection in hail, /ask endpoint to accept `context` field.
- Tier 3 (open/nice-to-have): on-demand sub-agents with idle timeout, single-process multi-identity option, async parallel delegation.
- Tier 4 (open): heartbeat monitoring via /health endpoints, response validation before sending to Tyler, graceful degradation if sub-agent is down.

**Root cause of my own empty memory:**
- My soul.md had no continuity instruction — now fixed.
- hail calls are stateless, so I never had anything worth persisting.
- Fixed: soul.md updated to include continuity section (same pattern as Tarn's).

## Open Items
- Tier 2–4 fixes not yet implemented — Forge would be the right lead on scoping these.
