"""Telegram channel adapter with MessageQueue for sequential per-chat processing."""

import asyncio
import logging
from typing import Callable

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.agent import AgentRunner
from src.config import Config

log = logging.getLogger(__name__)


class MessageQueue:
    """Per-chat sequential message processing queue."""

    def __init__(self):
        self._queues: dict[str, asyncio.Queue] = {}
        self._workers: dict[str, asyncio.Task] = {}

    async def enqueue(self, chat_id: str, handler: Callable) -> None:
        """Enqueue an async handler for sequential processing."""
        if chat_id not in self._queues:
            self._queues[chat_id] = asyncio.Queue()
            self._workers[chat_id] = asyncio.create_task(self._worker(chat_id))
        await self._queues[chat_id].put(handler)

    async def _worker(self, chat_id: str) -> None:
        """Consume queue serially for a chat_id."""
        queue = self._queues[chat_id]
        while True:
            handler = await queue.get()
            try:
                await handler()
            except Exception:
                log.exception("Error processing queued message for chat %s", chat_id)
            finally:
                queue.task_done()

    async def shutdown(self) -> None:
        """Cancel all worker tasks."""
        for task in self._workers.values():
            task.cancel()
        for task in self._workers.values():
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._workers.clear()
        self._queues.clear()


class TelegramBot:
    """Telegram bot that routes messages through MessageQueue to AgentRunner."""

    def __init__(self, config: Config, agent: AgentRunner):
        self.config = config
        self.agent = agent
        self.allowed_users: set[int] = set(config.telegram_allowed_users)
        self.app: Application | None = None
        self._queue = MessageQueue()

    async def start(self) -> None:
        """Build the application and start polling."""
        self.app = Application.builder().token(self.config.telegram_bot_token).build()

        self.app.add_handler(CommandHandler("start", self._handle_start))
        self.app.add_handler(CommandHandler("clear", self._handle_clear))
        self.app.add_handler(CommandHandler("whoami", self._handle_whoami))
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )

        log.info("Starting Telegram bot (polling)...")
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)

    async def stop(self) -> None:
        """Graceful shutdown."""
        await self._queue.shutdown()
        if self.app:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()

    async def send_message(self, chat_id: int | str, text: str) -> None:
        """Send a message to a chat (used by cron notifications)."""
        if not self.app:
            return
        # Telegram 4096 char limit
        text = str(text)
        if len(text) <= 4096:
            await self.app.bot.send_message(chat_id=chat_id, text=text)
        else:
            for i in range(0, len(text), 4096):
                await self.app.bot.send_message(chat_id=chat_id, text=text[i : i + 4096])

    def _is_allowed(self, user_id: int) -> bool:
        if not self.allowed_users:
            return True
        return user_id in self.allowed_users

    def _is_dm(self, update: Update) -> bool:
        return update.effective_chat.type == "private"

    def _is_mentioned(self, update: Update) -> bool:
        message = update.effective_message
        if message.reply_to_message and message.reply_to_message.from_user:
            if message.reply_to_message.from_user.id == self.app.bot.id:
                return True
        if message.entities:
            bot_username = f"@{self.app.bot.username}"
            for entity in message.entities:
                if entity.type == "mention":
                    mentioned = message.text[entity.offset : entity.offset + entity.length]
                    if mentioned.lower() == bot_username.lower():
                        return True
        return False

    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update.effective_user.id):
            await update.message.reply_text("Not authorized.")
            return
        await update.message.reply_text("Hey. What's up?")

    async def _handle_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = str(update.effective_chat.id)
        await self.agent.clear_history(chat_id)
        await update.message.reply_text("Conversation history cleared.")

    async def _handle_whoami(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        await update.message.reply_text(
            f"User ID: {user.id}\nUsername: @{user.username or 'none'}"
        )

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        chat_id = str(update.effective_chat.id)
        is_dm = self._is_dm(update)
        message_text = update.message.text

        if is_dm and not self._is_allowed(user_id):
            return

        if not is_dm and not self._is_mentioned(update):
            return

        # Strip bot mention in groups
        if not is_dm and self.app.bot.username:
            message_text = message_text.replace(f"@{self.app.bot.username}", "").strip()

        # Send typing immediately
        await update.effective_chat.send_action("typing")

        # Build handler and enqueue
        async def process():
            # Typing keepalive — refresh every 4 seconds until done
            done = asyncio.Event()

            async def typing_keepalive():
                while not done.is_set():
                    await asyncio.sleep(4)
                    if not done.is_set():
                        try:
                            await update.effective_chat.send_action("typing")
                        except Exception:
                            pass

            keepalive_task = asyncio.create_task(typing_keepalive())

            try:
                response = await self.agent.reply(
                    message=message_text,
                    chat_id=chat_id,
                    is_main_session=is_dm,
                )

                if len(response) <= 4096:
                    await update.message.reply_text(response)
                else:
                    for i in range(0, len(response), 4096):
                        await update.message.reply_text(response[i : i + 4096])

            except Exception:
                log.exception("Error processing message from chat %s", chat_id)
                await update.message.reply_text(
                    "Something went wrong. Try again in a moment."
                )
            finally:
                done.set()
                keepalive_task.cancel()
                try:
                    await keepalive_task
                except asyncio.CancelledError:
                    pass

        await self._queue.enqueue(chat_id, process)
