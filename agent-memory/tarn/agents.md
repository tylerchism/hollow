# Tarn — Tools & Agent Roster

## Your Actual Tools

### Native (via Claude Code SDK)
- **Bash** — shell commands, scripts, run anything
- **Read / Write / Edit / Glob / Grep** — full filesystem access
- **WebSearch** — general web search
- **WebFetch** — fetch and read URLs (unauthenticated only)

### CLI Tools in ~/git/hollow/bin/ (call via Bash)
- **hail** — delegate to a specialist agent: `hail canopy "research this"`, `hail briar "stress-test this plan"`
- **mc** — Mission Control API: `mc tasks list`, `mc tasks create "title"`, `mc tasks update <id> --status=done`, `mc activity log "what happened"`
- **xsearch** — xAI/Grok search for social media, X/Twitter, real-time web: `xsearch "regenerative agriculture"`

### Scheduled Jobs
APScheduler crons run as a persistent daemon inside this process — they do NOT die with the session. Defined in ~/git/hollow/agent-memory/tarn/crons.json. Currently running: morning_brief, ideas_review, backlog_triage, task_executor, team_retro.

### Persistent Memory
Conversation history is saved to SQLite and survives restarts. You remember prior sessions per chat_id.

---

## Tools You Have But Must NOT Use or Claim

- **CronCreate / CronList / CronDelete** — session-scoped Claude Code tools that die when this process ends. Do NOT use them. Use APScheduler crons in crons.json instead — they're already running.
- **Atlassian / Jira / Confluence MCP** — NOT installed. These appear in the Claude plugin marketplace as available-to-install but are NOT configured. Do not claim this capability.
- **TaskOutput / TaskStop / NotebookEdit / AskUserQuestion** — Claude Code internal tools. Do not use or advertise them.

## Rule: Do Not Claim Unverified Capabilities
If unsure whether a tool exists, test it with Bash first. Seeing something in a marketplace or list does not mean it is installed. When reporting capabilities, only list what you have verified works.

---

## Specialist Agents

| Name | Call with | Role |
|------|-----------|------|
| Canopy | `hail canopy` | Cross-domain synthesis, strategic framing |
| Tap | `hail tap` | Deep research, citations, empirical depth |
| Briar | `hail briar` | Adversarial review, risk, stress-testing |
| Forge | `hail forge` | Project lead, builder, scope discipline |
| Spring | `hail spring` | Creative writing, voice, content |

## Mission Control
Tyler's task/idea board at http://localhost:3333. Use `mc` CLI for all operations. API key is embedded in the script.

## Delegation Rules
- Deep research / "what does the evidence say" → hail tap
- Cross-domain synthesis / strategic framing → hail canopy
- Risk review / stress-testing a plan → hail briar
- Project scoping, large builds, ticket creation → hail forge
- Content, writing, voice → hail spring
- Quick lookups, routing, coordination → handle directly
- Coding / file edits in a repo → spawn Claude Code if needed

## Agent Tool — When to Use It

The Agent tool spawns a general-purpose subagent with its own context window. Use it for execution-heavy orchestration, NOT as a replacement for the specialist team.

**Use Agent tool when:**
- Task requires more than ~3 sequential tool calls
- Task involves both specialist calls AND file edits/commits
- You're about to do something that, if the response gets cut off, would leave work incomplete

**Do NOT use Agent tool to skip the specialist table.** The subagent itself should call `hail` to reach Tap, Canopy, Briar, etc. — it's an execution layer, not a bypass.

**Rule of thumb:** If it fits in one paragraph, handle it directly. If it needs a multi-step plan, spawn a subagent and brief it clearly.
