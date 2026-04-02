"""Discord channel adapter — two-way bot using discord.py 2.x async API.

Recommended server channel structure:
  #tarn          — two-way chat (main conversation channel)
  #morning-brief — agent posts morning brief here if channel exists
  #alerts        — watchdog/cron alerts
  #tasks         — task tracking posts
  #ideas         — ideas from the ideas cron
  #agent-output  — misc agent output

The bot responds to:
  - All messages in a channel named "tarn" (or any channel matching TARN_CHANNEL_NAME)
  - DMs to the bot
  - Messages that @mention the bot in other channels

Interim messaging: active channel ID is written to /tmp/hollow_active_discord_<agent>
so that bin/send_discord and bin/send_msg can fire mid-turn messages.
"""

import asyncio
import json
import logging
import os
import pathlib
import tempfile
import time
import urllib.error
import urllib.request
from typing import Callable

import discord
from discord.ext import commands

from src.agent import AgentRunner
from src.channels.background_tasks import BackgroundTaskManager
from src.config import Config

log = logging.getLogger(__name__)

# Channel name the bot treats as "always respond here" (case-insensitive)
TARN_CHANNEL_NAME = "tarn"

# Discord message character limit
DISCORD_MSG_LIMIT = 2000

# TTL for the seen-message dedup set (seconds)
DEDUP_TTL_SECS = 300


def _split_for_discord(text: str, limit: int = DISCORD_MSG_LIMIT) -> list[str]:
    """Split text into chunks no longer than *limit* chars.

    Splitting priority (highest to lowest):
      1. Paragraph breaks (double newline) — preserves visual structure.
      2. Sentence-ending punctuation followed by a space.
      3. Single newline.
      4. Hard cut at *limit* (last resort — avoids mid-word cuts where possible).

    Each returned chunk is non-empty and stripped of leading/trailing whitespace.
    """
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []

    while len(text) > limit:
        window = text[:limit]

        # 1. Paragraph break — find the last \n\n within the window.
        idx = window.rfind("\n\n")
        if idx > 0:
            chunks.append(text[:idx].rstrip())
            text = text[idx:].lstrip("\n")
            continue

        # 2. Sentence boundary — look for ". ", "! ", "? " (or same before \n).
        best = -1
        for punct in (". ", "! ", "? ", ".\n", "!\n", "?\n"):
            pos = window.rfind(punct)
            if pos > best:
                best = pos
        if best > 0:
            cut = best + 2  # include the punctuation character + space
            chunks.append(text[:cut].rstrip())
            text = text[cut:].lstrip()
            continue

        # 3. Single newline.
        idx = window.rfind("\n")
        if idx > 0:
            chunks.append(text[:idx].rstrip())
            text = text[idx:].lstrip("\n")
            continue

        # 4. Last resort — hard cut at limit.
        chunks.append(text[:limit])
        text = text[limit:]

    if text:
        chunks.append(text)

    return [c for c in chunks if c]


class MessageQueue:
    """Per-channel sequential message processing queue (mirrors telegram.py)."""

    def __init__(self):
        self._queues: dict[str, asyncio.Queue] = {}
        self._workers: dict[str, asyncio.Task] = {}

    async def enqueue(self, channel_id: str, handler: Callable) -> None:
        if channel_id not in self._queues:
            self._queues[channel_id] = asyncio.Queue()
            self._workers[channel_id] = asyncio.create_task(
                self._worker(channel_id)
            )
        await self._queues[channel_id].put(handler)

    async def _worker(self, channel_id: str) -> None:
        queue = self._queues[channel_id]
        while True:
            handler = await queue.get()
            try:
                await handler()
            except Exception:
                log.exception("Error processing queued message for channel %s", channel_id)
            finally:
                queue.task_done()

    async def shutdown(self) -> None:
        for task in self._workers.values():
            task.cancel()
        for task in self._workers.values():
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._workers.clear()
        self._queues.clear()


