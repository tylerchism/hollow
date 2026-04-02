"""Hollow — AI agent runtime entry point."""

import argparse
import asyncio
import json
import logging
import signal
import sys
from pathlib import Path

from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.agent import AgentRunner
from src.channels import DiscordBot, TelegramBot
from src.config import load_config
from src.memory import MemoryManager

log = logging.getLogger(__name__)


def make_api_app(
    agent: AgentRunner,
    bot: "TelegramBot | None" = None,
    discord_bot: "DiscordBot | None" = None,
) -> web.Application:
    """Create the aiohttp app for the HTTP API."""

    async def handle_health(request: web.Request) -> web.Response:
        cfg = agent.config
        agent_name = cfg.identity_dir.name if cfg.identity_dir else "hollow"

        bots: dict[str, bool] = {}
        if bot is not None:
            try:
                bots["telegram"] = bool(
                    bot.app.updater and bot.app.updater.running
                )
            except Exception:
                bots["telegram"] = False
        if discord_bot is not None:
            try:
                bots["discord"] = bool(
                    discord_bot._client and discord_bot._client.is_ready()
                )
            except Exception:
                bots["discord"] = False

        # If any configured bot failed to start, report degraded
        if bots and not all(bots.values()):
            return web.json_response({"status": "degraded", "agent": agent_name, "bots": bots})

        payload: dict = {"status": "ok", "agent": agent_name}
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

    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_post("/ask", handle_ask)
    app.router.add_get("/history/{channel_name}", handle_history)
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
    bot: TelegramBot | None,
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
        notify_telegram = job_def.get("notify_telegram", False)
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
            _notify=notify_telegram,
            _notify_discord=notify_discord,
            _discord_channel_name=discord_channel_name,
        ):
            log.info("Cron job '%s' firing", _name)
            try:
                _aname = agent.config.identity_dir.name if agent.config.identity_dir else "hollow"
                tg_active_file = Path(f"/tmp/hollow_active_chat_{_aname}")
                discord_channel_file = Path(f"/tmp/hollow_active_discord_{_aname}")
                discord_ts_file = Path(f"/tmp/hollow_active_discord_ts_{_aname}")

                saved_tg_chat_id = None
                saved_discord_channel = None
                saved_discord_ts = None

                if not _notify and tg_active_file.exists():
                    saved_tg_chat_id = tg_active_file.read_text()
                    tg_active_file.unlink()

                # If this cron delivers to a specific Discord channel, suppress
                # send_msg from routing interim messages to the active #tarn channel.
                if _discord_channel_name:
                    if discord_channel_file.exists():
                        saved_discord_channel = discord_channel_file.read_text()
                        discord_channel_file.unlink()
                    if discord_ts_file.exists():
                        saved_discord_ts = discord_ts_file.read_text()
                        discord_ts_file.unlink()

                try:
                    response = await asyncio.wait_for(
                        agent.reply(
                            message=_prompt,
                            chat_id=_chat_id,
                            is_main_session=_is_main,
                        ),
                        timeout=900,
                    )
                finally:
                    if saved_tg_chat_id is not None:
                        tg_active_file.write_text(saved_tg_chat_id)
                    if saved_discord_channel is not None:
                        discord_channel_file.write_text(saved_discord_channel)
                    if saved_discord_ts is not None:
                        discord_ts_file.write_text(saved_discord_ts)
                log.info("Cron job '%s' completed (%d chars)", _name, len(response))

                if _notify and bot and agent.config.heartbeat_chat_id:
                    await bot.send_message(agent.config.heartbeat_chat_id, response)

                if discord_bot and discord_bot._client and (_notify_discord or _discord_channel_name):
                    if _discord_channel_name:
                        # Try to post to the named channel on every guild
                        sent_any = False
                        for guild in discord_bot._client.guilds:
                            sent = await discord_bot.send_to_named_channel(
                                guild, _discord_channel_name, response
                            )
                            sent_any = sent_any or sent
                        if not sent_any:
                            log.warning(
                                "Cron '%s': Discord channel '#%s' not found on any guild",
                                _name,
                                _discord_channel_name,
                            )
                    elif _notify_discord and agent.config.heartbeat_chat_id:
                        # Fall back to heartbeat channel ID if no channel name given
                        await discord_bot.send_message(agent.config.heartbeat_chat_id, response)
            except Exception:
                log.exception("Cron job '%s' failed", _name)

        scheduler.add_job(cron_handler, trigger, id=name, name=name)
        log.info("Scheduled cron job '%s': %s (%s)", name, schedule, tz)

    return scheduler


