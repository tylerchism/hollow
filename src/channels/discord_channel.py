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

Interim messaging: active channel ID is written to /tmp/tarn_active_discord_channel
so that bin/send_discord and bin/send_msg can fire mid-turn messages.
"""

import asyncio
import logging
import os
import pathlib
import tempfile
import time
from typing import Callable

import discord
from discord.ext import commands

from src.agent import AgentRunner
from src.config import Config

log = logging.getLogger(__name__)

# Channel name the bot treats as "always respond here" (case-insensitive)
TARN_CHANNEL_NAME = "tarn"

# Discord message character limit
DISCORD_MSG_LIMIT = 2000

# TTL for the seen-message dedup set (seconds)
DEDUP_TTL_SECS = 300

# File written so bin/send_discord and bin/send_msg can send interim messages
ACTIVE_CHANNEL_FILE = pathlib.Path(tempfile.gettempdir()) / "tarn_active_discord_channel"


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


class DiscordBot:
    """Discord bot that routes messages through MessageQueue to AgentRunner."""

    def __init__(self, config: Config, agent: AgentRunner):
        self.config = config
        self.agent = agent
        self._queue = MessageQueue()
        self._client: commands.Bot | None = None

        # Dedup: message_id -> completed_at timestamp
        self._dedup_cache: dict[int, float] = {}

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

    def _write_active_channel(self, channel_id: str) -> None:
        try:
            import time
            ACTIVE_CHANNEL_FILE.write_text(channel_id)
            ts_file = pathlib.Path(tempfile.gettempdir()) / "tarn_active_discord_ts"
            ts_file.write_text(str(int(time.time())))
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

    def get_tarn_chat_ids(self) -> list[tuple[str, "discord.TextChannel"]]:
        """Return (chat_id_str, channel) for every 'tarn' channel across all guilds."""
        if not self._client:
            return []
        results = []
        for guild in self._client.guilds:
            channel = discord.utils.get(guild.text_channels, name=TARN_CHANNEL_NAME)
            if channel is not None:
                results.append((str(channel.id), channel))
        return results

    async def _send_chunked(self, channel, text: str) -> None:
        """Send text to a channel, splitting at Discord's 2000-char limit."""
        text = str(text)
        if len(text) <= DISCORD_MSG_LIMIT:
            await channel.send(text)
        else:
            for i in range(0, len(text), DISCORD_MSG_LIMIT):
                await channel.send(text[i : i + DISCORD_MSG_LIMIT])

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
    ) -> None:
        """Shared processing pipeline for all message types (mirrors telegram.py)."""
        msg_id = message.id

        if self._is_duplicate(msg_id):
            log.info("Discord: dropping duplicate message_id=%d", msg_id)
            return

        # Write active channel for bin/send_discord and bin/send_msg
        self._write_active_channel(channel_id)

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
                response = await self.agent.reply(
                    message=message_text,
                    chat_id=channel_id,
                    is_main_session=is_main_session,
                )
                await self._send_chunked(message.channel, response)
            except Exception:
                log.exception("Discord: error processing message in channel %s", channel_id)
                await message.channel.send("Something went wrong. Try again in a moment.")
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

    async def start(self) -> None:
        """Build the discord.py bot and connect."""
        token = os.getenv("DISCORD_BOT_TOKEN", "")
        if not token:
            raise RuntimeError("DISCORD_BOT_TOKEN is not set")

        intents = discord.Intents.default()
        intents.message_content = True  # Required for reading message text
        intents.guilds = True
        intents.dm_messages = True

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
                and message.channel.name.lower() == TARN_CHANNEL_NAME
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

            should_respond = is_dm or is_tarn_channel or is_mentioned or is_reply_to_bot or is_owner
            if not should_respond:
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

            await self._process_and_reply(
                message=message,
                channel_id=channel_id,
                user_id=user_id,
                message_text=content,
                is_main_session=is_main_session,
            )

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

    async def stop(self) -> None:
        """Graceful shutdown."""
        await self._queue.shutdown()
        if self._client:
            await self._client.close()
        # Clean up active channel file
        try:
            if ACTIVE_CHANNEL_FILE.exists():
                ACTIVE_CHANNEL_FILE.unlink()
        except Exception:
            pass
