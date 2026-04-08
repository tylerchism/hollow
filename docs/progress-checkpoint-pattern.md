# Progress Checkpoint Pattern

## Purpose

`progress.json` is the subtask-level checkpoint for multi-step Hollow tasks. It lives alongside the plan file in `plans/` and is the ground truth for resuming after a system restart.

MC holds task-level state (status, heartbeat, lease). `progress.json` holds subtask-level state (which steps are done, where we are, per-subtask notes). Both are needed. Neither alone is sufficient.

---

## Format

```json
{
  "plan_id": "string",
  "task_mc_id": "string",
  "started_at": "ISO timestamp",
  "updated_at": "ISO timestamp",
  "current_subtask": "subtask-id",
  "completed_subtasks": ["id1", "id2"],
  "failed_subtasks": [],
  "notes": {}
}
```

### Field definitions

| Field | Type | Description |
|-------|------|-------------|
| `plan_id` | string | Matches the plan file slug (e.g., `article-rewrite`) |
| `task_mc_id` | string | The MC task ID for this job (e.g., `TJUwhkIo21Dq7vUTAU_8B`) |
| `started_at` | ISO 8601 | When the task was first picked up |
| `updated_at` | ISO 8601 | Last write time — update on every subtask state change |
| `current_subtask` | string | Subtask ID currently in progress (or next to run) |
| `completed_subtasks` | string[] | IDs of subtasks that have successfully finished |
| `failed_subtasks` | string[] | IDs of subtasks that failed (with reason in `notes`) |
| `notes` | object | Free-form per-subtask notes (e.g., `{"reed-article-1": "3 voice drift flags, will retry"}`) |

---

## File Location

Progress files live adjacent to the plan file:

```
plans/
  article-rewrite.md
  progress-article-rewrite.json
```

Or inside a task subdirectory:

```
plans/article-rewrite/
  plan.md
  progress.json
```

The plan file's `progress_file` field specifies the relative path.

---

## Write Protocol

The executing agent is responsible for writing `progress.json`. The pattern:

1. **On first pickup:** Create `progress.json` with `started_at = now`, `current_subtask = first subtask id`, all other lists empty.
2. **After each subtask completes:** Add its ID to `completed_subtasks`, set `current_subtask` to the next subtask, update `updated_at`.
3. **On subtask failure:** Add its ID to `failed_subtasks`, write a note in `notes.<id>`, then decide per escalation_rules whether to continue or stop.
4. **On task completion:** `current_subtask` can be set to `""` or `"done"`. The MC task status (not the progress file) is the authoritative completion signal.

Write the file after each state transition, not in batches. A crash between writes loses at most one subtask's worth of work.

---

## How task_executor Uses progress.json

When task_executor picks up an `in_progress` task (recovery scenario) or a `ready` task that has a `plan_file:` pointer in its notes:

1. Reads the `plan_file:` value from task notes.
2. Derives the progress file path from the plan's `progress_file` field.
3. If the progress file exists: reads it and builds a `<progress_checkpoint>` context block.
4. Injects the context block into the agent prompt before calling hail.

The injected context tells the agent:
- Which subtask to resume from
- Which subtasks are already done (skip them)
- Any per-subtask notes from prior runs

If no progress file exists, task_executor passes the prompt as-is (first run, no checkpoint state).

---

## Linking progress.json to MC

Store the plan file path in the MC task's notes field using pipe-separated key-value format:

```
plan_file: plans/article-rewrite.md | fire_job_id: fire_abc123 | agent: forge
```

task_executor parses this format when reading the notes field. The `plan_file:` key is the trigger for progress checkpoint loading.

---

## Example: Mid-task restart

**State before restart:**
- Task `TJUwhkIo21Dq7vUTAU_8B` is `in_progress`, lease expired
- `plans/progress-article-rewrite.json` exists:

```json
{
  "plan_id": "article-rewrite",
  "task_mc_id": "TJUwhkIo21Dq7vUTAU_8B",
  "started_at": "2026-04-08T10:00:00Z",
  "updated_at": "2026-04-08T11:30:00Z",
  "current_subtask": "reed-article-2",
  "completed_subtasks": ["spring-article-1", "reed-article-1", "spring-revision-article-1", "spring-article-2"],
  "failed_subtasks": [],
  "notes": {}
}
```

**Recovery flow:**
1. Watchdog detects expired lease, resets task to `ready` with `needs-review` tag.
2. task_executor picks up the task.
3. Reads notes: `plan_file: plans/article-rewrite.md`.
4. Reads `plans/progress-article-rewrite.json`.
5. Builds context: current=`reed-article-2`, completed=`[spring-article-1, reed-article-1, spring-revision-article-1, spring-article-2]`.
6. Injects `<progress_checkpoint>` block into agent prompt.
7. Hails `reed` with full context — Reed resumes at article 2 rather than restarting from article 1.

No rework on completed articles. The worst case is re-running the current subtask once.
