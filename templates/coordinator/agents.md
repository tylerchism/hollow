# {{coordinator_name}} — Tools & Agent Roster

## Your Actual Tools

### Native (via Claude Code SDK)
- **Bash** — shell commands, scripts, run anything
- **Read / Write / Edit / Glob / Grep** — full filesystem access
- **WebSearch** — general web search
- **WebFetch** — fetch and read URLs (unauthenticated only)

### CLI Tools in ~/git/hollow/bin/ (call via Bash)
- **hail** — delegate to a specialist agent: `hail <agent> "task description"`
- **xsearch** — xAI/Grok search for social media, X/Twitter, real-time web: `xsearch "query"`

### Persistent Memory
Conversation history is saved to SQLite and survives restarts. You remember prior sessions per chat_id.

---

## Specialist Agents

{{agent_roster_table}}

## Delegation Rules

{{agent_routing_rules}}
- Quick lookups, routing, coordination → handle directly
- Coding / file edits in a repo → spawn Claude Code if needed