def _load_channel_routing() -> dict[str, dict]:
    """Load channel routing config from hollow.config.json.

    Returns a dict keyed by discord_channel name (e.g. "trader-bot") with
    values containing at least: port, webhook_url, display_name.
    """
    routing: dict[str, dict] = {}
    try:
        config_path = pathlib.Path(__file__).parent.parent.parent / "hollow.config.json"
        if not config_path.exists():
            return routing
        data = json.loads(config_path.read_text())
        for agent in data.get("agents", []):
            ch = agent.get("discord_channel", "").strip().lower()
            if not ch:
                continue
            routing[ch] = {
                "name": agent.get("name", ""),
                "port": int(agent.get("port", 0)),
                "webhook_url": agent.get("discord_webhook", ""),
                "display_name": agent.get("discord_display_name", agent.get("name", "")),
            }
    except Exception:
        log.exception("Failed to load channel routing from hollow.config.json")
    return routing


async def _post_to_agent(port: int, message: str, chat_id: str) -> str | None:
    """POST to an agent's /ask endpoint. Returns the response text or None on error."""
    import asyncio
    loop = asyncio.get_running_loop()
    url = f"http://127.0.0.1:{port}/ask"
    payload = json.dumps({
        "message": message,
        "chat_id": chat_id,
        "is_main_session": True,
    }).encode()

    def _do_request():
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=900) as resp:
            data = json.loads(resp.read().decode())
            return data.get("response", "")

    try:
        return await loop.run_in_executor(None, _do_request)
    except Exception:
        log.exception("Failed to POST to agent at port %d", port)
        return None


