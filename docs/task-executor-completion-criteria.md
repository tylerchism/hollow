# Task Executor — Explicit Completion Criteria

**Created:** 2026-03-22 | **Origin:** Briar retro flag (2026-03-21)

---

## The Problem

Briar flagged that task_executor was auto-completing tasks without explicit, documented criteria for what "done" means. Some completions had empty activity log payloads. Others shipped LLM chat responses with no persistent artifact. This doc defines explicit criteria per task type and establishes what a valid completion looks like.

---

## Completion Criteria by Task Type

### Research / Analysis (Canopy, Tap, Briar)
**Done when:**
- Specialist has returned a substantive response (not just "no issues found")
- Response has been either: (a) saved to a file, or (b) summarized and logged in agent-activity with `summary` field
- Activity log entry has non-empty `task_title`, `route_chosen`, and `summary`

**NOT done:**
- Specialist response delivered only as chat message with no persistent record
- Empty `payload` in activity log

### Writing / Content (Spring, Forge doc outputs)
**Done when:**
- File exists at the specified path
- File contains expected structure (verify with `head` or `grep`)
- Activity log entry references the file path in `summary`

**NOT done:**
- "I would write..." response without creating the file
- File path in log doesn't match actual file location

### Code / Build (Forge, claude-code)
**Done when:**
- Code file(s) exist at expected paths
- Build/run was attempted and result (pass/fail) noted
- If blocked on Tyler decisions: task status = `blocked` with `blocked_reason`, NOT `done`

**NOT done:**
- Scoped but not built
- `done` status with empty payload and no file evidence

### Documentation Updates (direct)
**Done when:**
- File diff confirmed (read the section, verify the content exists)
- Activity log `summary` states what was added/changed, not just that the task ran

### SPIKE Tasks (any route)
**Done when:**
- At least one follow-up task has been created in Mission Control
- The deliverable (doc, analysis, decision list) exists as a persistent artifact
- Follow-up task is linked or referenced in the activity log `summary`

**NOT done:**
- Spike "completed" with no follow-up task created
- Analysis delivered in chat with no file and no follow-up task

---

## Per-Cycle Logging Requirement

Every task_executor run should log a cycle manifest. When completing a task, the `mc activity log` entry MUST include:

```
completed: [task_title] | route: [route_chosen] | summary: [one-sentence outcome with artifact path if applicable]
```

If a task is marked `done` with an empty payload, it is treated as **unverified completion** and should be re-audited.

---

## Audit: Auto-Completions Through 2026-03-22

| Task | Route | Artifact | Status |
|------|-------|----------|--------|
| Update AGENTS.md with task.completed payload schema | direct | `agent-memory/tarn/agents.md` — routing schema section added | ✓ Verified |
| SPIKE: Define brief template standard for Spring tasks | hail canopy | `templates/spring-brief.md` exists with full template | ✓ Verified |
| Expand Briar's mandate to cover Ghost and Crow modes | direct | `agents/briar/soul.md` — `## Modes` section with Ghost + Crow | ✓ Verified |
| SPIKE: Evaluate Phase 2 Agent Additions | hail canopy | Chat-only summary — no persistent file | ⚠️ Partial (no artifact) |
| Build per-person vector knowledge bases (Subtask 1) | hail forge | `docs/rag-spike.md` exists — task correctly set to `blocked` | ✓ Verified |
| (claude-code, 2026-03-19) | claude-code | Empty payload, no task_title | ✗ Unverified |

**Gap:** The Phase 2 SPIKE evaluation was delivered as a chat message but no file was saved. The analysis (Architect > Editor > Archivist-as-cron, Ghost/Crow → Briar modes) was sound, but it has no persistent artifact. Recommendation: save Canopy spike outputs to `docs/` going forward.

---

## Remediation for Identified Gaps

1. **SPIKE: Evaluate Phase 2 Agent Additions** — output was acted on (Briar modes added, routing rules being written), but no doc exists. Low urgency — decisions already implemented.
2. **claude-code empty payload (2026-03-19)** — pre-Hollow era entry, cannot trace. Treat as legacy noise.
3. **Forward rule:** All Canopy/Tap research outputs for completed tasks must be saved to `docs/` or `agent-memory/` — not just delivered in the session response.
