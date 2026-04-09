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


def _resolve_allowed_tools(
    payload_tools: object,
    agent: "AgentRunner",
) -> "list[str] | None":
    """Resolve the effective allowed_tools list for a request.

    Priority (highest to lowest):
      1. payload_tools  — list passed in the HTTP request body (hail --allowed-tools)
      2. agent.config.allowed_tools — loaded from ALLOWED_TOOLS env var at startup
      3. None — means agent.reply() uses its NATIVE_TOOLS default

    A payload value of ["all"] or an empty config list both mean "no restriction".
    Tool names in the payload are expected to be SDK-style (capitalized, e.g. "Bash").
    """
    # 1. Payload override
    if payload_tools is not None:
        if isinstance(payload_tools, list):
            if payload_tools and payload_tools != ["all"]:
                return [str(t) for t in payload_tools]
        # payload present but empty or ["all"] → unrestricted
        return None

    # 2. Agent config
    cfg_tools = getattr(agent.config, "allowed_tools", [])
    if cfg_tools:
        return cfg_tools  # already parsed SDK-style names

    # 3. Default: no restriction
    return None


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
    # Index 3: True while a background refresh task is running (prevents duplicates).
    _claude_probe_cache: list = [None, "", 0.0, False]
    _CLAUDE_PROBE_TTL = 300

    async def _run_probe_background() -> None:
        """Execute the Claude API probe and update the cache.

        Always runs as a fire-and-forget asyncio.Task so it never blocks
        /health.  The in-flight flag (index 3) prevents duplicate tasks.
        """
        _claude_probe_cache[3] = True
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
        _claude_probe_cache[3] = False

    def _get_cached_claude_status() -> tuple[bool, str]:
        """Return the most recent cached Claude probe result immediately (no I/O).

        If the cache is stale (or empty), kick off a background refresh task so
        the *next* /health call will have a fresh value.  This means /health
        always returns in O(1) — it is never blocked by an SDK round-trip.

        On first call (cache empty), we optimistically return ok=True with a
        "probe_pending" reason so the endpoint doesn't immediately report
        degraded before the first probe completes.
        """
        now = _time.time()
        cache_age = now - _claude_probe_cache[2]
        in_flight = _claude_probe_cache[3]

        # Kick off a background refresh if stale and not already running
        if not in_flight and (
            _claude_probe_cache[0] is None or cache_age >= _CLAUDE_PROBE_TTL
        ):
            asyncio.create_task(
                _run_probe_background(),
                name="claude_api_probe",
            )

        # Return cached value (optimistic on first call before probe completes)
        if _claude_probe_cache[0] is None:
            return True, "probe_pending"
        return _claude_probe_cache[0], _claude_probe_cache[1]

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

        # Read the cached Claude probe result — never awaits the SDK.
        # A background task refreshes the cache when it goes stale.
        claude_ok, claude_reason = _get_cached_claude_status()

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
        model_override = data.get("model", "").strip()
        # allowed_tools: payload override > agent config > NATIVE_TOOLS default
        allowed_tools = _resolve_allowed_tools(data.get("allowed_tools"), agent)

        try:
            response = await agent.reply(
                message=message,
                chat_id=chat_id,
                is_main_session=is_main_session,
                context_injection=context_injection,
                model_override=model_override,
                allowed_tools=allowed_tools,
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
        model_override = data.get("model", "").strip()
        # allowed_tools: payload override > agent config > NATIVE_TOOLS default
        allowed_tools = _resolve_allowed_tools(data.get("allowed_tools"), agent)

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
                    model_override=model_override,
                    allowed_tools=allowed_tools,
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
                    model_override=model_override,
                    allowed_tools=allowed_tools,
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


async def _ollama_chat(model: str, prompt: str, timeout: float = 60.0) -> str:
    """Call the local Ollama HTTP API and return the response text.

    Uses http://localhost:11434/api/generate with stream=False.
    Returns empty string on error so callers can degrade gracefully.
    """
    import json as _json
    try:
        import aiohttp as _aiohttp
        payload = {"model": model, "prompt": prompt, "stream": False}
        async with _aiohttp.ClientSession() as session:
            async with session.post(
                "http://localhost:11434/api/generate",
                json=payload,
                timeout=_aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    log.warning("Ollama returned HTTP %d for model %s", resp.status, model)
                    return ""
                data = await resp.json(content_type=None)
                return data.get("response", "").strip()
    except Exception as exc:
        log.warning("_ollama_chat failed (%s): %s", model, exc)
        return ""


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

        # Skip disabled crons
        if not job_def.get("enabled", True):
            log.info("Cron job '%s' is disabled — skipping", name)
            continue

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

        # ── Pure-script cron (no LLM at all) ─────────────────────────────────
        # If type == "script", run the script_command field directly as a shell
        # command.  Falls back to prompt if script_command is absent.
        # No LLM session is spun up.
        _cron_type = job_def.get("type", "")
        if _cron_type == "script":
            _script_cmd = job_def.get("script_command", "") or prompt
            _gate_cmd = job_def.get("gate_command", "")

            async def cron_handler(  # type: ignore[misc]
                _name=name,
                _script=_script_cmd,
                _gate=_gate_cmd,
                _discord_channel_name=discord_channel_name,
            ):
                """Script cron: runs a shell command directly, no LLM."""
                import subprocess as _sp
                log.info("Cron job '%s' firing (script mode)", _name)
                try:
                    # Optional gate: run a quick check before the main script.
                    # If gate exits non-zero, skip the main script.
                    if _gate:
                        gate_result = _sp.run(
                            _gate, shell=True, capture_output=True, text=True, timeout=30
                        )
                        if gate_result.returncode != 0:
                            log.info(
                                "Cron job '%s' gate check failed (rc=%d) — skipping: %s",
                                _name, gate_result.returncode, gate_result.stdout.strip()[:200],
                            )
                            return
                    result = _sp.run(
                        _script, shell=True, capture_output=True, text=True, timeout=600
                    )
                    output = (result.stdout + result.stderr).strip()
                    log.info(
                        "Cron job '%s' (script) exited rc=%d: %s",
                        _name, result.returncode, output[:500],
                    )
                    # Optionally post output to a named Discord channel
                    if discord_bot and discord_bot._client and _discord_channel_name and output:
                        try:
                            sent_any = False
                            for guild in discord_bot._client.guilds:
                                sent = await discord_bot.send_to_named_channel(
                                    guild, _discord_channel_name, output
                                )
                                sent_any = sent_any or sent
                            if not sent_any:
                                log.warning(
                                    "Cron '%s': Discord channel '#%s' not found on any guild",
                                    _name, _discord_channel_name,
                                )
                        except Exception:
                            log.exception("Cron '%s': failed to post script output to Discord", _name)
                except Exception:
                    log.exception("Script cron job '%s' failed", _name)

            scheduler.add_job(cron_handler, trigger, id=name, name=name)
            log.info("Scheduled SCRIPT cron job '%s': %s (%s)", name, schedule, tz)
            continue

        # ── Local-model cron (no Claude API) ──────────────────────────────────
        # If model starts with "local:" or prompt == "HEARTBEAT_LOCAL_OLLAMA",
        # use Ollama directly instead of the Claude SDK.
        _cron_model = job_def.get("model", "")
        _use_local_model = _cron_model.startswith("local:") or prompt == "HEARTBEAT_LOCAL_OLLAMA"
        _local_ollama_model = _cron_model.replace("local:", "") if _cron_model.startswith("local:") else "gemma4:e4b"

        if _use_local_model:
            async def cron_handler(  # type: ignore[misc]
                _name=name,
                _chat_id=chat_id,
                _ollama_model=_local_ollama_model,
                _discord_channel_name=discord_channel_name,
            ):
                """Heartbeat cron: uses local Ollama model, no Claude API."""
                log.info("Cron job '%s' firing (local Ollama: %s)", _name, _ollama_model)
                try:
                    # Build a minimal task-state prompt for the heartbeat check
                    import subprocess as _sp
                    task_summary = ""
                    try:
                        result = _sp.run(
                            ["python3", "-c",
                             "import subprocess, json; "
                             "r = subprocess.run(['curl','-s','http://localhost:3333/api/tasks?status=in_progress',"
                             "'-H','x-api-key: 462f220e3c3f15324825a86373da874f92302bf6fc646e3330731468cc959c22'],"
                             "capture_output=True,text=True); "
                             "tasks=json.loads(r.stdout) if r.stdout else []; "
                             "print(', '.join(t.get('title','?')[:60] for t in tasks[:5]) or 'none')"],
                            capture_output=True, text=True, timeout=10,
                        )
                        task_summary = result.stdout.strip()
                    except Exception:
                        task_summary = "unknown"

                    heartbeat_prompt = (
                        f"You are Tarn, an AI agent running on a server. "
                        f"Current in-progress tasks: {task_summary or 'none'}. "
                        f"In one sentence, confirm the system is running normally or flag any concern."
                    )

                    response = await _ollama_chat(_ollama_model, heartbeat_prompt, timeout=30.0)
                    if not response:
                        response = "Heartbeat: system running (Ollama unavailable for status check)."

                    log.info("Heartbeat cron result: %s", response[:200])

                    # Post to Discord #tarn if configured
                    if discord_bot and discord_bot._client and _discord_channel_name:
                        try:
                            from src.channels.discord_channel import DiscordBot as _DB
                            chan = await discord_bot._get_channel_by_name(_discord_channel_name)
                            if chan:
                                await discord_bot._send_chunked(chan, f"[heartbeat] {response}")
                        except Exception:
                            log.debug("Heartbeat: failed to post to Discord #%s", _discord_channel_name, exc_info=True)
                except Exception:
                    log.exception("Heartbeat cron '%s' failed", _name)

            scheduler.add_job(cron_handler, trigger, id=name, name=name)
            log.info("Scheduled LOCAL-MODEL cron job '%s': %s (%s) [%s]", name, schedule, tz, _local_ollama_model)
            continue

        _gate_command = job_def.get("gate_command", "")

        async def cron_handler(
            _name=name,
            _prompt=prompt,
            _chat_id=chat_id,
            _is_main=is_main_session,
            _notify_discord=notify_discord,
            _discord_channel_name=discord_channel_name,
            _gate=_gate_command,
        ):
            log.info("Cron job '%s' firing", _name)
            try:
                # ── Gate check (optional Gemma4/shell precheck) ───────────────
                if _gate:
                    import subprocess as _sp
                    gate_result = _sp.run(
                        _gate, shell=True, capture_output=True, text=True, timeout=60
                    )
                    if gate_result.returncode != 0:
                        log.info(
                            "Cron job '%s' gate check failed (rc=%d) — skipping LLM call: %s",
                            _name, gate_result.returncode, gate_result.stdout.strip()[:200],
                        )
                        return
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

    if not parts:
        return ""

    return (
        f"[Snapshot from {int(age_secs)}s before restart]\n"
        + "\n".join(parts)
    )


async def _detect_unanswered_message(agent, chat_id: str) -> str | None:
    """Check if Tyler's last message was not substantively answered.

    Returns the unanswered message text if Tyler's last message was followed
    only by startup/I'm-back type messages (or no reply at all). Returns None
    if the last message was properly answered or no history exists.
    """
    try:
        cursor = await agent.history._db.execute(
            "SELECT role, content FROM chat_sessions "
            "WHERE chat_id = ? AND session_key = '' "
            "ORDER BY created_at DESC LIMIT 10",
            (chat_id,),
        )
        rows = await cursor.fetchall()
        if not rows:
            return None

        # Reverse to chronological order (oldest first)
        rows = list(reversed(rows))

        # Find the last user message index
        last_user_idx = None
        for i, (role, _content) in enumerate(rows):
            if role == "user":
                last_user_idx = i

        if last_user_idx is None:
            return None

        last_user_msg = rows[last_user_idx][1].strip()

        # Skip trivial messages (too short to be meaningful)
        if len(last_user_msg) < 10:
            return None

        # Check what assistant messages followed the last user message
        subsequent_assistant = [
            content for role, content in rows[last_user_idx + 1:]
            if role == "assistant"
        ]

        if not subsequent_assistant:
            # No reply at all — definitely unanswered
            return last_user_msg

        # Check if all subsequent assistant messages look like startup/I'm-back messages
        STARTUP_PHRASES = (
            "back up.", "back up,", "i'm back", "maintenance restart", "restart complete",
            "back online", "restarted.", "ready to continue", "back — ready",
            "picking up where", "back and ready", "🔄", "back.",
        )
        all_startup = all(
            any(phrase in msg.lower() for phrase in STARTUP_PHRASES)
            for msg in subsequent_assistant
        )

        return last_user_msg if all_startup else None
    except Exception:
        log.debug("_detect_unanswered_message failed for %s", chat_id, exc_info=True)
        return None


def _is_restart_request(msg: str) -> bool:
    """Return True if msg appears to be asking Tarn to restart itself."""
    lower = msg.lower()
    return any(kw in lower for kw in (
        "restart yourself", "restart tarn", "restart the bot", "restart-tarn",
        "restart-agent", "reboot yourself", "bin/restart",
    ))


def _has_pending_restart_request(config, since_ts: float) -> bool:
    """Return True if pending-restart.md exists and was modified after since_ts.

    This is the guard against restart loops: if Tyler's unanswered message was
    a restart request, we only re-queue it if there's an explicit pending-restart.md
    file with a timestamp newer than the current restart.
    """
    try:
        _pending = config.memory_dir / "pending-restart.md"
        if _pending.exists():
            return _pending.stat().st_mtime > since_ts
    except Exception:
        pass
    return False


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

    # ── Read restart reason from /tmp/<agent>_restart_reason ─────────────────
    # Written by bin/restart-agent before killing the old process.
    # Delete after reading so it doesn't persist across future restarts.
    _agent_name_for_reason = config.identity_dir.name if config.identity_dir else "tarn"
    _restart_reason_file = Path(f"/tmp/{_agent_name_for_reason}_restart_reason")
    _restart_reason_text: str | None = None
    try:
        if _restart_reason_file.exists():
            _restart_reason_text = _restart_reason_file.read_text().strip() or None
            _restart_reason_file.unlink(missing_ok=True)
            if _restart_reason_text:
                log.info("Startup: restart reason from file: %s", _restart_reason_text)
    except Exception:
        log.debug("Startup: failed to read restart reason file", exc_info=True)

    # Fallback: if no /tmp reason file, read last entry from hollow-restart-log.jsonl
    if not _restart_reason_text:
        try:
            _restart_log = config.data_dir.parent / "hollow-restart-log.jsonl"
            if _restart_log.exists():
                import json as _json
                _lines = _restart_log.read_text().strip().splitlines()
                if _lines:
                    _entry = _json.loads(_lines[-1])
                    _restart_reason_text = (
                        f"{_entry.get('trigger', '?')}: {_entry.get('detail', 'unknown')} "
                        f"(logged {_entry.get('timestamp', '?')})"
                    )
                    log.info("Startup: restart reason from JSONL log: %s", _restart_reason_text)
        except Exception:
            log.debug("Startup: failed to read hollow-restart-log.jsonl", exc_info=True)

    # Startup is a read-only context review — block Bash so the agent cannot
    # execute shell commands (restart-tarn, kill, pkill, etc.) while booting.
    from src.agent import NATIVE_TOOLS
    STARTUP_ALLOWED_TOOLS = [t for t in NATIVE_TOOLS if t in ("Read", "Glob", "Grep")]

    # ── Phase 1: read pre-kill snapshot ───────────────────────────────────────
    snapshot = read_snapshot(config.data_dir)
    snapshot_context = _format_snapshot_context(snapshot)
    if snapshot_context:
        log.info("Startup: loaded pre-kill snapshot (%d bg tasks)",
                 len(snapshot.get("bg_tasks", [])))
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
        maint_msg = "Maintenance restart complete."
        if _restart_reason_text and "maintenance" not in _restart_reason_text.lower():
            maint_msg += f" Reason: {_restart_reason_text}"
        if discord_bot and discord_bot._client:
            for discord_chat_id, tarn_channel in discord_bot.get_tarn_chat_ids():
                try:
                    await discord_bot._send_chunked(tarn_channel, maint_msg)
                except Exception:
                    log.exception("Failed to send maintenance restart notification to Discord channel %s", discord_chat_id)
        return

    # Build startup ping message with restart reason disclosure
    if _restart_reason_text:
        ping = f"Back up. Restart triggered by: {_restart_reason_text}"
    else:
        ping = "Back up. Restart reason unknown — check data/hollow-restart-log.jsonl"

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
        """Run the context review for a single channel using local Ollama (lean mode).

        Uses gemma4:e4b via Ollama instead of Claude API to avoid token burn on restart.
        Falls back to a static "I'm back" message if Ollama is unavailable.
        Detects unanswered Tyler messages and queues them for actual agent processing.
        """
        import time as _time

        # If there's already a recent assistant reply, skip to avoid doubling.
        if await _channel_already_responded(agent, chat_id, elapsed_seconds=300):
            log.info("Startup: channel %s already has a recent reply — skipping", chat_id)
            return

        # Detect if Tyler's last message went unanswered before the restart
        unanswered_msg = await _detect_unanswered_message(agent, chat_id)
        if unanswered_msg:
            log.info(
                "Startup: channel %s has unanswered message (%.60s...)",
                chat_id, unanswered_msg,
            )

        recent_history = await _load_recent_conversation(agent, chat_id)
        prompt = _build_startup_prompt(recent_history, snapshot_context)

        # Lean-mode: use local Ollama instead of Claude API to save tokens.
        ollama_model = "gemma4:e4b"
        lean_prompt = (
            "You are reviewing recent conversation messages to determine what work is currently most important/active.\n\n"
            "Here are the recent messages (most recent last):\n"
        )
        if recent_history:
            lean_prompt += recent_history + "\n\n"
        if unanswered_msg:
            lean_prompt += (
                f"NOTE: The following user message was sent right before the restart and "
                f"has not been substantively answered yet: \"{unanswered_msg[:200]}\"\n\n"
            )
        lean_prompt += (
            "Based on these messages, what is the single most important thing currently in progress or recently requested? "
            "Focus on the MOST RECENT incomplete task or request, not the one with the most message volume. "
            "Provide a 1-2 sentence summary of what Tarn should pick up. "
            "Be specific and terse. Do not use markdown."
        )

        # Build unanswered-message prefix for the startup response
        def _unanswered_prefix() -> str:
            if not unanswered_msg:
                return ""
            snippet = unanswered_msg[:100] + ("..." if len(unanswered_msg) > 100 else "")
            return f"Before I get into restart context — you asked: \"{snippet}\" right before I went down. Picking that up now.\n\n"

        def _fallback_msg() -> str:
            if unanswered_msg:
                snippet = unanswered_msg[:100] + ("..." if len(unanswered_msg) > 100 else "")
                return f"Back — saw you asked: \"{snippet}\". Picking that up now."
            return "I'm back — ready to continue. What are we working on?"

        try:
            response = await asyncio.wait_for(
                _ollama_chat(ollama_model, lean_prompt, timeout=45.0),
                timeout=50.0,
            )
            if response and response.strip():
                log.info("Startup: Ollama context review succeeded for channel %s", chat_id)
                await send_fn(_unanswered_prefix() + response)
            else:
                log.warning("Startup: Ollama returned empty response for channel %s — using fallback", chat_id)
                await send_fn(_fallback_msg())
        except asyncio.TimeoutError:
            log.warning("Startup context review (Ollama) timed out for channel %s", chat_id)
            await send_fn(_fallback_msg())
        except Exception:
            log.exception("Failed to run startup context review for channel %s", chat_id)
            await send_fn(_fallback_msg())

        # Queue the unanswered message for actual agent processing
        if unanswered_msg:
            # Restart-loop guard: don't auto-execute restart requests unless
            # pending-restart.md explicitly lists one newer than this startup.
            _startup_ts = _time.time() - 120  # generous: started within last 2 minutes
            if _is_restart_request(unanswered_msg) and not _has_pending_restart_request(config, _startup_ts):
                log.info(
                    "Startup: skipping re-queue of restart request in channel %s "
                    "(loop guard: no pending-restart.md entry)",
                    chat_id,
                )
            else:
                log.info("Startup: queuing unanswered message for agent processing in channel %s", chat_id)

                async def _unanswered_deliver(text: str) -> None:
                    await send_fn(text)

                async def _unanswered_error(exc: Exception) -> None:
                    log.error(
                        "Startup: failed to process unanswered message in channel %s: %s",
                        chat_id, exc,
                    )

                _unanswered_task = asyncio.create_task(
                    agent.reply(message=unanswered_msg, chat_id=chat_id, is_main_session=True)
                )
                if discord_bot and hasattr(discord_bot, "_bg_tasks"):
                    discord_bot._bg_tasks.register(
                        task=_unanswered_task,
                        deliver_fn=_unanswered_deliver,
                        error_fn=_unanswered_error,
                        channel_type="discord",
                        channel_id=chat_id,
                        original_message=unanswered_msg,
                    )

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

    # Remove the starting sentinel once we are bound and listening —
    # this signals the watchdog that startup succeeded and it is safe to
    # resume health checks for this agent.
    _sentinel_agent_name = config.identity_dir.name if config.identity_dir else None
    if _sentinel_agent_name:
        _sentinel_file = Path(f"/tmp/{_sentinel_agent_name}_starting")
        try:
            _sentinel_file.unlink(missing_ok=True)
            log.debug("Removed starting sentinel %s", _sentinel_file)
        except Exception:
            log.debug("Could not remove starting sentinel %s", _sentinel_file, exc_info=True)

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