def check_setup(config, require_telegram: bool = True) -> list[str]:
    """Verify required files and config. Returns list of issues."""
    issues = []

    if require_telegram and not config.telegram_bot_token:
        issues.append("TELEGRAM_BOT_TOKEN not set")

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


async def _send_startup_notification(config, agent, bot, discord_bot) -> None:
    """On restart: review recent context per-channel and proactively continue work."""
    import os

    # Maintenance restarts are quiet — skip full re-orientation to avoid noise.
    restart_reason = os.environ.get("TARN_RESTART_REASON", "").strip().lower()
    is_maintenance = restart_reason == "maintenance"

    # Startup is a read-only context review — block Bash so the agent cannot
    # execute shell commands (restart-tarn, kill, pkill, etc.) while booting.
    from src.agent import NATIVE_TOOLS
    STARTUP_ALLOWED_TOOLS = [t for t in NATIVE_TOOLS if t != "Bash"]

    # Wait for Discord to fully connect before sending.
    # If Discord is enabled, wait for on_ready to fire (up to 30s); otherwise
    # fall back to a short sleep so Telegram still gets the notification promptly.
    if discord_bot:
        try:
            await asyncio.wait_for(discord_bot._ready_event.wait(), timeout=30)
        except asyncio.TimeoutError:
            log.warning("Discord ready_event timed out after 30s — proceeding with startup notification anyway")
    else:
        await asyncio.sleep(8)

    # Maintenance restart: post a minimal one-liner to Discord #tarn only.
    # No Telegram, no context re-evaluation.
    if is_maintenance:
        log.info("Maintenance restart detected — sending minimal notification to Discord only")
        if discord_bot and discord_bot._client:
            for discord_chat_id, tarn_channel in discord_bot.get_tarn_chat_ids():
                try:
                    await discord_bot._send_chunked(
                        tarn_channel,
                        "🔧 Maintenance restart complete. Crons reloaded.",
                    )
                except Exception:
                    log.exception("Failed to send maintenance restart notification to Discord channel %s", discord_chat_id)
        return

    ping = "I'm back — reviewing context and picking up where we left off..."

    startup_prompt = (
        "I just restarted. Review our recent conversation history and the current Mission Control "
        "task state to understand what we were working on. Then:\n"
        "1. Send a brief message (1-3 sentences) summarizing where things stand\n"
        "2. If there was active work in progress, continue it — don't wait to be re-prompted\n"
        "3. If nothing was actively in progress, just confirm you're up and note any pending "
        "decisions or blocked tasks that need attention\n\n"
        "Be proactive. Tyler should not have to re-prompt you to continue.\n\n"
        "CRITICAL: Do NOT run restart-tarn or any self-restart commands during this startup "
        "sequence. You literally just restarted — calling restart-tarn now would kill this "
        "process and create an infinite restart loop. If the previous conversation was about "
        "restart issues, just summarize the status — do not attempt to fix it by restarting again."
    )

    # ── Telegram ──────────────────────────────────────────────────────────────
    if bot and config.heartbeat_chat_id:
        tg_chat_id = str(config.heartbeat_chat_id)
        try:
            await bot.send_message(config.heartbeat_chat_id, ping)
        except Exception:
            log.exception("Failed to send Telegram startup ping")
        try:
            response = await asyncio.wait_for(
                agent.reply(
                    message=startup_prompt,
                    chat_id=tg_chat_id,
                    is_main_session=True,
                    allowed_tools=STARTUP_ALLOWED_TOOLS,
                ),
                timeout=120,
            )
            if response and response.strip():
                await bot.send_message(config.heartbeat_chat_id, response)
        except asyncio.TimeoutError:
            log.warning("Telegram startup context review timed out after 120s")
        except Exception:
            log.exception("Failed to run Telegram startup context review")
            try:
                await bot.send_message(
                    config.heartbeat_chat_id,
                    "I'm back — ready to continue. What are we working on?",
                )
            except Exception:
                pass

    # ── Discord ───────────────────────────────────────────────────────────────
    # Track the timestamp when this startup notification began. Any channel
    # that already has an assistant response AFTER this timestamp has already
    # been handled (e.g. the user messaged manually before the startup loop
    # got to that guild) — skip the AI review to avoid a double-message.
    startup_began_at = asyncio.get_event_loop().time()

    if discord_bot and discord_bot._client:
        for discord_chat_id, tarn_channel in discord_bot.get_tarn_chat_ids():
            try:
                await discord_bot._send_chunked(tarn_channel, ping)
            except Exception:
                log.exception("Failed to send Discord startup ping to channel %s", discord_chat_id)

            # If there's already a recent assistant reply in this channel's session
            # (posted after startup began), the user manually re-engaged and Tarn
            # already responded — skip the automated context review to avoid doubling.
            elapsed = asyncio.get_event_loop().time() - startup_began_at
            if await _channel_already_responded(agent, discord_chat_id, elapsed):
                log.info(
                    "Startup: Discord channel %s already has a recent reply — skipping duplicate context review",
                    discord_chat_id,
                )
                continue

            try:
                response = await asyncio.wait_for(
                    agent.reply(
                        message=startup_prompt,
                        chat_id=discord_chat_id,
                        is_main_session=True,
                        allowed_tools=STARTUP_ALLOWED_TOOLS,
                    ),
                    timeout=120,
                )
                if response and response.strip():
                    await discord_bot._send_chunked(tarn_channel, response)
            except asyncio.TimeoutError:
                log.warning("Discord startup context review timed out after 120s for channel %s", discord_chat_id)
            except Exception:
                log.exception("Failed to run Discord startup context review for channel %s", discord_chat_id)
                try:
                    await discord_bot._send_chunked(
                        tarn_channel,
                        "I'm back — ready to continue. What are we working on?",
                    )
                except Exception:
                    pass


