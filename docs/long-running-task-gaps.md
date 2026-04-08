# Long-Running Task Robustness: Gaps

_Phase 3c audit. Scope: tasks that take 30+ minutes (e.g., clip extraction, multi-article pipelines)._

---

## Current setup summary

- **Lease duration:** 2 hours (`TASK_LEASE_SECS=7200`, env-overridable). Set atomically at claim.
- **Heartbeat loop:** `bin/wait-job` heartbeats the MC task on every poll (every 30s). Each heartbeat extends the lease by another 2 hours from now.
- **Watchdog:** Pings agent HTTP health endpoints. If an agent is down, it restarts via `bin/restart-agent`. Watchdog does NOT reset stale MC leases — it only checks agent process liveness.
- **Claim:** Now atomic via `POST /api/tasks/:id/claim`. Race condition is closed.

---

## Gaps

### 1. Watchdog does not reset expired MC leases

The watchdog (`bin/watchdog`) checks agent process health and restarts dead agents. It does not query MC for tasks with expired `lease_expires_at`. If a task's executor process dies between heartbeats (or the agent crashes silently without watchdog catching it), the MC task stays `in_progress` indefinitely with a stale lease.

**Who resets it?** Nothing currently. The heartbeat checks in the redesign doc (Section 2, "Task lease audit") are documented as planned but not implemented in the heartbeat cron.

**Impact:** A crashed task is stuck `in_progress` until manually cleared. The next task_executor run will not pick it up because `status != ready`.

**Fix needed:** Either the watchdog or the heartbeat cron must query `GET /api/tasks?status=in_progress`, check each task's `lease_expires_at`, and PATCH expired tasks back to `ready` (with a `stale_lease` tag). This is the watchdog's job per the redesign doc.

---

### 2. Mid-hail restart loses the in-flight job ID

If task_executor is in the `fire_job()` call (between firing and recording the job ID in MC notes) and the process is killed, the job was fired but the `fire_job_id` was never written to MC. On recovery, the task has no `fire_job_id` in its notes, so task_executor re-fires — creating a duplicate job.

**Impact:** Duplicate hail calls. For idempotent tasks (write-only, non-destructive) this is acceptable. For stateful tasks (e.g., a job that posts to Discord and marks something in a DB) it could create duplicate outputs.

**Frequency:** Very low — the window is a few hundred milliseconds between fire and the notes PATCH. But it exists.

**Partial mitigation:** The current `restart_policy` field on MC tasks is set to `idempotent` by default. A future fix would write the `fire_job_id` to the plan's `progress.json` before notifying MC, so recovery can find it even if the MC notes write failed.

---

### 3. wait-job has a 2-hour absolute max; no dynamic timeout from task

`wait-job` defaults to `--max-minutes 120`. task_executor does not pass a task-specific timeout (e.g., from `expected_duration_minutes` on the MC task). A 3-hour clip extraction job would time out and be marked blocked.

**Impact:** Long tasks with `expected_duration_minutes > 120` will be killed at 120 minutes.

**Fix needed:** task_executor should pass `--max-minutes $(( expected_duration_minutes * 2 ))` (with a floor) when calling `wait_job()`. The `expected_duration_minutes` field already exists in the MC task schema.

---

### 4. Agent restart during wait-job loses the /result endpoint

If the target agent (e.g., Forge) restarts while `wait-job` is polling, the in-flight job's result disappears from memory (the `/result/<job_id>` endpoint returns 404). `wait-job` exits with code 2 (cancelled/404), and task_executor re-fires the job.

**Impact:** For long jobs, re-firing means restarting from scratch — unless the agent's task implements its own progress.json and the prompt includes checkpoint context.

**Current state:** The re-fire path is implemented (`_execute_long_task` calls itself recursively with `fire_job_id=None`). The checkpoint context injection (Phase 3b) mitigates the restart-from-scratch problem if the agent wrote progress.json. But if the agent hadn't written any progress before the restart, work is lost.

**No fix needed now** (acceptable tradeoff for current task types). Revisit when tasks have external side effects that can't be replayed.

---

## Items confirmed working

- Atomic claim: race condition closed (Phase 3 carryover, now done).
- Heartbeat during wait-job: every 30s poll extends the lease. A 30-minute task with a 2-hour lease will never expire due to heartbeat gaps.
- Watchdog restarts dead agents: process-level liveness is covered.
- Recovery path for existing fire_job_id: task_executor checks `/result` before re-firing.
