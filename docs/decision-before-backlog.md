# Decision-Before-Backlog Protocol

**Status:** Active (provisional definition — v1, ships 2026-03-26)

---

## The Rule

Before creating any task that requires a Tyler-owned definition, classification rule, preference, or approval criteria, that definition must be either:

**(A) Already made** — it exists in memory/, a prior task's notes, or a previous Tyler decision. Reference it explicitly in the task description.

**(B) Explicitly scoped into the task** — the task's first subtask must be: *"Get Tyler's answer on [specific question]."* The task enters backlog in a pre-gated state until that answer exists.

**Do not create a task that silently depends on a Tyler definition that doesn't exist yet.** That task will stall immediately upon entering the backlog, and the stall will be invisible.

---

## What Counts as Tyler-Owned

**Provisional definition (Candidate A):** A decision is Tyler-owned if no agent can resolve it without a call from Tyler.

This means: agents couldn't resolve it by searching memory/, inferring from prior decisions, delegating to a specialist, or applying established team norms. If it genuinely requires Tyler's direct input, it's Tyler-owned.

*This definition is provisional. It will be adjusted based on real cases as they arise. Tyler can override any specific ruling — each override becomes a precedent that narrows or refines the definition.*

---

## How to Apply It

**When creating a task:**
1. Ask: does this task require a Tyler-owned decision to be actionable?
2. If yes, and the decision already exists → reference it in the description. Task proceeds normally.
3. If yes, and the decision doesn't exist → scope it as subtask 1: *"Tyler decision needed: [specific question]."* Tag the task `needs-tyler`. It enters backlog as pre-gated.

**When a task stalls in backlog:**
- If the reason is a missing Tyler definition, apply (B) retroactively — surface the specific question to Tyler before the task proceeds.
- Do not let stalled tasks accumulate without a visible question attached.

---

## What This Fixes

The pattern this protocol addresses: a task requiring Tyler to define something entered the backlog before that definition existed. The task then stalled immediately, and the stall was invisible — it looked like a backlog task, not a pending decision.

The fix is to make the dependency explicit at task-creation time, not at stall time.

---

## Exceptions

- Spikes explicitly scoped to *discover* what Tyler should decide are exempt — their purpose is to narrow the decision space before Tyler is asked.
- Tasks where the "Tyler-owned" aspect is secondary and the primary work can proceed in parallel are exempt, as long as the decision dependency is documented.

---

## Related

- `pending-decisions.md` — not yet built; blocked on Tyler confirming this definition (task `xJPko5SSTausD-CpQMiEp`)
- Task `XIhUX6IAH80RqDRjoK0qo` — the original task that spawned this protocol; unblocked by this doc shipping
