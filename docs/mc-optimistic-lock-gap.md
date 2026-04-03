# MC Optimistic Lock Gap

## What's Missing

The Mission Control API (`http://localhost:3333`) does **not** support an atomic claim operation. There is no `/api/tasks/<id>/claim` endpoint (tested: returns 404). The only way to claim a task is via a standard PATCH:

```
PATCH /api/tasks/<id>
{"status": "in_progress", "heartbeat_at": "...", "lease_expires_at": "...", "locked_by": "task_executor"}
```

This PATCH is not conditional — it succeeds regardless of the task's current state. There is no support for optimistic concurrency (e.g. `If-Match: <etag>` or `expected_status: ready` in the body).

## The Double-Claim Risk

If two instances of `task_executor` run simultaneously (e.g. a cron overlap because the previous run was slow, or two agents that share the same cron schedule), both can:

1. Call `mc tasks list --status=ready` at nearly the same time and see the same set of ready tasks.
2. Both issue `PATCH status=in_progress` for the same task — both PATCHes succeed.
3. Both execute the task. The last writer wins for status updates, so one executor's work silently overwrites the other's.

**Concrete failure modes:**
- Duplicate work: both agents call `hail forge "build X"` for the same task — two simultaneous Forge sessions, potential file conflicts.
- Data corruption for tasks with side effects (filesystem writes, git commits).
- `locked_by` field reflects whichever PATCH landed last, not the one actually doing the work.

## Current Mitigation (partial)

Step 1 of this system (already shipped) writes `status=in_progress + heartbeat_at + locked_by` in a **single atomic PATCH**, which eliminates the null-heartbeat window. This reduces the race window from "time between two separate API calls" to "network RTT for a single call." In practice, with a 30-minute cron interval and typical <100ms API latency, double-claim remains unlikely but is not architecturally prevented.

## What the MC API Would Need to Add

To make task claim truly atomic and safe, MC needs one of the following:

### Option A: Dedicated `/claim` endpoint with conflict detection

```
POST /api/tasks/<id>/claim
Body: {"executor": "task_executor", "expected_status": "ready"}
```

**Behavior:**
- If `status == "ready"`: atomically set `status=in_progress`, `locked_by=<executor>`, `heartbeat_at=now`, `lease_expires_at=now+120m`. Return 200 with the updated task.
- If `status != "ready"`: return **409 Conflict** with `{"error": "task not in ready state", "current_status": "in_progress"}`.
- The check+write must be a single DB transaction.

**Caller behavior on 409:** skip this task and move to the next one in the ready list. No retry.

### Option B: Conditional PATCH via `expected_status` field

```
PATCH /api/tasks/<id>
Body: {"status": "in_progress", "expected_status": "ready", ...}
```

**Behavior:**
- If `current_status == expected_status`: apply the PATCH, return 200.
- If `current_status != expected_status`: return 409 Conflict.

This is similar to optimistic locking via ETag headers (`If-Match`) but task-specific.

### Option C: ETag / If-Match header support

Standard HTTP optimistic concurrency: include an `ETag` in GET/PATCH responses, require `If-Match: <etag>` on claim PATCHes. Return 412 Precondition Failed if the ETag has changed.

---

## Recommended Path

Option A (dedicated `/claim`) is the cleanest — it makes the intent explicit, requires no client-side ETag tracking, and maps directly to the "claim a unit of work" workflow pattern. It should be implemented in the MC Next.js API at `app/api/tasks/[id]/claim/route.ts`.

Until MC adds atomic claim, the current double-claim risk is **low** (requires cron overlap within a ~100ms window) but **non-zero**. The watchdog's lease-expiry reset and `needs-review` tag provide a recovery path if double-claims do occur.
