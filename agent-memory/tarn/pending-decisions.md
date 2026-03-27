# Pending Decisions

Tyler-blocked decisions that need a call before agents can proceed. Anything here older than 3 days should surface in the morning brief.

---

## Protocol: decision-before-backlog

**Status:** PROVISIONAL (shipped 2026-03-27)

### Tyler-owned definition

> **"Tyler-owned" means: no agent can resolve without a Tyler call.**

This is Candidate A, chosen provisionally. Tyler adjusts from evidence as cases arise — not from abstract debate upfront.

### How the protocol works

Before creating any task that requires a Tyler-owned definition (classification rule, preference, approval criteria), that definition must be either:

- **(A)** Already made — agent can proceed
- **(B)** Explicitly scoped as the first subtask: "Get Tyler's answer on X"

If (B), the task does not enter the backlog in a blocked state. It enters ready, with the Tyler-call as step one.

### Why provisional

Candidate A was picked to unblock the system — the old approach (hold protocol until Tyler defines it perfectly) was itself the pattern the protocol was designed to prevent. Tyler will encounter edge cases and override specific rulings as they come. Each override is evidence that refines the definition. Update this doc when the definition evolves.

---

## Active pending decisions

_None currently._

---

_Resolved 2026-03-27:_
- **Cutover confirmation** — genuine completion confirmed by Tyler. Archivist cron and routing audit both moved to backlog.
- **RAG spike corpus PoC** — Daniel Vitalis, Tap handles retrieval, shared bin/retrieve. All 7 tickets created.
- **Architect binding vs advisory** — tabled, condition-blocked until Forge drops design under pressure.
- **Temporal spike** — closed, no evidence of cron unreliability at current scale.

---

_Add new entries here as they arise, with format:_

```
### [Decision title]
- **Date blocked:** YYYY-MM-DD
- **Decision needed:** One sentence.
- **Context:** Path to file with full context.
- **Escalate after:** YYYY-MM-DD (3 days from blocked date)
```
