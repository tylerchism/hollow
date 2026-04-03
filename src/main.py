"""Hollow — AI agent runtime entry point."""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time as _time_module
import uuid
from pathlib import Path

from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.agent import AgentRunner
from src.channels import DiscordBot
from src.config import load_config
from src.memory import MemoryManager
from src.snapshot import read_snapshot, write_snapshot

log = logging.getLogger(__name__)

# In-memory store for fire job results.
# Key: job_id (e.g. "fire_abc12345")
# Value: {"status": "pending|done|error", "response": str|None, "created_at": float}
_fire_jobs: dict[str, dict] = {}

# Max concurrent fire jobs (active, not queued) — configurable via env var.
FIRE_CONCURRENCY_LIMIT = int(os.environ.get("FIRE_CONCURRENCY_LIMIT", "2"))

# Fire job results expire after 1 hour.
_FIRE_JOB_TTL_SECS = 3600


def _fire_job_id() -> str:
    """Generate a fire job ID: 'fire_' + first 8 hex chars of a UUID4."""
    return "fire_" + uuid.uuid4().hex[:8]


def _sweep_stale_fire_jobs() -> None:
    """Remove fire job records older than _FIRE_JOB_TTL_SECS from _fire_jobs."""
    now = _time_module.time()
    stale = [
        jid for jid, rec in _fire_jobs.items()
        if now - rec.get("created_at", 0) > _FIRE_JOB_TTL_SECS
    ]
    for jid in stale:
        del _fire_jobs[jid]
    if stale:
        log.debug("Swept %d stale fire job(s)", len(stale))


