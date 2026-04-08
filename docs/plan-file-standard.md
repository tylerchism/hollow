# Plan File Standard

Plan files are structured Markdown documents that define multi-step tasks for autonomous execution by Hollow agents. They live in the `plans/` directory and are referenced from MC task records via the `plan_file:` notes field.

---

## Required Fields

### `goal`
One sentence. What is this task trying to accomplish?

### `success_criteria`
Binary, testable. How do we know the task is done? Each criterion must be answerable with yes/no.

### `subtasks[]`
An ordered list of subtasks. Each subtask has:

| Field | Type | Values | Description |
|-------|------|--------|-------------|
| `id` | string | unique slug | Stable identifier for this subtask (used in progress.json) |
| `title` | string | — | Short human-readable name |
| `autonomy` | enum | `full` / `tyler-checkpoint` / `blocked-on-tyler` | Whether the agent can proceed without Tyler input |
| `status` | enum | `pending` / `in-progress` / `done` / `skipped` | Current state (updated by agent as work progresses) |
| `agent` | string | agent keyword | Which agent executes this subtask (e.g., `forge`, `spring`, `tap`) |

**Autonomy levels:**
- `full` — agent executes without checking in with Tyler
- `tyler-checkpoint` — agent pauses, posts results to Discord, and waits for Tyler's response before continuing
- `blocked-on-tyler` — agent cannot start without explicit Tyler input (e.g., Tyler must provide an asset, a decision, or an approval)

### `progress_file`
Relative path to the `progress.json` file for this task. Conventionally `plans/<task-name>/progress.json` when a subdirectory is used, or `plans/progress-<task-name>.json` for a flat layout.

### `tyler_checkpoints[]`
Explicit list of subtask IDs that require Tyler input before the agent continues. This duplicates the `tyler-checkpoint` autonomy level on individual subtasks in a single flat list, making it easy for the task executor to scan without iterating all subtasks.

### `escalation_rules`
What to do on failure. Can be per-subtask or global. Format: free prose or a short list. At minimum, specify:
- What counts as a failure for this task (wrong output, timeout, missing asset)
- Whether to retry, skip, or escalate to Tyler
- What information Tyler needs to unblock the escalation

### `restart_robustness`
How to resume if the system restarts mid-task. Must reference `progress_file` and describe which subtasks are idempotent (safe to re-run) vs. stateful (require the checkpoint to avoid re-doing work).

---

## File Location

```
plans/
  <task-name>.md          # plan file
  progress-<task-name>.json   # progress file (flat layout)
```

Or in a subdirectory for tasks with many output artifacts:

```
plans/<task-name>/
  plan.md
  progress.json
  candidates.json         # task-specific outputs
```

## MC Integration

When creating an MC task for a plan-file-driven job:
- Store the plan file path in the task notes: `plan_file: plans/<task-name>.md`
- Tag the task `long_running` if any subtask is expected to take >5 minutes
- The task executor reads the notes, locates the progress file, and injects checkpoint state into the agent prompt on pickup

---

## Example Plan File

```markdown
# Article Rewrite: Tyler's Voice

## Goal
Rewrite docs/articles/article-{1..3}-draft.md in Tyler's voice.

## Success criteria
- Each article rewritten to match Tyler's voice per agents/spring/voice.md
- Reed approves each rewrite (no voice drift flags)
- Tyler receives final drafts and confirms acceptance

## Subtasks

- id: spring-article-1
  title: Spring draft — article 1
  autonomy: full
  status: pending
  agent: spring

- id: reed-article-1
  title: Reed edit — article 1
  autonomy: full
  status: pending
  agent: reed

- id: spring-revision-article-1
  title: Spring revision — article 1
  autonomy: full
  status: pending
  agent: spring

- id: spring-article-2
  title: Spring draft — article 2
  autonomy: full
  status: pending
  agent: spring

- id: reed-article-2
  title: Reed edit — article 2
  autonomy: full
  status: pending
  agent: reed

- id: spring-revision-article-2
  title: Spring revision — article 2
  autonomy: full
  status: pending
  agent: spring

- id: tyler-review
  title: Tyler reviews all final drafts
  autonomy: tyler-checkpoint
  status: pending
  agent: tarn

## progress_file
plans/progress-article-rewrite.json

## tyler_checkpoints
- tyler-review

## escalation_rules
- If Reed flags >3 major voice drift issues on any article: pause, post to
  #content, request Tyler review voice.md before continuing.
- If hail spring/reed fails with HTTP error after 2 retries: mark task
  blocked with needs-tyler tag and a note identifying the failing article.
- Global: if >50% of subtasks fail, escalate immediately rather than
  continuing partial completion.

## restart_robustness
progress_file is the checkpoint. On restart:
1. Read progress-article-rewrite.json.
2. Skip all subtasks listed in completed_subtasks.
3. Re-run current_subtask from the beginning (all spring/reed calls are
   idempotent — re-running overwrites the previous draft with a fresh one).
4. Articles not yet started: begin from spring draft.
```
