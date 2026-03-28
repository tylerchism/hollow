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


def make_api_app(agent: AgentRunner) -> web.Application:
    """Create the aiohttp app for the HTTP API."""

    async def handle_health(request: web.Request) -> web.Response:
        cfg = agent.config
        agent_name = cfg.identity_dir.name if cfg.identity_dir else "hollow"
        return web.json_response({"status": "ok", "agent": agent_name})

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

    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_post("/ask", handle_ask)
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
                response = await agent.reply(
                    message=_prompt,
                    chat_id=_chat_id,
                    is_main_session=_is_main,
                )
                log.info("Cron job '%s' completed (%d chars)", _name, len(response))

                if _notify and bot and agent.config.heartbeat_chat_id:
                    await bot.send_message(agent.config.heartbeat_chat_id, response)

                if discord_bot and (_notify_discord or _discord_channel_name):
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


async def run(args: argparse.Namespace = None):
    """Main async entry point — HTTP API + Telegram bot + APScheduler."""
    config = load_config()
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
    api_app = make_api_app(agent)
    api_runner = web.AppRunner(api_app)
    await api_runner.setup()
    site = web.TCPSite(api_runner, config.api_host, config.api_port)
    await site.start()
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
            discord_task = asyncio.create_task(discord_bot.start())
            log.info("Discord bot starting (background task)")

        scheduler.start()
        log.info("APScheduler started with %d job(s)", len(scheduler.get_jobs()))

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
