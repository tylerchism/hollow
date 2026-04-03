"""task_executor.py — Durable MC task execution for Hollow agents.

Picks up ready/in_progress MC tasks assigned to this executor, claims them,
and executes them via hail --detach + bin/wait-job.

Atomic claim contract
---------------------
When a task is claimed (set to in_progress), heartbeat_at and lease_expires_at
are written in THE SAME API call as status=in_progress.  This eliminates the
null-heartbeat window that existed when claim and first heartbeat were separate
calls.  If the watchdog sees a task in_progress with a null or stale heartbeat_at
it will reclaim it — writing them atomically prevents false reclaims immediately
after claim.

Optimistic lock dependency
--------------------------
# TODO(optimistic-lock): The MC API lacks an atomic claim operation.
# Currently claim is: PATCH /tasks/<id> {status: in_progress, heartbeat_at: now,
# lease_expires_at: now+120min, locked_by: <executor>}.  If two executor instances
# race, both may succeed and double-claim the same task.  The fix requires an MC
# API endpoint: POST /tasks/<id>/claim?executor=<name> that succeeds only if
# locked_by IS NULL and status IN ('ready', 'backlog').  Until that exists,
# deploy exactly one task_executor instance per task pool.
# Backlog task created: see Step 6 in implementation notes.

Long-running task flow (long_running: true OR estimated duration > 5 min)
--------------------------------------------------------------------------
1. hail --detach <agent> "<prompt>"  →  captures job_id
2. mc tasks update <id> --notes "fire_job_id: <job_id> | agent: <agent>"
3. bin/wait-job <agent> <job_id> --task-id <mc_task_id>
   - polls /result/<job_id> every 30s
   - posts heartbeat to MC on each poll
   - exits 0=done, 1=error, 2=cancelled/404, 3=timeout
4. Map exit code to MC task status (done / blocked)

Recovery: if task_executor picks up in_progress task with fire_job_id in notes,
poll /result first.  If pending → resume wait-job.  If done → mark done.
If 404 → re-fire.

expected_duration_minutes field
--------------------------------
# TODO(expected-duration-minutes): MC tasks schema does not yet have an
# expected_duration_minutes field.  task_executor uses LONG_TASK_THRESHOLD_MINS
# (env: LONG_TASK_THRESHOLD_MINS, default 5) to determine whether to use the
# fire+wait-job path.  Once the field exists, executors should set it on claim
# so the watchdog can use dynamic thresholds instead of the fixed 15-minute
# default.  Until then, long-running tasks must be tagged with 'long_running'.
# Backlog task created: nS2HVcpI6pvGawLE85eCN
# See also: MC schema backlog item for adding the column to tasks SQLite.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

HOLLOW_ROOT = Path(__file__).parent.parent.resolve()
BIN_MC = str(HOLLOW_ROOT / "bin" / "mc")
BIN_HAIL = str(HOLLOW_ROOT / "bin" / "hail")
BIN_WAIT_JOB = str(HOLLOW_ROOT / "bin" / "wait-job")

# Executor identity — set via env var so multiple agents can share MC
EXECUTOR_NAME = os.environ.get("TASK_EXECUTOR_NAME", "tarn")

# Lease duration for claimed tasks (seconds); watchdog reclaims if exceeded
LEASE_DURATION_SECS = int(os.environ.get("TASK_LEASE_SECS", "7200"))  # 2 hours

# Estimated duration threshold above which we use fire+wait-job (minutes)
LONG_TASK_THRESHOLD_MINS = int(os.environ.get("LONG_TASK_THRESHOLD_MINS", "5"))


def _now_iso() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _lease_iso(duration_secs: int = LEASE_DURATION_SECS) -> str:
    """Return a UTC ISO-8601 timestamp <duration_secs> from now."""
    return (datetime.now(timezone.utc) + timedelta(seconds=duration_secs)).isoformat()


def _run_mc(*args: str) -> dict:
    """Run the mc CLI with the given args. Returns parsed JSON or raises."""
    import json
    cmd = [BIN_MC] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(
            f"mc {' '.join(args)} failed (rc={result.returncode}): {result.stderr.strip()}"
        )
    return json.loads(result.stdout)


def claim_task(task_id: str) -> dict:
    """Atomically claim a task: set in_progress + heartbeat_at + lease_expires_at.

    Writes all three fields in a single PATCH call to eliminate the
    null-heartbeat window between claim and first heartbeat tick.

    Returns the updated task dict from MC.
    """
    now = _now_iso()
    lease = _lease_iso()
    log.info(
        "Claiming task %s: status=in_progress heartbeat_at=%s lease_expires_at=%s locked_by=%s",
        task_id, now, lease, EXECUTOR_NAME,
    )
    return _run_mc(
        "tasks", "update", task_id,
        "--status=in_progress",
        f"--heartbeat-at={now}",
        f"--lease-expires-at={lease}",
        f"--locked-by={EXECUTOR_NAME}",
    )


def post_heartbeat(task_id: str) -> None:
    """Update heartbeat_at and extend lease_expires_at for an in_progress task."""
    now = _now_iso()
    lease = _lease_iso()
    _run_mc(
        "tasks", "update", task_id,
        f"--heartbeat-at={now}",
        f"--lease-expires-at={lease}",
    )
    log.debug("Heartbeat posted for task %s (lease until %s)", task_id, lease)


def mark_done(task_id: str, notes: Optional[str] = None) -> None:
    """Mark a task done, clearing heartbeat/lease fields."""
    args = ["tasks", "update", task_id, "--status=done"]
    if notes:
        args += [f"--notes={notes}"]
    _run_mc(*args)
    log.info("Task %s marked done", task_id)


def mark_blocked(task_id: str, reason: str, notes: Optional[str] = None) -> None:
    """Mark a task blocked with a reason."""
    # MC requires blocked_reason to contain a task ID or 'needs-tyler'
    # For executor-generated blocks, prefix with needs-tyler so the API accepts it
    safe_reason = reason if "needs-tyler" in reason.lower() else f"needs-tyler: {reason}"
    args = ["tasks", "update", task_id, "--status=blocked", f"--blocked-reason={safe_reason}"]
    if notes:
        args += [f"--notes={notes}"]
    _run_mc(*args)
    log.warning("Task %s marked blocked: %s", task_id, reason)


def fire_job(agent: str, message: str) -> str:
    """Fire a detached job via hail --detach. Returns job_id."""
    result = subprocess.run(
        [BIN_HAIL, "--detach", agent, message],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"hail --detach failed (rc={result.returncode}): {result.stderr.strip()}"
        )
    job_id = result.stdout.strip()
    if not job_id.startswith("fire_"):
        raise RuntimeError(f"hail --detach returned unexpected output: {job_id!r}")
    return job_id


def wait_job(agent: str, job_id: str, task_id: Optional[str] = None, max_minutes: int = 120) -> int:
    """Run bin/wait-job and return its exit code.

    Exit codes:
      0 = done (success)
      1 = error (job failed)
      2 = cancelled or 404 (re-fire)
      3 = timeout
    """
    cmd = [BIN_WAIT_JOB, agent, job_id]
    if task_id:
        cmd += ["--task-id", task_id]
    cmd += ["--max-minutes", str(max_minutes)]
    result = subprocess.run(cmd, timeout=(max_minutes + 5) * 60)
    return result.returncode


def execute_task(task: dict) -> None:
    """Execute a single MC task.

    For tasks with long_running=true in tags or estimated duration > threshold:
      - Use fire+wait-job pattern
    For short tasks:
      - Use hail synchronously (blocking)
    """
    task_id = task["id"]
    title = task.get("title", "")
    description = task.get("description", "")
    tags = task.get("tags", [])
    notes = task.get("notes", "")
    agent = task.get("assigned_to", EXECUTOR_NAME)

    # ── Atomic claim: sets in_progress + heartbeat_at + lease_expires_at in one call ──
    claim_task(task_id)

    # Detect if this is a recovery scenario (fire_job_id in notes)
    fire_job_id: Optional[str] = None
    if notes and "fire_job_id:" in notes:
        for part in notes.split("|"):
            part = part.strip()
            if part.startswith("fire_job_id:"):
                fire_job_id = part.split(":", 1)[1].strip()
            elif part.startswith("agent:"):
                agent = part.split(":", 1)[1].strip()

    is_long_running = "long_running" in tags or fire_job_id is not None
    prompt = f"{title}\n\n{description}".strip()

    if is_long_running:
        _execute_long_task(task_id, agent, prompt, fire_job_id=fire_job_id)
    else:
        _execute_short_task(task_id, agent, prompt)


def _execute_long_task(
    task_id: str,
    agent: str,
    prompt: str,
    fire_job_id: Optional[str] = None,
) -> None:
    """Fire+wait-job execution path for long-running tasks.

    If fire_job_id is provided (recovery), skip the fire step and resume polling.
    """
    # Recovery: check existing job first
    if fire_job_id:
        import urllib.request, json as _json
        import urllib.error
        try:
            hollow_cfg = _load_hollow_config()
            base_url = hollow_cfg.get(agent, "http://127.0.0.1:8080")
            with urllib.request.urlopen(f"{base_url}/result/{fire_job_id}", timeout=10) as r:
                rec = _json.loads(r.read())
            if rec.get("status") == "done":
                mark_done(task_id, notes=f"recovered job {fire_job_id} — already done")
                return
            elif rec.get("status") == "error":
                mark_blocked(task_id, f"needs-tyler: job {fire_job_id} failed: {rec.get('response','')[:200]}")
                return
            # pending or unknown: fall through to wait-job
            log.info("Recovering task %s: job %s still pending — resuming wait-job", task_id, fire_job_id)
        except Exception as exc:
            log.warning("Recovery poll failed for job %s: %s — re-firing", fire_job_id, exc)
            fire_job_id = None  # will re-fire below

    # Fire step (new job or re-fire after 404)
    if fire_job_id is None:
        try:
            fire_job_id = fire_job(agent, prompt)
            log.info("Task %s fired as job %s on agent %s", task_id, fire_job_id, agent)
        except Exception as exc:
            mark_blocked(task_id, f"needs-tyler: fire failed: {exc}")
            return

        # Record job_id in task notes
        try:
            _run_mc(
                "tasks", "update", task_id,
                f"--notes=fire_job_id: {fire_job_id} | agent: {agent}",
            )
        except Exception as exc:
            log.warning("Could not update notes for task %s: %s", task_id, exc)

    # Wait for completion (wait-job handles heartbeats)
    exit_code = wait_job(agent, fire_job_id, task_id=task_id)

    if exit_code == 0:
        mark_done(task_id, notes=f"completed via job {fire_job_id}")
    elif exit_code == 1:
        mark_blocked(task_id, f"needs-tyler: job {fire_job_id} failed")
    elif exit_code == 2:
        # 404 or cancelled — re-fire
        log.warning("Task %s: job %s cancelled/404, re-firing", task_id, fire_job_id)
        _execute_long_task(task_id, agent, prompt, fire_job_id=None)
    elif exit_code == 3:
        mark_blocked(task_id, "needs-tyler: job timed out after 120 min")
    else:
        mark_blocked(task_id, f"needs-tyler: wait-job exited with unexpected code {exit_code}")


def _execute_short_task(task_id: str, agent: str, prompt: str) -> None:
    """Execute a short task synchronously via hail (blocking call to /ask)."""
    try:
        result = subprocess.run(
            [BIN_HAIL, agent, prompt],
            capture_output=True, text=True,
            timeout=360,  # 6 min ceiling for "short" tasks
        )
        if result.returncode == 0:
            mark_done(task_id, notes=f"hail response: {result.stdout.strip()[:500]}")
        else:
            mark_blocked(
                task_id,
                f"needs-tyler: hail failed (rc={result.returncode}): {result.stderr.strip()[:200]}",
            )
    except subprocess.TimeoutExpired:
        mark_blocked(task_id, "needs-tyler: hail timed out after 6 min — use long_running tag")
    except Exception as exc:
        mark_blocked(task_id, f"needs-tyler: execution error: {exc}")


def _load_hollow_config() -> dict[str, str]:
    """Load agent name -> base URL mapping from hollow.config.json."""
    import json
    cfg_path = HOLLOW_ROOT / "hollow.config.json"
    if not cfg_path.exists():
        return {}
    try:
        cfg = json.loads(cfg_path.read_text())
        result = {}
        for agent in cfg.get("agents", []):
            keyword = agent.get("hail_keyword", agent.get("name", ""))
            port = agent.get("port")
            if keyword and port:
                result[keyword.lower()] = f"http://127.0.0.1:{port}"
        return result
    except Exception:
        return {}