async def run(args: argparse.Namespace = None):
    """Main async entry point — HTTP API + Telegram bot + APScheduler."""
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

    issues = check_setup(config, require_telegram=bool(config.telegram_bot_token))
    if issues:
        print(f"{len(issues)} issue(s):")
        for issue in issues:
            print(f"  - {issue}")
        # Don't exit — allow running without telegram for testing
        if any("Missing" in i for i in issues):
            print("\nWarning: some identity files missing. Context will be incomplete.")

    # Initialize memory
    memory = MemoryManager(config)
    await memory.initialize()

    # Initialize agent
    agent = AgentRunner(config, memory)
    await agent.initialize()

    # Initialize Telegram bot (if token available)
    bot = None
    if config.telegram_bot_token:
        bot = TelegramBot(config, agent)

    # Initialize Discord bot (if token available)
    discord_bot = None
    if config.discord_bot_token:
        discord_bot = DiscordBot(config, agent)

    # Load and set up cron scheduler
    crons = load_crons(config.memory_dir)
    scheduler = setup_scheduler(agent, bot, crons, config.user_timezone, discord_bot=discord_bot)

    # Set up HTTP API
    api_app = make_api_app(agent, bot=bot, discord_bot=discord_bot)
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
        if bot:
            await bot.start()
            log.info("Telegram bot started")

        if discord_bot:
            # discord.py's start() runs until close() is called — run as background task
            discord_task = asyncio.create_task(discord_bot.start(token=config.discord_bot_token))
            log.info("Discord bot starting (background task)")

        scheduler.start()
        log.info("APScheduler started with %d job(s)", len(scheduler.get_jobs()))

        # Send startup notification and proactively pick up where we left off.
        # Agents can set STARTUP_NOTIFICATION=false to suppress this (e.g. Flux,
        # whose #trader-bot channel is a conversation channel, not a monitoring
        # channel, and should not receive unsolicited restart announcements).
        if config.startup_notification:
            asyncio.create_task(
                _send_startup_notification(config, agent, bot, discord_bot)
            )

        print("Hollow is running. Press Ctrl+C to stop.")
        await stop_event.wait()
    finally:
        log.info("Shutting down...")
        scheduler.shutdown(wait=False)
        if discord_bot:
            await discord_bot.stop()
            if discord_task and not discord_task.done():
                discord_task.cancel()
                try:
                    await discord_task
                except (asyncio.CancelledError, Exception):
                    pass
        if bot:
            await bot.stop()
        await api_runner.cleanup()
        await agent.history.close()
        await memory.close()
        # Clean up stale active-channel temp files so they don't mis-route
        # messages after a restart.
        _aname = config.identity_dir.name if config.identity_dir else "hollow"
        for _tmp_file in (
            Path(f"/tmp/hollow_active_chat_{_aname}"),
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