def make_api_app(
    agent: AgentRunner,
    discord_bot: "DiscordBot | None" = None,
) -> web.Application:
    """Create the aiohttp app for the HTTP API."""

    import time as _time

    # Claude API probe cache: (result_ok: bool, reason: str, checked_at: float)
    # TTL of 5 minutes so watchdog polls (every 5 min) don't hammer the API.
    _claude_probe_cache: list = [None, "", 0.0]
    _CLAUDE_PROBE_TTL = 300

    async def _probe_claude_api() -> tuple[bool, str]:
        """Send a minimal probe to the Claude API to verify reachability.

        Returns (ok, reason).  Uses claude_code_sdk (same transport the agent
        uses) with max_turns=1 so the call is as cheap as possible.
        Result is cached for _CLAUDE_PROBE_TTL seconds.
        """
        now = _time.time()
        if _claude_probe_cache[0] is not None and now - _claude_probe_cache[2] < _CLAUDE_PROBE_TTL:
            return _claude_probe_cache[0], _claude_probe_cache[1]

        try:
            from claude_code_sdk import (
                query as _sdk_query,
                ClaudeCodeOptions as _CCO,
                AssistantMessage as _AM,
            )
            options = _CCO(
                model="claude-haiku-4-5",
                max_turns=1,
                permission_mode="bypassPermissions",
            )
            got_response = False
            async for msg in _sdk_query(prompt="Reply with exactly: ok", options=options):
                if isinstance(msg, _AM):
                    got_response = True
            ok = got_response
            reason = "" if ok else "no response from claude CLI"
        except Exception as e:
            ok, reason = False, str(e)[:200]

        _claude_probe_cache[0] = ok
        _claude_probe_cache[1] = reason
        _claude_probe_cache[2] = _time.time()
        return ok, reason

    async def handle_health(request: web.Request) -> web.Response:
        cfg = agent.config
        agent_name = cfg.identity_dir.name if cfg.identity_dir else "hollow"

        bots: dict[str, bool] = {}
        if discord_bot is not None:
            try:
                bots["discord"] = bool(
                    discord_bot._client and discord_bot._client.is_ready()
                )
            except Exception:
                bots["discord"] = False

        # Probe the Claude API — degraded if unreachable
        claude_ok, claude_reason = await _probe_claude_api()

        degraded_reasons: list[str] = []
        if bots and not all(bots.values()):
            degraded_reasons.append("bot_disconnected")
        if not claude_ok:
            degraded_reasons.append(f"claude_api_unreachable: {claude_reason}")

        if degraded_reasons:
            payload: dict = {
                "status": "degraded",
                "agent": agent_name,
                "reasons": degraded_reasons,
            }
            if bots:
                payload["bots"] = bots
            return web.json_response(payload)

        payload = {"status": "ok", "agent": agent_name}
        if bots:
            payload["bots"] = bots
        return web.json_response(payload)

    async def handle_ask(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        message = data.get("message", "").strip()
        if not message:
            return web.json_response({"error": "message is required"}, status=400)

        chat_id = data.get("chat_id", "api")
        is_main_session = data.get("is_main_session", True)
        context_injection = data.get("context", "").strip()

        try:
            response = await agent.reply(
                message=message,
                chat_id=chat_id,
                is_main_session=is_main_session,
                context_injection=context_injection,
            )
            return web.json_response({"response": response})
        except Exception as e:
            log.exception("Error in /ask handler")
            return web.json_response({"error": str(e)}, status=500)

    async def handle_history(request: web.Request) -> web.Response:
        """Return recent message history for a named Discord channel.

        GET /history/<channel_name>?limit=50

        channel_name: Discord channel to read (e.g. "trader-bot")
        limit: number of messages to return, 1-100 (default 50)

        Returns plain text — one line per message, oldest first:
          [YYYY-MM-DD HH:MM:SS UTC] Author: message text
        """
        channel_name = request.match_info.get("channel_name", "").strip()
        if not channel_name:
            return web.Response(text="channel_name is required", status=400)

        try:
            limit = int(request.rel_url.query.get("limit", "50"))
        except ValueError:
            limit = 50

        if discord_bot is None:
            return web.Response(text="Discord bot is not enabled on this agent.", status=503)

        history_text = await discord_bot.get_channel_history(channel_name, limit=limit)
        return web.Response(text=history_text, content_type="text/plain")

    async def handle_snapshot(request: web.Request) -> web.Response:
        """POST /snapshot — synchronously write current state to SQLite.

        Called by restart-agent before sending SIGTERM so the new process can
        read a fresh snapshot and build a richer continuity message.
        """
        import time as _time

        # Collect background tasks from whichever manager(s) exist
        bg_tasks: list[dict] = []
        managers = []
        if discord_bot is not None:
            managers.append(discord_bot._bg_tasks)

        for mgr in managers:
            for task_id, record in list(mgr._tasks.items()):
                started_at = getattr(record, "started_at", None) or _time.time()
                bg_tasks.append({
                    "task_id": task_id,
                    "channel_type": record.channel_type,
                    "channel_id": record.channel_id,
                    "original_message": record.original_message,
                    "started_at": started_at,
                })

        # Collect cron job state from the scheduler stored on the app
        cron_jobs: list[dict] = []
        _scheduler = request.app.get("scheduler")
        if _scheduler is not None:
            try:
                for job in _scheduler.get_jobs():
                    next_run = job.next_run_time
                    cron_jobs.append({
                        "name": job.id,
                        "next_run_time": next_run.isoformat() if next_run else None,
                    })
            except Exception:
                log.exception("handle_snapshot: failed to collect cron state")

        write_snapshot(
            data_dir=agent.config.data_dir,
            bg_tasks=bg_tasks,
            cron_jobs=cron_jobs,
        )
        return web.json_response({"status": "ok", "bg_tasks": len(bg_tasks), "cron_jobs": len(cron_jobs)})

    # ── /fire endpoint ────────────────────────────────────────────────────────

    async def handle_fire(request: web.Request) -> web.Response:
        """POST /fire — queue work async, return job_id immediately (<1s).

        Accepts same payload as /ask: message, chat_id, context, idempotency_key,
        result_channel.

        Returns {"job_id": "fire_<uuid8>", "status": "queued"} immediately.
        If at FIRE_CONCURRENCY_LIMIT: also returns queue_position.
        If idempotency_key matches existing job: returns that job's id.
        """
        _sweep_stale_fire_jobs()

        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        message = data.get("message", "").strip()
        if not message:
            return web.json_response({"error": "message is required"}, status=400)

        chat_id = data.get("chat_id", "fire_api")
        context_injection = data.get("context", "").strip()
        idempotency_key = data.get("idempotency_key", "").strip()
        result_channel = data.get("result_channel", "").strip()

        # Idempotency: return existing job if key matches
        if idempotency_key:
            for jid, rec in _fire_jobs.items():
                if rec.get("idempotency_key") == idempotency_key:
                    log.info("/fire idempotency hit: %s -> %s", idempotency_key, jid)
                    return web.json_response({"job_id": jid, "status": rec["status"]})

        job_id = _fire_job_id()
        _fire_jobs[job_id] = {
            "status": "pending",
            "response": None,
            "created_at": _time_module.time(),
            "idempotency_key": idempotency_key,
            "result_channel": result_channel,
        }

        # Count active (non-pending-queued) fire jobs
        active_count = sum(
            1 for rec in _fire_jobs.values()
            if rec["status"] == "pending" and rec.get("_task") is not None
        )
        queue_position = max(0, active_count - FIRE_CONCURRENCY_LIMIT + 1)

        # Deliver fn: store result in _fire_jobs and optionally post to Discord
        async def _fire_deliver(result: str, _jid: str = job_id, _rc: str = result_channel) -> None:
            if _jid in _fire_jobs:
                _fire_jobs[_jid]["status"] = "done"
                _fire_jobs[_jid]["response"] = result
            log.info("/fire job %s completed (%d chars)", _jid, len(result or ""))
            if _rc and discord_bot is not None:
                try:
                    # Post result to named Discord channel.
                    # send_to_named_channel uses discord.utils.get internally.
                    # We also try fetch_channel fallback for guild-cache timing issues.
                    if discord_bot._client:
                        sent_any = False
                        for guild in discord_bot._client.guilds:
                            sent = await discord_bot.send_to_named_channel(guild, _rc, result or "(empty)")
                            if sent:
                                sent_any = True
                        if not sent_any:
                            log.warning("/fire job %s: result_channel '#%s' not found on any guild", _jid, _rc)
                except Exception:
                    log.exception("/fire job %s: failed to post to result_channel '%s'", _jid, _rc)

        async def _fire_error(exc: Exception, _jid: str = job_id) -> None:
            if _jid in _fire_jobs:
                _fire_jobs[_jid]["status"] = "error"
                _fire_jobs[_jid]["response"] = str(exc)
            log.error("/fire job %s error: %s", _jid, exc)

        # Create and register the background task
        _bg = discord_bot._bg_tasks if discord_bot is not None else None
        if _bg is None:
            # No bg manager — create a standalone task + done callback
            reply_task = asyncio.create_task(
                agent.reply(
                    message=message,
                    chat_id=chat_id,
                    is_main_session=False,
                    context_injection=context_injection,
                ),
                name=f"fire_{job_id}",
            )

            def _done_cb(t: asyncio.Task, _jid: str = job_id) -> None:
                asyncio.create_task(_fire_done_cb(t, _jid))

            async def _fire_done_cb(t: asyncio.Task, _jid: str) -> None:
                if t.cancelled():
                    if _jid in _fire_jobs:
                        _fire_jobs[_jid]["status"] = "error"
                        _fire_jobs[_jid]["response"] = "cancelled"
                    return
                exc = t.exception()
                if exc is not None:
                    await _fire_error(exc, _jid=_jid)
                else:
                    await _fire_deliver(t.result() or "", _jid=_jid)

            reply_task.add_done_callback(_done_cb)
            _fire_jobs[job_id]["_task"] = True
        else:
            reply_task = asyncio.create_task(
                agent.reply(
                    message=message,
                    chat_id=chat_id,
                    is_main_session=False,
                    context_injection=context_injection,
                ),
                name=f"fire_{job_id}",
            )
            _bg.register(
                task=reply_task,
                deliver_fn=_fire_deliver,
                error_fn=_fire_error,
                channel_type="fire",
                channel_id=job_id,
                original_message=message,
                result_channel=result_channel,
            )
            _fire_jobs[job_id]["_task"] = True

        log.info("/fire queued job %s (active=%d, limit=%d)", job_id, active_count + 1, FIRE_CONCURRENCY_LIMIT)

        resp: dict = {"job_id": job_id, "status": "queued"}
        if queue_position > 0:
            resp["queue_position"] = queue_position
        return web.json_response(resp)

    # ── /result/{job_id} endpoint ─────────────────────────────────────────────

    async def handle_result(request: web.Request) -> web.Response:
        """GET /result/{job_id} — return status and response for a fire job."""
        job_id = request.match_info.get("job_id", "").strip()
        if not job_id:
            return web.json_response({"error": "job_id is required"}, status=400)

        rec = _fire_jobs.get(job_id)
        if rec is None:
            return web.json_response({"status": "not_found", "job_id": job_id}, status=404)

        result: dict = {"job_id": job_id, "status": rec["status"]}
        if rec["status"] in ("done", "error") and rec.get("response") is not None:
            result["response"] = rec["response"]
        return web.json_response(result)

    # ── /jobs endpoint ─────────────────────────────────────────────────────────

    async def handle_jobs(request: web.Request) -> web.Response:
        """GET /jobs — list all known fire jobs and their statuses."""
        jobs = []
        for jid, rec in _fire_jobs.items():
            entry: dict = {
                "job_id": jid,
                "status": rec["status"],
                "created_at": rec.get("created_at"),
            }
            if rec.get("idempotency_key"):
                entry["idempotency_key"] = rec["idempotency_key"]
            if rec.get("result_channel"):
                entry["result_channel"] = rec["result_channel"]
            if rec["status"] in ("done", "error") and rec.get("response") is not None:
                entry["response"] = rec["response"]
            jobs.append(entry)
        jobs.sort(key=lambda j: j.get("created_at") or 0)
        return web.json_response({"jobs": jobs, "total": len(jobs)})

    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_post("/ask", handle_ask)
    app.router.add_post("/fire", handle_fire)
    app.router.add_get("/result/{job_id}", handle_result)
    app.router.add_get("/jobs", handle_jobs)
    app.router.add_get("/history/{channel_name}", handle_history)
    app.router.add_post("/snapshot", handle_snapshot)
    return app


def load_crons(memory_dir: Path) -> list[dict]:
    """Load cron job definitions from crons.json."""
    crons_path = memory_dir / "crons.json"
    if not crons_path.exists():
        log.info("No crons.json found at %s — no cron jobs loaded", crons_path)
        return []
    try:
        data = json.loads(crons_path.read_text())
        if isinstance(data, list):
            return data
        log.warning("crons.json is not a list — ignoring")
        return []
    except (json.JSONDecodeError, OSError) as e:
        log.error("Failed to load crons.json: %s", e)
        return []


def setup_scheduler(
    agent: AgentRunner,
    crons: list[dict],
    default_tz: str,
    discord_bot: DiscordBot | None = None,
) -> AsyncIOScheduler:
    """Create and configure APScheduler with cron jobs."""
    scheduler = AsyncIOScheduler(
        job_defaults={
            "misfire_grace_time": 300,
            "coalesce": True,
        }
    )

    for job_def in crons:
        name = job_def.get("name", "unnamed")
        schedule = job_def.get("schedule")
        if not schedule:
            log.warning("Cron job '%s' has no schedule — skipping", name)
            continue

        tz = job_def.get("tz", default_tz)
        prompt = job_def.get("prompt", "")
        chat_id = job_def.get("chat_id", f"cron_{name}")
        is_main_session = job_def.get("is_main_session", False)
        notify_discord = job_def.get("notify_discord", False)
        # Optional: post to a named Discord channel (e.g. "morning-brief")
        discord_channel_name = job_def.get("discord_channel_name", "")

        # Parse cron expression: "min hour day month dow"
        parts = schedule.split()
        if len(parts) != 5:
            log.warning("Cron job '%s' has invalid schedule '%s' — skipping", name, schedule)
            continue

        trigger = CronTrigger(
            minute=parts[0],
            hour=parts[1],
            day=parts[2],
            month=parts[3],
            day_of_week=parts[4],
            timezone=tz,
        )

        async def cron_handler(
            _name=name,
            _prompt=prompt,
            _chat_id=chat_id,
            _is_main=is_main_session,
            _notify_discord=notify_discord,
            _discord_channel_name=discord_channel_name,
        ):
            log.info("Cron job '%s' firing", _name)
            try:
                _aname = agent.config.identity_dir.name if agent.config.identity_dir else "hollow"
                discord_channel_file = Path(f"/tmp/hollow_active_discord_{_aname}")
                discord_ts_file = Path(f"/tmp/hollow_active_discord_ts_{_aname}")

                saved_discord_channel = None
                saved_discord_ts = None

                # If this cron delivers to a specific Discord channel, suppress
                # send_msg from routing interim messages to the active #tarn channel.
                if _discord_channel_name:
                    if discord_channel_file.exists():
                        saved_discord_channel = discord_channel_file.read_text()
                        discord_channel_file.unlink()
                    if discord_ts_file.exists():
                        saved_discord_ts = discord_ts_file.read_text()
                        discord_ts_file.unlink()

                # Build reply task without wait_for — task is never cancelled on timeout.
                # Matches the discord channel pattern: wait up to 900s, then
                # hand off to BackgroundTaskManager for delivery when it finishes.
                reply_task = asyncio.create_task(
                    agent.reply(
                        message=_prompt,
                        chat_id=_chat_id,
                        is_main_session=_is_main,
                    )
                )

                async def _deliver_cron(result: str) -> None:
                    """Deliver cron result — same path whether on-time or background."""
                    if not result:
                        return
                    log.info("Cron job '%s' delivering (%d chars)", _name, len(result))
                    if discord_bot and discord_bot._client and (
                        _notify_discord or _discord_channel_name
                    ):
                        if _discord_channel_name:
                            sent_any = False
                            for guild in discord_bot._client.guilds:
                                sent = await discord_bot.send_to_named_channel(
                                    guild, _discord_channel_name, result
                                )
                                sent_any = sent_any or sent
                            if not sent_any:
                                log.warning(
                                    "Cron '%s': Discord channel '#%s' not found on any guild",
                                    _name, _discord_channel_name,
                                )


                try:
                    finished, _ = await asyncio.wait({reply_task}, timeout=900)
                finally:
                    # Restore active-channel markers regardless of outcome
                    if saved_discord_channel is not None:
                        discord_channel_file.write_text(saved_discord_channel)
                    if saved_discord_ts is not None:
                        discord_ts_file.write_text(saved_discord_ts)

                if reply_task in finished:
                    exc = reply_task.exception()
                    if exc is not None:
                        raise exc
                    response = reply_task.result()
                    log.info("Cron job '%s' completed (%d chars)", _name, len(response))
                    await _deliver_cron(response)
                else:
                    # Still running past 900s — hand off to BackgroundTaskManager.
                    _bg = discord_bot._bg_tasks if discord_bot is not None else None
                    if _bg is not None:
                        log.info(
                            "Cron job '%s' still running after 900s — handing off to BackgroundTaskManager",
                            _name,
                        )

                        async def _cron_error(exc: Exception) -> None:
                            log.error("Cron job '%s' background task raised: %s", _name, exc)

                        _bg.register(
                            task=reply_task,
                            deliver_fn=_deliver_cron,
                            error_fn=_cron_error,
                            channel_type="cron",
                            channel_id=_chat_id,
                            original_message=_prompt,
                        )
                    else:
                        log.warning(
                            "Cron job '%s' timed out after 900s and no BackgroundTaskManager "
                            "available — result will be lost",
                            _name,
                        )
            except Exception:
                log.exception("Cron job '%s' failed", _name)

        scheduler.add_job(cron_handler, trigger, id=name, name=name)
        log.info("Scheduled cron job '%s': %s (%s)", name, schedule, tz)

    return scheduler


def check_setup(config) -> list[str]:
    """Verify required files and config. Returns list of issues."""
    issues = []

    required_files = ["soul.md", "identity.md"]
    for f in required_files:
        if config.identity_dir:
            if not (config.identity_dir / f).exists():
                issues.append(f"Missing: {config.identity_dir / f}")
        elif not (config.memory_dir / f).exists():
            issues.append(f"Missing: {config.memory_dir / f}")

    return issues


async def _channel_already_responded(agent, chat_id: str, elapsed_seconds: float) -> bool:
    """Return True if the agent already sent an assistant reply in this channel
    within the last `elapsed_seconds + 120` seconds.

    Used during startup to detect channels where the user manually messaged
    Tarn before the startup notification loop got to them, causing Tarn to
    reply naturally — so we skip the automated startup context review to
    avoid sending a duplicate message.
    """
    try:
        import time as _time
        # Look back far enough to cover the time since startup began, plus a buffer.
        lookback = max(int(elapsed_seconds) + 120, 300)
        cutoff = _time.time() - lookback
        cursor = await agent.history._db.execute(
            "SELECT role FROM chat_sessions WHERE chat_id = ? AND role = 'assistant' AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
            (chat_id, cutoff),
        )
        row = await cursor.fetchone()
        return row is not None
    except Exception:
        log.debug("_channel_already_responded: could not check history for %s", chat_id, exc_info=True)
    return False


async def _recover_pending_tasks(config, agent, discord_bot) -> None:
    """Re-run any tasks that were in-flight when the process last died.

    Reads the pending_tasks SQLite table from each channel's BackgroundTaskManager,
    posts a recovery notice to each affected channel, then re-runs agent.reply()
    with the original message.  The result is delivered normally through the
    BackgroundTaskManager callback pipeline.
    """
    from src.channels.background_tasks import BackgroundTaskManager

    managers: list[BackgroundTaskManager] = []
    if discord_bot is not None:
        managers.append(discord_bot._bg_tasks)

    for mgr in managers:
        pending = await mgr.load_pending()
        if not pending:
            continue
        log.info("Recovering %d pending task(s) from previous run", len(pending))
        for record in pending:
            log.info(
                "Recovering task %s: channel_type=%s channel_id=%s",
                record.task_id, record.channel_type, record.channel_id,
            )

            # Build a delivery callback based on channel type
            _channel_type = record.channel_type
            _channel_id = record.channel_id
            _original_message = record.original_message
            _result_channel = record.result_channel  # non-empty for fire/api jobs with Discord delivery

            # Fire/api jobs: re-register in _fire_jobs so /result polling still works,
            # and deliver to Discord result_channel if configured.
            if _channel_type in ("fire", "api"):
                _job_id = _channel_id
                # Re-register the job in _fire_jobs so /result can track it
                if _job_id not in _fire_jobs:
                    _fire_jobs[_job_id] = {
                        "status": "pending",
                        "response": None,
                        "created_at": _time_module.time(),
                        "idempotency_key": "",
                        "result_channel": _result_channel,
                    }

                async def _deliver_recovery(
                    result: str, _jid=_job_id, _rc=_result_channel
                ) -> None:
                    if _jid in _fire_jobs:
                        _fire_jobs[_jid]["status"] = "done"
                        _fire_jobs[_jid]["response"] = result
                    log.info("Recovery: fire job %s completed (%d chars)", _jid, len(result or ""))
                    if _rc and discord_bot is not None:
                        try:
                            if discord_bot._client:
                                sent_any = False
                                for guild in discord_bot._client.guilds:
                                    sent = await discord_bot.send_to_named_channel(guild, _rc, result or "(empty)")
                                    if sent:
                                        sent_any = True
                                if not sent_any:
                                    log.warning("Recovery: result_channel '#%s' not found on any guild", _rc)
                        except Exception:
                            log.exception("Recovery: failed to post fire job %s to result_channel '%s'", _jid, _rc)

                async def _error_recovery(exc: Exception, _jid=_job_id) -> None:
                    if _jid in _fire_jobs:
                        _fire_jobs[_jid]["status"] = "error"
                        _fire_jobs[_jid]["response"] = str(exc)
                    log.error("Recovery: fire job %s error: %s", _jid, exc)

            else:
                async def _deliver_recovery(result: str, _ct=_channel_type, _cid=_channel_id) -> None:
                    if not result or not result.strip():
                        result = "Task complete — no output was returned."
                    if _ct == "discord" and discord_bot is not None:
                        try:
                            channel = discord_bot._client and discord_bot._client.get_channel(int(_cid))
                            if channel is not None:
                                await discord_bot._send_chunked(channel, result)
                            else:
                                log.warning("Recovery: Discord channel %s not found", _cid)
                        except Exception:
                            log.exception("Recovery: failed to deliver to Discord channel %s", _cid)

                async def _error_recovery(exc: Exception, _ct=_channel_type, _cid=_channel_id) -> None:
                    msg = f"⚠️ Recovered task failed: {exc}"
                    if _ct == "discord" and discord_bot is not None:
                        try:
                            channel = discord_bot._client and discord_bot._client.get_channel(int(_cid))
                            if channel is not None:
                                await channel.send(msg)
                        except Exception:
                            pass

            # Post a recovery notice before re-running the task
            # (only for user-facing channels, not fire/api jobs)
            notice = "⚠️ I restarted while working on your request. Picking up where I left off..."
            try:
                if _channel_type == "discord" and discord_bot is not None:
                    channel = discord_bot._client and discord_bot._client.get_channel(int(_channel_id))
                    if channel is not None:
                        await channel.send(notice)
            except Exception:
                log.exception("Recovery: failed to send notice to %s/%s", _channel_type, _channel_id)

            # Remove the old record and re-run the task via BackgroundTaskManager
            # so it gets a fresh task_id and a new SQLite row.  The old row will
            # be cleaned up by _delete_pending during the next register() → done cycle
            # or on the next clean startup when load_pending() finds nothing.
            reply_task = asyncio.create_task(
                agent.reply(
                    message=_original_message,
                    chat_id=_channel_id,
                    is_main_session=False,
                )
            )
            mgr.register(
                task=reply_task,
                deliver_fn=_deliver_recovery,
                error_fn=_error_recovery,
                channel_type=_channel_type,
                channel_id=_channel_id,
                original_message=_original_message,
            )
            # Remove the old stale record now that we've re-registered
            await mgr._delete_pending(record.task_id)


async def _load_recent_conversation(agent, chat_id: str, n_turns: int = 15) -> str:
    """Load the last n_turns from the SQLite conversation history for a channel.

    Returns a formatted string suitable for injection into the startup prompt,
    or an empty string if no history is available.

    A "turn" is a user+assistant pair; we fetch up to 2*n_turns messages.
    """
    try:
        limit = n_turns * 2
        cursor = await agent.history._db.execute(
            "SELECT role, content FROM chat_sessions "
            "WHERE chat_id = ? AND session_key = '' "
            "ORDER BY created_at DESC LIMIT ?",
            (chat_id, limit),
        )
        rows = await cursor.fetchall()
        if not rows:
            return ""
        # Reverse to get chronological order (oldest first)
        rows = list(reversed(rows))
        parts = []
        for role, content in rows:
            display = "Tyler" if role == "user" else "Tarn"
            # Truncate very long messages to keep the prompt manageable
            snippet = content[:800] + "..." if len(content) > 800 else content
            parts.append(f"{display}: {snippet}")
        return "\n".join(parts)
    except Exception:
        log.debug("_load_recent_conversation: failed for chat_id=%s", chat_id, exc_info=True)
        return ""


def _format_snapshot_context(snapshot: dict | None) -> str:
    """Format snapshot data into a compact context string for the startup prompt."""
    if snapshot is None:
        return ""

    import time as _time

    parts: list[str] = []
    age_secs = _time.time() - snapshot.get("saved_at", 0)

    bg_tasks = snapshot.get("bg_tasks", [])
    if bg_tasks:
        task_lines = []
        for t in bg_tasks:
            started = t.get("started_at", 0)
            elapsed_min = int((_time.time() - started) / 60) if started else 0
            msg = t.get("original_message", "")[:120]
            task_lines.append(
                f"  - [{t.get('channel_type','?')}/{t.get('channel_id','?')}] "
                f"started {elapsed_min}m ago — {msg!r}"
            )
        parts.append("Background tasks that were in-flight at shutdown:\n" + "\n".join(task_lines))

    cron_jobs = snapshot.get("cron_jobs", [])
    if cron_jobs:
        cron_lines = [f"  - {j.get('name','?')} (next: {j.get('next_run_time') or 'unknown'})" for j in cron_jobs]
        parts.append("Cron jobs at shutdown:\n" + "\n".join(cron_lines))

    if not parts:
        return ""

    return (
        f"[Snapshot from {int(age_secs)}s before restart]\n"
        + "\n".join(parts)
    )


async def _send_startup_notification(config, agent, discord_bot) -> None:
    """On restart: review recent context per-channel and proactively continue work.

    Phase 2: injects last 15 conversation turns from SQLite as the primary
    handoff context.  MC board is NOT consulted.

    Phase 3: reads /tmp/<agent>_restart_origin_channel to determine routing:
      - If origin channel is set (agent triggered the restart): send full context
        review ONLY to that channel.  All others get nothing.
      - If not set (watchdog crash): send to all tarn channels + Telegram.
    """
    import os

    # Maintenance restarts are quiet — skip full re-orientation to avoid noise.
    restart_reason = os.environ.get("TARN_RESTART_REASON", "").strip().lower()
    is_maintenance = restart_reason == "maintenance"

    # Startup is a read-only context review — block Bash so the agent cannot
    # execute shell commands (restart-tarn, kill, pkill, etc.) while booting.
    from src.agent import NATIVE_TOOLS
    STARTUP_ALLOWED_TOOLS = [t for t in NATIVE_TOOLS if t in ("Read", "Glob", "Grep")]

    # ── Phase 1: read pre-kill snapshot ───────────────────────────────────────
    snapshot = read_snapshot(config.data_dir)
    snapshot_context = _format_snapshot_context(snapshot)
    if snapshot_context:
        log.info("Startup: loaded pre-kill snapshot (%d bg tasks, %d cron jobs)",
                 len(snapshot.get("bg_tasks", [])), len(snapshot.get("cron_jobs", [])))
    else:
        log.info("Startup: no usable snapshot found")

    # ── Phase 3: origin-channel routing ───────────────────────────────────────
    _agent_name = config.identity_dir.name if config.identity_dir else "hollow"
    _origin_channel_file = Path(f"/tmp/{_agent_name}_restart_origin_channel")
    origin_channel_id: str | None = None
    try:
        if _origin_channel_file.exists():
            origin_channel_id = _origin_channel_file.read_text().strip() or None
            _origin_channel_file.unlink(missing_ok=True)
            if origin_channel_id:
                log.info("Startup: origin-channel routing to channel %s", origin_channel_id)
    except Exception:
        log.debug("Startup: failed to read origin-channel file", exc_info=True)

    # Wait for Discord to fully connect before sending.
    if discord_bot:
        try:
            await asyncio.wait_for(discord_bot._ready_event.wait(), timeout=30)
            await asyncio.sleep(2)
        except asyncio.TimeoutError:
            log.warning("Discord ready_event timed out after 30s — proceeding with startup notification anyway")

        # Write active channel file before context review so any tool calls go to the right place
        _tarn_channel_id = str(origin_channel_id or config.discord_tarn_channel_id or "")
        if discord_bot and _tarn_channel_id:
            try:
                discord_bot._write_active_channel(_tarn_channel_id)
            except Exception:
                log.debug("Startup: failed to pre-set active channel", exc_info=True)
    else:
        await asyncio.sleep(8)

    # Maintenance restart: post a minimal one-liner to Discord #tarn only.
    if is_maintenance:
        log.info("Maintenance restart detected — sending minimal notification to Discord only")
        if discord_bot and discord_bot._client:
            for discord_chat_id, tarn_channel in discord_bot.get_tarn_chat_ids():
                try:
                    await discord_bot._send_chunked(
                        tarn_channel,
                        "Maintenance restart complete. Crons reloaded.",
                    )
                except Exception:
                    log.exception("Failed to send maintenance restart notification to Discord channel %s", discord_chat_id)
        return

    ping = "I'm back — reviewing context and picking up where we left off..."

    def _build_startup_prompt(recent_history: str, bg_context: str) -> str:
        """Build the startup prompt with conversation history + snapshot, no MC board."""
        parts = [
            "I just restarted. Below is the recent conversation history for this channel "
            "and any background tasks that were in-flight when I shut down. "
            "Use this to pick up exactly where we left off — conversational thread first, "
            "background tasks second.",
            "",
        ]

        if recent_history:
            parts.append("Recent conversation in this channel (oldest first):")
            parts.append(recent_history)
            parts.append("")

        if bg_context:
            parts.append(bg_context)
            parts.append("")

        parts.extend([
            "Based on the above:",
            "1. Send a brief message (1-3 sentences) picking up the conversational thread "
            "and naming the next concrete step",
            "2. If there was active background work, mention its status",
            "3. If nothing was actively in progress, just confirm you're up",
            "",
            "Be specific and terse. Tyler should not have to re-prompt you.",
            "",
            "CRITICAL: Do NOT run restart-agent or any self-restart commands during this "
            "startup sequence. You literally just restarted — calling restart-agent now would "
            "create an infinite restart loop. If the previous conversation was about restart "
            "issues, summarize the status only.",
        ])
        return "\n".join(parts)

    async def _do_channel_review(chat_id: str, send_fn) -> None:
        """Run the context review for a single channel and deliver the result."""
        # If there's already a recent assistant reply, skip to avoid doubling.
        if await _channel_already_responded(agent, chat_id, elapsed_seconds=300):
            log.info("Startup: channel %s already has a recent reply — skipping", chat_id)
            return

        recent_history = await _load_recent_conversation(agent, chat_id)
        prompt = _build_startup_prompt(recent_history, snapshot_context)

        try:
            response = await asyncio.wait_for(
                agent.reply(
                    message=prompt,
                    chat_id=chat_id,
                    is_main_session=True,
                    allowed_tools=STARTUP_ALLOWED_TOOLS,
                ),
                timeout=120,
            )
            if response and response.strip():
                await send_fn(response)
        except asyncio.TimeoutError:
            log.warning("Startup context review timed out after 120s for channel %s", chat_id)
            await send_fn("I'm back — ready to continue. What are we working on?")
        except Exception:
            log.exception("Failed to run startup context review for channel %s", chat_id)
            await send_fn("I'm back — ready to continue. What are we working on?")

    # ── Origin-channel routing decision ───────────────────────────────────────
    # If origin_channel_id is set: the agent itself triggered the restart from a
    # specific Discord channel.  Send the full review ONLY there.  Skip Telegram
    # and all other channels.
    #
    # If origin_channel_id is NOT set: watchdog or unexpected crash.  Send to
    # Telegram (if configured) AND all tarn Discord channels.

    if origin_channel_id:
        # Targeted restart from a known channel — send only there
        log.info("Startup: targeted restart — sending context review to origin channel %s only", origin_channel_id)
        if discord_bot and discord_bot._client:
            try:
                channel = discord_bot._client.get_channel(int(origin_channel_id))
                if channel is None:
                    channel = await discord_bot._client.fetch_channel(int(origin_channel_id))
            except Exception:
                channel = None

            if channel is not None:
                try:
                    await discord_bot._send_chunked(channel, ping)
                except Exception:
                    log.exception("Failed to send startup ping to origin channel %s", origin_channel_id)

                async def _discord_origin_send(text: str) -> None:
                    await discord_bot._send_chunked(channel, text)

                await _do_channel_review(origin_channel_id, _discord_origin_send)
            else:
                log.warning("Startup: origin channel %s not found — falling back to all tarn channels", origin_channel_id)
                # Fall through to broadcast below
                origin_channel_id = None

    if not origin_channel_id:
        # Watchdog or unexpected crash — broadcast to all tarn Discord channels

        if discord_bot and discord_bot._client:
            tarn_channels = discord_bot.get_tarn_chat_ids()
            if not tarn_channels and config.discord_tarn_channel_id:
                log.warning("Startup: get_tarn_chat_ids() returned empty — using DISCORD_TARN_CHANNEL_ID fallback")
                try:
                    _fb_ch = await discord_bot._client.fetch_channel(config.discord_tarn_channel_id)
                    tarn_channels = [(str(config.discord_tarn_channel_id), _fb_ch)]
                except Exception:
                    log.warning("Startup: hardcoded fallback fetch also failed", exc_info=True)
            for discord_chat_id, tarn_channel in tarn_channels:
                try:
                    await discord_bot._send_chunked(tarn_channel, ping)
                except Exception:
                    log.exception("Failed to send Discord startup ping to channel %s", discord_chat_id)

                async def _discord_tarn_send(text: str, _ch=tarn_channel) -> None:
                    try:
                        await discord_bot._send_chunked(_ch, text)
                    except Exception:
                        log.exception("Failed to deliver Discord startup response to channel %s", discord_chat_id)

                await _do_channel_review(discord_chat_id, _discord_tarn_send)


async def run(args: argparse.Namespace = None):
    """Main async entry point — HTTP API + Discord bot + APScheduler."""
    # Resolve the agent-specific .env path early so load_config() can ingest
    # it before constructing the Config object (avoids post-hoc field patching).
    env_path = None
    if args and args.identity_dir is not None:
        candidate = args.identity_dir.resolve() / ".env"
        if candidate.exists():
            env_path = candidate
    config = load_config(env_path=env_path)
    if args:
        apply_overrides(config, args)

    # Ensure data_dir exists
    config.data_dir.mkdir(parents=True, exist_ok=True)

    print(f"Hollow v0.1.0")
    print(f"  Identity: {config.identity_dir or 'default'}")
    print(f"  Memory:   {config.memory_dir}")
    print(f"  Data:     {config.data_dir}")
    print(f"  Model:    {config.primary_model}")
    print(f"  Port:     {config.api_port}")
    print()

    issues = check_setup(config)
    if issues:
        print(f"{len(issues)} issue(s):")
        for issue in issues:
            print(f"  - {issue}")
        if any("Missing" in i for i in issues):
            print("\nWarning: some identity files missing. Context will be incomplete.")

    # Initialize memory
    memory = MemoryManager(config)
    await memory.initialize()

    # Initialize agent
    agent = AgentRunner(config, memory)
    await agent.initialize()

    # Initialize Discord bot (if token available)
    discord_bot = None
    if config.discord_bot_token:
        discord_bot = DiscordBot(config, agent)

    # Load and set up cron scheduler
    crons = load_crons(config.memory_dir)
    scheduler = setup_scheduler(agent, crons, config.user_timezone, discord_bot=discord_bot)

    # Set up HTTP API
    api_app = make_api_app(agent, discord_bot=discord_bot)
    # Store scheduler reference so /snapshot handler can read cron state
    api_app["scheduler"] = scheduler
    api_runner = web.AppRunner(api_app)
    await api_runner.setup()
    _port_retries = 20
    for _attempt in range(1, _port_retries + 1):
        try:
            # Re-create TCPSite each attempt: aiohttp registers the site in the
            # runner before the actual TCP bind, so a failed start() leaves a
            # stale registration that would raise RuntimeError on retry.
            site = web.TCPSite(api_runner, config.api_host, config.api_port)
            await site.start()
            break
        except OSError as _e:
            if _attempt == _port_retries:
                raise
            log.warning(
                "Port %d not yet free (%s), retrying %d/%d...",
                config.api_port, _e, _attempt, _port_retries,
            )
            # Un-register the failed site so the next attempt can create a fresh one
            try:
                api_runner._sites.remove(site)  # type: ignore[attr-defined]
            except (ValueError, AttributeError):
                pass
            await asyncio.sleep(1.5)
    log.info("HTTP API listening on http://%s:%d", config.api_host, config.api_port)

    stop_event = asyncio.Event()

    def signal_handler():
        log.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    discord_task = None
    try:
        # Start services
        if discord_bot:
            # discord.py's start() runs until close() is called — run as background task
            discord_task = asyncio.create_task(discord_bot.start(token=config.discord_bot_token))
            log.info("Discord bot starting (background task)")

        scheduler.start()
        log.info("APScheduler started with %d job(s)", len(scheduler.get_jobs()))

        # Recover any tasks that were in-flight when the process last died.
        # Runs unconditionally (independent of startup_notification) so long-running
        # tasks are always recovered even on agents with STARTUP_NOTIFICATION=false.
        asyncio.create_task(
            _recover_pending_tasks(config, agent, discord_bot)
        )

        # Send startup notification and proactively pick up where we left off.
        # Agents can set STARTUP_NOTIFICATION=false to suppress this (e.g. Flux,
        # whose #trader-bot channel is a conversation channel, not a monitoring
        # channel, and should not receive unsolicited restart announcements).
        if config.startup_notification:
            asyncio.create_task(
                _send_startup_notification(config, agent, discord_bot)
            )

        print("Hollow is running. Press Ctrl+C to stop.")
        await stop_event.wait()
    finally:
        log.info("Shutting down...")
        scheduler.shutdown(wait=False)
        if discord_bot:
            await discord_bot._bg_tasks.shutdown()
            await discord_bot.stop()
            if discord_task and not discord_task.done():
                discord_task.cancel()
                try:
                    await discord_task
                except (asyncio.CancelledError, Exception):
                    pass
        await api_runner.cleanup()
        await agent.history.close()
        await memory.close()
        # Clean up stale active-channel temp files so they don't mis-route
        # messages after a restart.
        _aname = config.identity_dir.name if config.identity_dir else "hollow"
        for _tmp_file in (
            Path(f"/tmp/hollow_active_discord_{_aname}"),
            Path(f"/tmp/hollow_active_discord_ts_{_aname}"),
        ):
            try:
                _tmp_file.unlink(missing_ok=True)
            except Exception:
                log.debug("Failed to remove %s on shutdown (non-fatal)", _tmp_file, exc_info=True)
        log.info("Shutdown complete.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hollow — AI agent runtime")
    parser.add_argument("--port", type=int, default=None, help="HTTP API port")
    parser.add_argument("--identity-dir", type=Path, default=None, help="Path to soul.md + identity.md")
    parser.add_argument("--memory-dir", type=Path, default=None, help="Path to memory directory")
    parser.add_argument("--data-dir", type=Path, default=None, help="Path to data directory (SQLite, audit log)")
    return parser.parse_args()


def apply_overrides(config, args: argparse.Namespace) -> None:
    if args.port is not None:
        config.api_port = args.port
    if args.identity_dir is not None:
        config.identity_dir = args.identity_dir.resolve()
        # Agent-specific .env was already loaded by load_config(env_path=...)
        # before the Config object was constructed — no field patching needed.
    if args.memory_dir is not None:
        config.memory_dir = args.memory_dir.resolve()
    if args.data_dir is not None:
        config.data_dir = args.data_dir.resolve()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
