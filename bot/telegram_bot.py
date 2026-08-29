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
        self.app.on_callback_query(filters.regex(r"^setmodel:"))(self._handle_set_model_callback)

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self.config.admin_user_ids

    async def _is_allowed(self, user_id: int) -> bool:
        if self.config.allow_all_users:
            return True
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

    def _build_models_keyboard(self, models: list[str], current_model: str) -> InlineKeyboardMarkup:
        """Build an inline keyboard menu for available models."""
        buttons = []
        for idx, m in enumerate(models):
            is_active = (m == current_model)
            prefix = "✅ " if is_active else "🤖 "
            label = f"{prefix}{m}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"setmodel:{idx}")])
        return InlineKeyboardMarkup(buttons)

    async def _models(self, client: Client, message: Message):
        if not await self._is_allowed(message.from_user.id):
            await self._deny_access(message)
            return
        models = await self.openai.get_models()
        user_id = message.from_user.id
        user_model = await self.db.get_user_model(user_id)
        current = user_model or self.openai.get_current_model()

        text = (
            "🤖 **Select Your AI Model**\n\n"
            f"Active model: `{current}`\n\n"
            "Click any model below to select it, or use `/model <name>`:"
        )
        markup = self._build_models_keyboard(models, current)
        await message.reply_text(text, reply_markup=markup)

    async def _model(self, client: Client, message: Message):
        if not await self._is_allowed(message.from_user.id):
            await self._deny_access(message)
            return
        user_id = message.from_user.id
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await self._models(client, message)
            return
        model_name = parts[1].strip()
        models = await self.openai.get_models()
        if model_name not in models:
            await message.reply_text(f"Model `{model_name}` not found. Use /models to list available.")
            return
        await self.db.set_user_model(user_id, model_name)
        await message.reply_text(f"Switched model to `{model_name}`.")

    async def _handle_set_model_callback(self, client: Client, callback_query):
        user_id = callback_query.from_user.id
        if not await self._is_allowed(user_id):
            await callback_query.answer("Access denied.", show_alert=True)
            return

        data = callback_query.data  # "setmodel:<idx>"
        try:
            idx = int(data.split(":", 1)[1])
            models = await self.openai.get_models()
            if idx < 0 or idx >= len(models):
                await callback_query.answer("Model not found.", show_alert=True)
                return
            target_model = models[idx]
        except (ValueError, IndexError):
            await callback_query.answer("Invalid selection.", show_alert=True)
            return

        await self.db.set_user_model(user_id, target_model)
        await callback_query.answer(f"Switched model to: {target_model}")

        # Delete the buttons message
        try:
            await callback_query.message.delete()
        except Exception as e:
            logger.debug(f"Failed to delete models message: {e}")

        # Send confirmation message
        try:
            await client.send_message(
                chat_id=callback_query.message.chat.id,
                text=f"✅ Switched model to `{target_model}`."
            )
        except Exception as e:
            logger.debug(f"Failed to send model confirmation message: {e}")

    async def _handle_message(self, client: Client, message: Message):
        if not await self._is_allowed(message.from_user.id):
            await self._deny_access(message)
            return
        await self._respond(message, message.text)

    async def _handle_group_message(self, client: Client, message: Message):
        if not message.text or not message.from_user:
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
        text_lower = message.text.lower()
        if not (
            message.text.startswith(trigger)
            or (bot_username and f"@{bot_username.lower()}" in text_lower)
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
        if not text and not message.reply_to_message:
            return

        # Use feminine persona when replying to bot's message
        if is_reply_to_bot:
            await self._respond(message, text or "[replied to bot]", persona="woman")
        else:
            await self._respond(message, text or "[replied to message]")

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
        """Generate and stream an AI response with full chat, user profile, and reply context."""
        chat_id = message.chat.id
        user_id = message.from_user.id if message.from_user else 0
        username = message.from_user.username if message.from_user else None

        if user_id:
            await self.db.track_user(user_id, username)

        # Rate limit check
        if user_id and not self._is_admin(user_id) and self._is_rate_limited(user_id):
            await message.reply_text("You're sending too fast. Please wait a moment.")
            return

        # Resolve per-user model
        user_model = await self.db.get_user_model(user_id) if user_id else None

        # 1. Chat Context
        chat_title = message.chat.title or message.chat.first_name or "Private Chat"
        chat_type_str = str(message.chat.type).split(".")[-1].upper() if hasattr(message.chat, "type") else "CHAT"
        chat_context = f"Chat Name: '{chat_title}' (ID: {chat_id}, Type: {chat_type_str})"

        # 2. Sender Profile Context (Name, ID, Username, Bio)
        sender = message.from_user
        sender_id = sender.id if sender else 0
        sender_name = f"{sender.first_name or ''} {sender.last_name or ''}".strip() if sender else "Unknown"
        sender_uname_str = f"@{sender.username}" if sender and sender.username else "None"
        sender_bio = "Not set / Private"

        if sender and sender.id:
            try:
                user_chat = await self.app.get_chat(sender.id)
                if user_chat and user_chat.bio:
                    sender_bio = user_chat.bio
            except Exception:
                pass

        user_context = (
            f"User Speaking with You:\n"
            f"- Name: {sender_name}\n"
            f"- User ID: {sender_id}\n"
            f"- Username: {sender_uname_str}\n"
            f"- Bio: {sender_bio}"
        )

        # 3. Replied Message Context
        reply_context = ""
        if message.reply_to_message:
            r_msg = message.reply_to_message
            r_sender = r_msg.from_user
            r_name = f"{r_sender.first_name or ''} {r_sender.last_name or ''}".strip() if r_sender else "Unknown"
            r_uname_str = f"@{r_sender.username}" if r_sender and r_sender.username else "None"
            r_id = r_sender.id if r_sender else "Unknown"
            r_text = r_msg.text or r_msg.caption or "[Media / Non-text content]"

            reply_context = (
                f"\n\nContext - Replying to message:\n"
                f"- Original Author: {r_name} (ID: {r_id}, Username: {r_uname_str})\n"
                f"- Original Content: \"{r_text}\""
            )

        context_prompt = (
            f"### Current Context:\n"
            f"- {chat_context}\n"
            f"- {user_context}"
            f"{reply_context}\n\n"
            "Use this context naturally to personalize your output and accurately address replied messages."
        )

        # Choose system prompt based on persona
        if persona == "woman":
            base_prompt = (
                "You are a real human woman chatting casually on Telegram. "
                "You're warm, playful, witty, and natural. You use casual language, "
                "short sentences, and sometimes emojis. Never reveal you are an AI. "
                "Respond like a real person would in a group chat."
            )
        else:
            base_prompt = self.config.openai_system_prompt

        system_prompt = f"{base_prompt}\n\n{context_prompt}"

        # Format user prompt with speaker info
        formatted_user_input = f"[{sender_name} ({sender_uname_str})]: {user_text}"

        # Build messages with history
        history = await self.db.get_conversation(chat_id)
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": formatted_user_input})

        # Send typing action status
        try:
            await self.app.send_chat_action(chat_id, enums.ChatAction.TYPING)
        except Exception as e:
            logger.debug(f"Failed to send typing action: {e}")

        # Send placeholder
        reply = await message.reply_text("...")

        # Stream response with periodic edits & typing action refresh
        full_response = ""
        actual_model_used = user_model or self.openai.get_current_model()
        last_edit = time.time()
        last_typing = time.time()
        try:
            async for chunk, model_used in self.openai.chat_completion_stream(messages, model=user_model):
                full_response += chunk
                actual_model_used = model_used
                now = time.time()
                if now - last_typing >= 4.0:
                    try:
                        await self.app.send_chat_action(chat_id, enums.ChatAction.TYPING)
                    except Exception:
                        pass
                    last_typing = now
                if now - last_edit >= self.config.stream_update_interval:
                    await reply.edit_text(full_response + " ▌", parse_mode=enums.ParseMode.DISABLED)
                    last_edit = now

            # Final edit
            if full_response:
                await reply.edit_text(full_response, parse_mode=enums.ParseMode.DISABLED)
            else:
                await reply.edit_text("(empty response)", parse_mode=enums.ParseMode.DISABLED)

            # Send second message ONLY if user's requested model failed and fallback was triggered
            if user_model and actual_model_used != user_model:
                await message.reply_text(
                    f"💡 Note: Your selected model `{user_model}` was unavailable or failed, "
                    f"so we used the active fallback model `{actual_model_used}` instead. "
                    "You can switch models using /models."
                )
        except Exception as e:
            logger.error(f"Error generating response: {e}")
            await reply.edit_text("Something went wrong. Try again later.", parse_mode=enums.ParseMode.DISABLED)
            return

        # Save formatted input and response to history
        await self.db.add_message(chat_id, "user", formatted_user_input)
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

        # Only the user who sent the inline query can click Generate
        if callback_query.from_user.id != user_id:
            self.inline_queries_cache[result_id] = cached  # restore cache for the real user
            await callback_query.answer("Only the sender can generate.", show_alert=True)
            return

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
        working_model = await self.openai.find_working_default_model()
        logger.info(f"Active working default model: {working_model}")
        await self.app.start()
        logger.info("Bot started.")
        await asyncio.Event().wait()  # run forever
