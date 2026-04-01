# Temporal Spike: Evaluation for Long-Running Agent Task Durability

**Date:** 2026-03-31
**Verdict:** SKIP — existing architecture covers the problem adequately

---

## The Problem

Tarn runs tasks that take 2-120 minutes. If Tarn crashes mid-task, the task loses all progress and restarts from scratch on next pickup. The question: would Temporal.io solve this, and is it worth adopting?

---

## Q1: Does Temporal solve the problem?

Yes, exactly. Temporal's durable execution model persists workflow state at each activity boundary. If the process crashes mid-workflow, the workflow resumes from the last completed activity checkpoint on next worker startup. Checkpoint granularity is per-activity — each `await` on an activity in the Python SDK is a durable checkpoint.

## Q2: Operational cost

**Self-hosted:** Requires a Temporal server (Go binary), a database (PostgreSQL or Cassandra), and worker processes. Minimum viable: `temporal server start-dev` (all-in-one, SQLite backend). Adds 2-3 services to manage. Non-trivial.

**Temporal Cloud:** ~$25/month hobby tier. Near-zero operational overhead but adds external dependency.

## Q3: Migration cost

Estimated 3-5 days engineering for a Python developer to migrate one task type (e.g., WhisperX batch) to Temporal. Learning curve is real: workflow/activity separation, determinism constraints on workflow code, versioning. Not plug-and-play.

## Q4: Alternatives

**SQLite checkpointing (recommended):** Each task stage writes progress to SQLite. On re-run, skip completed stages. Solves 80% of the crash-recovery problem. Cost: 1-2 hours per task type. The WhisperX batch already does this (skips existing `timestamps.json` files).

**State file checkpointing:** Task writes a `.progress` file after each phase. Language-agnostic, zero infrastructure. Solves ~70% of cases without any new infrastructure.

**What Hollow already has:** The heartbeat/lease system + `restart_policy: idempotent` covers the most important case: watchdog detects crashed tasks, resets them to ready, and they restart cleanly. Many tasks are already idempotent by design.

## Q5: Verdict

**SKIP Temporal.** The existing architecture already covers 80-90% of the crash-recovery problem:
- Watchdog detects expired leases → resets to ready
- `restart_policy: idempotent` enables clean restarts  
- Task-level idempotency (skip-existing patterns) handles the long-task case

Adding Temporal would add significant operational overhead (database, separate Go service, SDK migration) for marginal gain. The remaining gap is best addressed by task-level SQLite checkpointing patterns applied where needed, not by adopting distributed workflow infrastructure.

**Revisit if:** Hollow scales to 5+ concurrent long-running tasks that each take >60 minutes and crash recovery matters within seconds, not minutes.

---

## Spike Closing: STATE C — CLOSE WITHOUT CONVERSION

Verdict is conclusive and negative on adoption. No downstream build task needed.