async def _send_webhook(webhook_url: str, text: str, username: str) -> None:
    """POST a message to a Discord webhook as the given username."""
    import asyncio
    loop = asyncio.get_running_loop()

    def _post_chunk(chunk: str) -> None:
        payload = json.dumps({"content": chunk, "username": username}).encode()
        req = urllib.request.Request(webhook_url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                _ = resp.read()
        except urllib.error.HTTPError as exc:
            log.error("Webhook POST failed: %s %s", exc.code, exc.reason)
        except Exception:
            log.exception("Webhook POST error")

    text = str(text)
    chunks = _split_for_discord(text) if text else [""]
    for chunk in chunks:
        await loop.run_in_executor(None, _post_chunk, chunk)


class DiscordBot:
    """Discord bot that routes messages through MessageQueue to AgentRunner."""

    def __init__(self, config: Config, agent: AgentRunner):
        self.config = config
        self.agent = agent
        self._queue = MessageQueue()
        self._client: commands.Bot | None = None
        self._ready_event: asyncio.Event = asyncio.Event()
        self._shutting_down: bool = False

        # Dedup: message_id -> completed_at timestamp
        self._dedup_cache: dict[int, float] = {}
        self._bg_tasks = BackgroundTaskManager()

        # Agent name — used to namespace /tmp files so concurrent bots don't
        # clobber each other's active-channel state.
        _agent_name = config.identity_dir.name if config.identity_dir else "hollow"
        _tmp = pathlib.Path(tempfile.gettempdir())
        self._active_channel_file     = _tmp / f"hollow_active_discord_{_agent_name}"
        self._active_discord_ts_file  = _tmp / f"hollow_active_discord_ts_{_agent_name}"
        self._active_webhook_file     = _tmp / f"hollow_active_discord_webhook_{_agent_name}"
        self._active_webhook_name_file = _tmp / f"hollow_active_discord_webhook_name_{_agent_name}"

        # Channel routing: maps discord_channel names → agent config
        # Loaded once at startup from hollow.config.json.
        self._channel_routing: dict[str, dict] = _load_channel_routing()
        log.info(
            "Discord channel routing loaded: %s",
            {ch: r["name"] for ch, r in self._channel_routing.items()},
        )

        # Allowed Discord user IDs (loaded from DISCORD_ALLOWED_USERS env var,
        # comma-separated). Empty = allow all.
        allowed_raw = os.getenv("DISCORD_ALLOWED_USERS", "")
        self._allowed_users: set[int] = {
            int(u.strip()) for u in allowed_raw.split(",") if u.strip()
        }

        # Owner Discord user ID (loaded from DISCORD_OWNER_ID env var).
        # When set, the owner can message the bot in any channel and get a
        # response — channel gating (#tarn / DM / mention) is bypassed for
        # this user only.
        owner_raw = os.getenv("DISCORD_OWNER_ID", "").strip()
        self._owner_id: int | None = int(owner_raw) if owner_raw else None

        # Silent channels (loaded from DISCORD_SILENT_CHANNELS env var,
        # comma-separated channel names). The bot reads messages in these
        # channels for cron/context but NEVER responds — not even to the owner.
        silent_raw = os.getenv("DISCORD_SILENT_CHANNELS", "")
        self._silent_channels: set[str] = {
            c.strip().lower() for c in silent_raw.split(",") if c.strip()
        }

    # ─── Dedup ───────────────────────────────────────────────────────────────

    def _is_duplicate(self, message_id: int) -> bool:
        now = time.time()
        expired = [mid for mid, ts in self._dedup_cache.items() if now - ts > DEDUP_TTL_SECS]
        for mid in expired:
            del self._dedup_cache[mid]
        return message_id in self._dedup_cache

    def _mark_seen(self, message_id: int) -> None:
        self._dedup_cache[message_id] = time.time()

    # ─── Permissions ─────────────────────────────────────────────────────────

    def _is_allowed(self, user_id: int) -> bool:
        if not self._allowed_users:
            return True
        return user_id in self._allowed_users

    # ─── Active channel tracking ─────────────────────────────────────────────

    def _write_active_channel(self, channel_id: str, channel_name: str = "") -> None:
        try:
            import time
            self._active_channel_file.write_text(channel_id)
            self._active_discord_ts_file.write_text(str(int(time.time())))

            # If this channel has a dedicated webhook route, write the webhook
            # URL and display name so send_discord / send_msg can post as the
            # routed agent (e.g. "Flux" in #trader-bot) via webhook.
            channel_name_lower = channel_name.lower()
            routing = self._channel_routing.get(channel_name_lower)
            if routing and routing.get("webhook_url"):
                self._active_webhook_file.write_text(routing["webhook_url"])
                self._active_webhook_name_file.write_text(routing["display_name"])
            else:
                # Clear any stale webhook files when the active channel changes
                # to a non-routed channel.
                try:
                    self._active_webhook_file.unlink(missing_ok=True)
                    self._active_webhook_name_file.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception:
            pass

    # ─── Send helpers ─────────────────────────────────────────────────────────

    async def send_message(self, channel_id: int | str, text: str) -> None:
        """Send a message to a channel by ID (used by cron notifications)."""
        if not self._client:
            return
        channel = self._client.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self._client.fetch_channel(int(channel_id))
            except Exception:
                log.warning("Discord: could not find channel %s", channel_id)
                return
        await self._send_chunked(channel, text)

    async def send_to_named_channel(self, guild: discord.Guild, channel_name: str, text: str) -> bool:
        """Send to a channel by name within a guild. Returns True if sent."""
        channel = discord.utils.get(guild.text_channels, name=channel_name)
        if channel is None:
            return False
        await self._send_chunked(channel, text)
        return True

    async def post_to_log(self, message: str) -> bool:
        """Post a message to the trading log channel (one-way write, no response).

        Reads TRADING_LOG_CHANNEL from the environment (default: "trading-log").
        Returns True if the message was sent to at least one guild, False otherwise.
        """
        channel_name = os.getenv("TRADING_LOG_CHANNEL", "trading-log").strip()
        if not self._client:
            log.warning("post_to_log: Discord client not ready, dropping message")
            return False
        sent_any = False
        for guild in self._client.guilds:
            sent = await self.send_to_named_channel(guild, channel_name, message)
            if sent:
                sent_any = True
        if not sent_any:
            log.warning("post_to_log: channel #%s not found on any guild", channel_name)
        return sent_any

    def get_tarn_chat_ids(self) -> list[tuple[str, "discord.TextChannel"]]:
        """Return (chat_id_str, channel) for every 'tarn' channel across all guilds."""
        if not self._client:
            return []
        results = []
        for guild in self._client.guilds:
            channel = discord.utils.get(guild.text_channels, name=self.config.discord_channel_name)
            if channel is not None:
                results.append((str(channel.id), channel))
        return results

    async def get_channel_history(self, channel_name: str, limit: int = 50) -> str:
        """Fetch recent message history from a named Discord channel.

        Searches all guilds for a text channel matching channel_name (case-insensitive).
        Returns a formatted string of recent messages, oldest first.
        Silent-channel status does NOT block reading — this method is for explicit
        history lookups, not automatic responses.

        Args:
            channel_name: The channel name to look up (e.g. "trader-bot").
            limit: Maximum number of messages to fetch (default 50, max 100).

        Returns:
            Formatted string with messages, or an error/empty message string.
        """
        if not self._client:
            return "Discord client is not connected."

        limit = max(1, min(limit, 100))
        channel_name_lower = channel_name.lower().lstrip("#")

        target_channel = None
        for guild in self._client.guilds:
            ch = discord.utils.get(guild.text_channels, name=channel_name_lower)
            if ch is not None:
                target_channel = ch
                break

        if target_channel is None:
            return f"Channel #{channel_name_lower} not found on any guild."

        try:
            messages = []
            async for msg in target_channel.history(limit=limit, oldest_first=True):
                ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                author = msg.author.display_name or msg.author.name
                content = msg.content or "(no text)"
                messages.append(f"[{ts}] {author}: {content}")
            if not messages:
                return f"No messages found in #{channel_name_lower}."
            return "\n".join(messages)
        except discord.Forbidden:
            return f"Missing permissions to read #{channel_name_lower}."
        except Exception as exc:
            log.exception("get_channel_history: error fetching #%s", channel_name_lower)
            return f"Error fetching history for #{channel_name_lower}: {exc}"

    async def _send_chunked(self, channel, text: str) -> None:
        """Send text to a channel, splitting at Discord's 2000-char limit.

        Splits are made at paragraph breaks (double newlines) when possible,
        then at sentence-ending punctuation, then at single newlines, and
        finally at the hard character limit as a last resort.
        """
        text = str(text)
        for chunk in _split_for_discord(text):
            await channel.send(chunk)

    # ─── Attachment download ──────────────────────────────────────────────────

    async def _download_attachment(self, attachment: discord.Attachment) -> str:
        """Download a Discord attachment to /tmp and return the local path."""
        import httpx

        filename = attachment.filename or f"discord_attachment_{attachment.id}"
        suffix = pathlib.Path(filename).suffix or ""
        tmp_path = pathlib.Path(tempfile.gettempdir()) / f"tarn_discord_{attachment.id}{suffix}"

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(attachment.url)
            resp.raise_for_status()
            tmp_path.write_bytes(resp.content)

        return str(tmp_path)

    # ─── Core processing pipeline ─────────────────────────────────────────────

    async def _process_and_reply(
        self,
        message: discord.Message,
        channel_id: str,
        user_id: int,
        message_text: str,
        is_main_session: bool,
        channel_name: str = "",
        webhook_url: str = "",
        webhook_display_name: str = "",
    ) -> None:
        """Shared processing pipeline for all message types (mirrors telegram.py).

        When *webhook_url* is provided responses are delivered via that webhook
        (posting as *webhook_display_name*) instead of via the Discord bot API.
        This is used for self-routed channels (e.g. Flux's #trader-bot) so the
        full 900s wait + BackgroundTaskManager flow is available while still
        posting replies as the branded webhook user.
        """
        msg_id = message.id

        if self._is_duplicate(msg_id):
            log.info("Discord: dropping duplicate message_id=%d", msg_id)
            return

        # Write active channel for bin/send_discord and bin/send_msg
        self._write_active_channel(channel_id, channel_name=channel_name)

        async def _send_reply(text: str) -> None:
            """Deliver *text* — via webhook if configured, otherwise direct."""
            if webhook_url:
                await _send_webhook(
                    webhook_url=webhook_url,
                    text=text,
                    username=webhook_display_name,
                )
            else:
                await self._send_chunked(message.channel, text)

        async def _send_status(text: str) -> None:
            """Send a short status notice always via the bot API (not webhook)."""
            await message.channel.send(text)

        async def process():
            # Show typing indicator while the agent works
            done = asyncio.Event()

            async def typing_keepalive():
                while not done.is_set():
                    try:
                        async with message.channel.typing():
                            # typing() context only lasts ~10s; re-enter in a loop
                            await asyncio.sleep(8)
                    except Exception:
                        await asyncio.sleep(8)

            keepalive_task = asyncio.create_task(typing_keepalive())
            try:
                # Create the task without wait_for so it is never cancelled on
                # timeout — we can hand it off to BackgroundTaskManager instead.
                reply_task = asyncio.create_task(
                    self.agent.reply(
                        message=message_text,
                        chat_id=channel_id,
                        is_main_session=is_main_session,
                    )
                )
                finished, _ = await asyncio.wait({reply_task}, timeout=900)

                if reply_task in finished:
                    # Completed within the initial wait window — deliver normally.
                    exc = reply_task.exception()
                    if exc is not None:
                        raise exc
                    response = reply_task.result()
                    # Empty response can mean either genuinely no output OR the agent
                    # suppressed a shutdown error and returned "". In both cases, only
                    # send the fallback if we are NOT shutting down.
                    if not response or not response.strip():
                        if not self._shutting_down:
                            fallback = "I ran out of investigation steps before finishing. Send me a message to continue where I left off."
                            await _send_reply(fallback)
                    else:
                        await _send_reply(response)
                else:
                    # Timed out — keep the task alive and hand it to the manager.
                    log.info(
                        "Discord: agent.reply() still running after 900s for channel %s — handing off to background",
                        channel_id,
                    )
                    if not self._shutting_down:
                        await _send_status(
                            "Still on it — this is taking longer than usual. I'll post the result here when it's done."
                        )

                    _shutting_down_ref = self  # to check self._shutting_down at delivery time

                    async def _deliver(result: str, _sd=_shutting_down_ref) -> None:
                        if _sd._shutting_down:
                            return
                        if not result or not result.strip():
                            result = "I ran out of investigation steps before finishing. Send me a message to continue where I left off."
                        await _send_reply(result)

                    async def _on_error(exc: Exception, _sd=_shutting_down_ref) -> None:
                        if _sd._shutting_down:
                            return
                        await _send_status("Something went wrong with that task. Try again in a moment.")

                    self._bg_tasks.register(
                        task=reply_task,
                        deliver_fn=_deliver,
                        error_fn=_on_error,
                        channel_type="discord",
                        channel_id=channel_id,
                    )
            except Exception:
                log.exception("Discord: error processing message in channel %s", channel_id)
                if not self._shutting_down:
                    await _send_status("Something went wrong. Try again in a moment.")
            finally:
                done.set()
                keepalive_task.cancel()
                try:
                    await keepalive_task
                except asyncio.CancelledError:
                    pass
                self._mark_seen(msg_id)

        await self._queue.enqueue(channel_id, process)

    # ─── Bot setup and event handlers ─────────────────────────────────────────

    async def start(self, token: str | None = None) -> None:
        """Build the discord.py bot and connect."""
        token = token or self.config.discord_bot_token or os.getenv("DISCORD_BOT_TOKEN", "")
        if not token:
            raise RuntimeError("DISCORD_BOT_TOKEN is not set")

        intents = discord.Intents.default()
        intents.message_content = True  # Required for reading message text
        intents.guilds = True
        intents.dm_messages = True
        intents.reactions = True

        self._client = commands.Bot(
            command_prefix="!",  # Unused but required by commands.Bot
            intents=intents,
            help_command=None,
        )

        client = self._client

        @client.event
        async def on_ready():
            log.info(
                "Discord bot connected as %s (id=%s), in %d guild(s)",
                client.user,
                client.user.id,
                len(client.guilds),
            )
            for guild in client.guilds:
                log.info("  Guild: %s (id=%s)", guild.name, guild.id)
            self._ready_event.set()

        @client.event
        async def on_message(message: discord.Message):
            # Never reply to self
            if message.author == client.user:
                return

            # Ignore bots
            if message.author.bot:
                return

            user_id = message.author.id
            is_dm = isinstance(message.channel, discord.DMChannel)

            # Permission check
            if not self._is_allowed(user_id):
                return

            # Decide whether to respond
            is_tarn_channel = (
                not is_dm
                and hasattr(message.channel, "name")
                and message.channel.name.lower() == self.config.discord_channel_name
            )
            is_mentioned = client.user in message.mentions
            is_reply_to_bot = (
                message.reference is not None
                and message.reference.resolved is not None
                and isinstance(message.reference.resolved, discord.Message)
                and message.reference.resolved.author == client.user
            )

            # Owner bypass: if DISCORD_OWNER_ID is set and matches the
            # message author, respond regardless of channel.
            is_owner = self._owner_id is not None and user_id == self._owner_id

            # Routed channels: only respond if the routing entry points to THIS
            # agent (self-route by port). Cross-agent routes belong to the
            # owning bot and should be ignored here — otherwise two bots both
            # process the same message.
            _pre_channel_name = getattr(message.channel, "name", "").lower()
            _routing_entry = self._channel_routing.get(_pre_channel_name)
            is_routed_channel = (
                _routing_entry is not None
                and _routing_entry.get("port") == self.config.api_port
            )

            # Detect cross-agent channels: routing entry exists but points to a
            # different agent's port. Owner messages in these channels must still
            # go through the routing path (not be processed by Tarn directly).
            # is_owner grants access to all of Tarn's own channels but must NOT
            # suppress routing for channels owned by other agents.
            _is_cross_agent_channel = (
                _routing_entry is not None
                and _routing_entry.get("port")
                and _routing_entry.get("port") != self.config.api_port
            )

            # is_owner bypass applies only when the channel is NOT a cross-agent
            # channel. For cross-agent channels, the routing block below handles
            # the message (including owner messages) via the HTTP path.
            is_owner_eligible = is_owner and not _is_cross_agent_channel

            should_respond = is_dm or is_tarn_channel or is_mentioned or is_reply_to_bot or is_owner_eligible or is_routed_channel or _is_cross_agent_channel
            if not should_respond:
                return

            # Silent channel check: takes priority over everything, including
            # the owner bypass. Tarn can read these channels for context but
            # must never send a reply.
            channel_name = getattr(message.channel, "name", "").lower()
            if channel_name in self._silent_channels:
                log.debug("Discord: silent channel %r — skipping response", channel_name)
                return

            channel_id = str(message.channel.id)
            is_main_session = is_dm or is_tarn_channel

            # Build the message text
            content = message.content or ""

            # Strip bot mention from content
            if client.user.mention in content:
                content = content.replace(client.user.mention, "").strip()
            # Also strip <@!ID> variant
            alt_mention = f"<@!{client.user.id}>"
            if alt_mention in content:
                content = content.replace(alt_mention, "").strip()

            # Handle attachments
            if message.attachments:
                attachment_notes = []
                for att in message.attachments:
                    try:
                        local_path = await self._download_attachment(att)
                        mime = att.content_type or ""
                        filename = att.filename or ""
                        suffix = pathlib.Path(filename).suffix.lower()

                        if "image" in mime or suffix in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                            note = f"[Image sent via Discord — use the Read tool to view it: {local_path}]"
                        elif suffix == ".pdf" or "pdf" in mime:
                            note = f"[PDF file sent via Discord — use the Read tool to view it: {local_path}]"
                        else:
                            note = f"[File sent via Discord: {filename} — saved to {local_path}]"

                        attachment_notes.append(note)
                    except Exception:
                        log.exception("Discord: failed to download attachment %s", att.filename)
                        attachment_notes.append(f"[Attachment failed to download: {att.filename}]")

                if content:
                    content = content + "\n\n" + "\n".join(attachment_notes)
                else:
                    content = "\n".join(attachment_notes)

            if not content:
                return

            # Channel routing: if this channel is mapped to a specialist agent,
            # either call agent.reply() directly (self-route) or POST to the
            # remote agent's /ask endpoint, then deliver via webhook.
            routing = self._channel_routing.get(channel_name)
            if routing and routing.get("port") and routing.get("webhook_url"):
                log.info(
                    "Discord: routing #%s message to agent '%s' (port=%d)",
                    channel_name,
                    routing["name"],
                    routing["port"],
                )

                # Self-route: the routing config points at this agent's own port.
                # Skip the HTTP hop entirely and go through _process_and_reply()
                # so we get the full 900s asyncio.wait + BackgroundTaskManager
                # flow with proper typing keepalive and webhook delivery.
                if routing["port"] == self.config.api_port:
                    log.info(
                        "Discord: self-route detected for #%s — calling agent.reply() directly",
                        channel_name,
                    )
                    await self._process_and_reply(
                        message=message,
                        channel_id=channel_id,
                        user_id=user_id,
                        message_text=content,
                        is_main_session=True,
                        channel_name=channel_name,
                        webhook_url=routing["webhook_url"],
                        webhook_display_name=routing["display_name"],
                    )
                    return

                # Cross-agent route: POST to the remote agent's /ask endpoint.
                # Use a 900s timeout to match the BackgroundTaskManager window
                # on the remote side; the remote agent handles its own delivery.
                self._write_active_channel(channel_id, channel_name=channel_name)
                self._mark_seen(message.id)

                async def _routed_handler(
                    _content=content,
                    _channel_id=channel_id,
                    _routing=routing,
                    _discord_channel=message.channel,
                ):
                    async with _discord_channel.typing():
                        response = await _post_to_agent(
                            port=_routing["port"],
                            message=_content,
                            chat_id=_channel_id,
                        )
                    if response and response.strip():
                        await _send_webhook(
                            webhook_url=_routing["webhook_url"],
                            text=response,
                            username=_routing["display_name"],
                        )
                    else:
                        log.warning(
                            "Discord: routed agent '%s' returned empty response",
                            _routing["name"],
                        )

                await self._queue.enqueue(channel_id, _routed_handler)
                return

            await self._process_and_reply(
                message=message,
                channel_id=channel_id,
                user_id=user_id,
                message_text=content,
                is_main_session=is_main_session,
                channel_name=channel_name,
            )

        @client.event
        async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
            """💰 reaction on #ai-money → create MC idea."""
            # Only owner
            if self._owner_id is None or payload.user_id != self._owner_id:
                return
            # Only 💰 emoji
            if str(payload.emoji) != "💰":
                return
            # Only in #ai-money channel
            channel = client.get_channel(payload.channel_id)
            if channel is None or not hasattr(channel, "name") or channel.name != "ai-money":
                return
            try:
                message = await channel.fetch_message(payload.message_id)
                content = message.content or ""
                # Truncate to 80 chars for title
                title = content[:80].strip()
                if not title:
                    return
                # Escape quotes for shell
                safe_title = title.replace('"', '\\"')
                proc = await asyncio.create_subprocess_shell(
                    f'mc ideas create "{safe_title}"',
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()
                await channel.send("✅ Added to Mission Control ideas board")
            except Exception:
                log.exception("Failed to create MC idea from reaction")

        @client.tree.command(name="clear", description="Clear conversation history for this channel")
        async def slash_clear(interaction: discord.Interaction):
            if not self._is_allowed(interaction.user.id):
                await interaction.response.send_message("Not authorized.", ephemeral=True)
                return
            channel_id = str(interaction.channel_id)
            await self.agent.clear_history(channel_id)
            await interaction.response.send_message("Conversation history cleared.", ephemeral=True)

        @client.tree.command(name="whoami", description="Show your Discord user ID")
        async def slash_whoami(interaction: discord.Interaction):
            await interaction.response.send_message(
                f"User ID: `{interaction.user.id}`\nUsername: `{interaction.user.name}`",
                ephemeral=True,
            )

        # Start the bot (blocks until stopped via close())
        log.info("Starting Discord bot...")
        await client.start(token)

    @property
    def is_running(self) -> bool:
        """True if the bot has connected to Discord (on_ready has fired)."""
        return self._client is not None and self._ready_event.is_set()

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._shutting_down = True
        await self._bg_tasks.shutdown()
        await self._queue.shutdown()
        if self._client:
            await self._client.close()
        # Clean up active channel and webhook temp files
        for _f in (
            self._active_channel_file,
            self._active_discord_ts_file,
            self._active_webhook_file,
            self._active_webhook_name_file,
        ):
            try:
                _f.unlink(missing_ok=True)
            except Exception:
                pass
