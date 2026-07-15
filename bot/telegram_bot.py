import asyncio
import logging
import time
from collections import defaultdict
from uuid import uuid4

from pyrogram import Client, enums, filters
from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Message,
)

from bot.config import Config
from bot.database import Database
from bot.openai_helper import OpenAIHelper

logger = logging.getLogger(__name__)


class TelegramBot:
    def __init__(self, config: Config, openai: OpenAIHelper, db: Database):
        self.config = config
        self.openai = openai
        self.db = db
        self.app = Client(
            "ggqtbot",
            api_id=config.api_id,
            api_hash=config.api_hash,
            bot_token=config.bot_token,
        )
        self.inline_queries_cache: dict[str, str] = {}
        self._rate_limits: dict[int, list[float]] = defaultdict(list)
        self._register_handlers()

    def _register_handlers(self):
        self.app.on_message(filters.command("start") & filters.private)(self._start)
        self.app.on_message(filters.command("reset") & filters.private)(self._reset)
        self.app.on_message(filters.command("model"))(self._model)
        self.app.on_message(filters.command("models"))(self._models)
        self.app.on_message(filters.command("adduser"))(self._adduser)
        self.app.on_message(filters.command("removeuser"))(self._removeuser)
        self.app.on_message(filters.command("users"))(self._list_users)
        self.app.on_message(filters.command("help"))(self._help)
        self.app.on_message(
            filters.text & filters.private & ~filters.command(["start", "reset", "model", "models", "help", "adduser", "removeuser", "users"])
        )(self._handle_message)
        self.app.on_message(filters.text & filters.group)(self._handle_group_message)
        self.app.on_message(filters.sticker)(self._handle_sticker)
        self.app.on_inline_query()(self._handle_inline)
        self.app.on_callback_query(filters.regex(r"^gen:"))(self._handle_generate_callback)

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self.config.admin_user_ids

    async def _is_allowed(self, user_id: int) -> bool:
        if self._is_admin(user_id):
            return True
        return await self.db.is_allowed_user(user_id)

    async def _start(self, client: Client, message: Message):
        await message.reply_text(
            "Hey! Send me a message and I'll respond using AI.\n\n"
            "Commands:\n"
            "/reset - Clear conversation history\n"
            "/model <name> - Switch AI model\n"
            "/models - List available models\n"
            "/help - Show this message"
        )

    async def _reset(self, client: Client, message: Message):
        await self.db.reset_conversation(message.chat.id)
        await message.reply_text("Conversation reset.")

    async def _help(self, client: Client, message: Message):
        await self._start(client, message)

    async def _adduser(self, client: Client, message: Message):
        if not self._is_admin(message.from_user.id):
            await message.reply_text("Only admins can add users.")
            return
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply_text("Usage: /adduser <user_id>")
            return
        try:
            target_id = int(parts[1].strip())
        except ValueError:
            await message.reply_text("User ID must be a number.")
            return
        await self.db.add_allowed_user(target_id)
        await message.reply_text(f"User {target_id} has been granted access.")

    async def _removeuser(self, client: Client, message: Message):
        if not self._is_admin(message.from_user.id):
            await message.reply_text("Only admins can remove users.")
            return
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply_text("Usage: /removeuser <user_id>")
            return
        try:
            target_id = int(parts[1].strip())
        except ValueError:
            await message.reply_text("User ID must be a number.")
            return
        await self.db.remove_allowed_user(target_id)
        await message.reply_text(f"User {target_id} access revoked.")

    async def _list_users(self, client: Client, message: Message):
        if not self._is_admin(message.from_user.id):
            await message.reply_text("Only admins can view users.")
            return
        users = await self.db.get_allowed_users()
        if not users:
            await message.reply_text("No allowed users yet.")
            return
        text = "Allowed users:\n\n"
        for u in users:
            uid = u.get("user_id")
            uname = u.get("username", "unknown")
            text += f"• {uid} (@{uname})\n"
        await message.reply_text(text)

    async def _deny_access(self, message: Message):
        await message.reply_text(
            "You don't have access to this bot.\n"
            "Contact @alex5402 to get access."
        )

    def _is_rate_limited(self, user_id: int, max_requests: int = 10, window: int = 60) -> bool:
        """Check if user exceeded rate limit (default: 10 requests per 60 seconds)."""
        now = time.time()
        timestamps = self._rate_limits[user_id]
        # Remove old timestamps outside the window
        self._rate_limits[user_id] = [t for t in timestamps if now - t < window]
        if len(self._rate_limits[user_id]) >= max_requests:
            return True
        self._rate_limits[user_id].append(now)
        return False

    async def _models(self, client: Client, message: Message):
        if not await self._is_allowed(message.from_user.id):
            await self._deny_access(message)
            return
        models = await self.openai.get_models()
        current = self.openai.get_current_model()
        text = "Available models:\n\n"
        for m in models:
            marker = " (current)" if m == current else ""
            text += f"• `{m}`{marker}\n"
        text += f"\nUse /model <name> to switch."
        await message.reply_text(text)

    async def _model(self, client: Client, message: Message):
        if not await self._is_allowed(message.from_user.id):
            await self._deny_access(message)
            return
        user_id = message.from_user.id
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            user_model = await self.db.get_user_model(user_id)
            current = user_model or self.openai.get_current_model()
            await message.reply_text(f"Current model: `{current}`\nUse /model <name> to switch.")
            return
        model_name = parts[1].strip()
        models = await self.openai.get_models()
        if model_name not in models:
            await message.reply_text(f"Model `{model_name}` not found. Use /models to list available.")
            return
        await self.db.set_user_model(user_id, model_name)
        await message.reply_text(f"Switched to `{model_name}`.")

    async def _handle_message(self, client: Client, message: Message):
        if not await self._is_allowed(message.from_user.id):
            await self._deny_access(message)
            return
        await self._respond(message, message.text)

    async def _handle_group_message(self, client: Client, message: Message):
        if not message.text:
            return

        me = await client.get_me()
        bot_username = me.username
        bot_id = me.id
        trigger = self.config.group_trigger_keyword

        # Check if this is a reply to the bot's message
        is_reply_to_bot = (
            message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.id == bot_id
        )

        # Respond if: trigger keyword, mentions bot, or replies to bot
        if not (
            message.text.startswith(trigger)
            or (bot_username and f"@{bot_username}" in message.text)
            or is_reply_to_bot
        ):
            return
        if not await self._is_allowed(message.from_user.id):
            await self._deny_access(message)
            return
        text = message.text
        if text.startswith(trigger):
            text = text[len(trigger):].strip()
        text = text.replace(f"@{bot_username}", "").strip() if bot_username else text
        if not text:
            return

        # Use feminine persona when replying to bot's message
        if is_reply_to_bot:
            await self._respond(message, text, persona="woman")
        else:
            await self._respond(message, text)

    async def _handle_sticker(self, client: Client, message: Message):
        """Reply to stickers with a related sticker from the configured pack."""
        if not message.sticker:
            return
        emoji = message.sticker.emoji
        if not emoji:
            return

        try:
            import random
            stickers = await client.get_stickers(self.config.sticker_pack)
            # Find stickers matching the same emoji
            matching = [s for s in stickers if s.emoji == emoji]
            if not matching:
                # Fallback: pick a random sticker from the pack
                matching = stickers
            if matching:
                sticker = random.choice(matching)
                await message.reply_sticker(sticker.file_id)
        except Exception as e:
            logger.error(f"Sticker handler error: {e}")

    async def _respond(self, message: Message, user_text: str, persona: str | None = None):
        """Generate and stream an AI response."""
        chat_id = message.chat.id
        user_id = message.from_user.id
        await self.db.track_user(user_id, message.from_user.username)

        # Rate limit check
        if not self._is_admin(user_id) and self._is_rate_limited(user_id):
            await message.reply_text("You're sending too fast. Please wait a moment.")
            return

        # Resolve per-user model
        user_model = await self.db.get_user_model(user_id)

        # Choose system prompt based on persona
        if persona == "woman":
            system_prompt = (
                "You are a real human woman chatting casually on Telegram. "
                "You're warm, playful, witty, and natural. You use casual language, "
                "short sentences, and sometimes emojis. Never reveal you are an AI. "
                "Respond like a real person would in a group chat."
            )
        else:
            system_prompt = self.config.openai_system_prompt

        # Build messages with history
        history = await self.db.get_conversation(chat_id)
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_text})

        # Send placeholder
        reply = await message.reply_text("...")

        # Stream response with periodic edits
        full_response = ""
        last_edit = time.time()
        try:
            async for chunk in self.openai.chat_completion_stream(messages, model=user_model):
                full_response += chunk
                if time.time() - last_edit >= self.config.stream_update_interval:
                    await reply.edit_text(full_response + " ▌", parse_mode=enums.ParseMode.DISABLED)
                    last_edit = time.time()

            # Final edit
            if full_response:
                await reply.edit_text(full_response, parse_mode=enums.ParseMode.DISABLED)
            else:
                await reply.edit_text("(empty response)", parse_mode=enums.ParseMode.DISABLED)
        except Exception as e:
            logger.error(f"Error generating response: {e}")
            await reply.edit_text("Something went wrong. Try again later.", parse_mode=enums.ParseMode.DISABLED)
            return

        # Save to history
        await self.db.add_message(chat_id, "user", user_text)
        await self.db.add_message(chat_id, "assistant", full_response)

    async def _handle_inline(self, client: Client, inline_query: InlineQuery):
        query = inline_query.query.strip()
        if not query:
            return
        if not await self._is_allowed(inline_query.from_user.id):
            return

        result_id = str(uuid4())
        # Store query with user_id so callback can look it up
        callback_data = f"gen:{result_id}"
        self.inline_queries_cache[result_id] = {
            "query": query,
            "user_id": inline_query.from_user.id,
        }

        # Show query text with a "Generate" button — no auto-generation
        results = [
            InlineQueryResultArticle(
                id=result_id,
                title="Ask AI",
                description=query[:100],
                input_message_content=InputTextMessageContent(query),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🤖 Generate", callback_data=callback_data)]
                ]),
            )
        ]
        await inline_query.answer(results, cache_time=0)

    async def _handle_generate_callback(self, client: Client, callback_query):
        """Called when user clicks the Generate button on an inline message."""
        data = callback_query.data  # "gen:<result_id>"
        result_id = data.split(":", 1)[1]
        cached = self.inline_queries_cache.pop(result_id, None)

        if not cached:
            await callback_query.answer("Query expired, please try again.", show_alert=True)
            return

        query = cached["query"]
        user_id = cached["user_id"]
        inline_message_id = callback_query.inline_message_id

        # Rate limit check
        if not self._is_admin(user_id) and self._is_rate_limited(user_id):
            await callback_query.answer("Slow down! Try again in a minute.", show_alert=True)
            return

        # Acknowledge the button click
        await callback_query.answer("Generating...")

        # Update message to show generating state
        await client.edit_inline_text(
            inline_message_id=inline_message_id,
            text=f"{query}\n\n⏳ Generating...",
            parse_mode=enums.ParseMode.DISABLED,
            reply_markup=None,
        )

        # Resolve per-user model
        user_model = await self.db.get_user_model(user_id)

        try:
            inline_system_prompt = (
                "You have two jobs depending on the input:\n"
                "1. If the user wants creative content (poem, story, joke, quote, etc.), generate it.\n"
                "2. If the user writes a casual message (greeting, thought, opinion), rewrite it to sound better and more expressive while keeping the same meaning.\n"
                "In BOTH cases: output ONLY the final text. No labels, no explanation, no replies, no conversation. "
                "Never answer or respond to the user's text — only transform or generate."
            )
            messages = [
                {"role": "system", "content": inline_system_prompt},
                {"role": "user", "content": query},
            ]
            response = await self.openai.chat_completion(messages, model=user_model)
            if response:
                await client.edit_inline_text(
                    inline_message_id=inline_message_id,
                    text=response,
                    parse_mode=enums.ParseMode.DISABLED,
                    reply_markup=None,
                )
            else:
                await client.edit_inline_text(
                    inline_message_id=inline_message_id,
                    text=f"{query}\n\n(empty response)",
                    parse_mode=enums.ParseMode.DISABLED,
                    reply_markup=None,
                )
        except Exception as e:
            logger.error(f"Inline generate error: {e}")
            try:
                await client.edit_inline_text(
                    inline_message_id=inline_message_id,
                    text=f"{query}\n\n❌ Something went wrong. Try again later.",
                    parse_mode=enums.ParseMode.DISABLED,
                    reply_markup=None,
                )
            except Exception:
                pass

    async def run(self):
        await self.db.init()
        await self.openai.get_models()
        logger.info(f"Using model: {self.openai.get_current_model()}")
        await self.app.start()
        logger.info("Bot started.")
        await asyncio.Event().wait()  # run forever
