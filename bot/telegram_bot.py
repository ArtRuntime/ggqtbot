import asyncio
import logging
import random
import re
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

from datetime import datetime

from bot.config import Config
from bot.database import Database
from bot.openai_helper import OpenAIHelper
from bot.web_search import WebSearch

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
        self._addapi_sessions: dict[int, dict] = {}
        self._last_random_chat: dict[int, float] = defaultdict(float)
        self._register_handlers()

    def _register_handlers(self):
        self.app.on_message(filters.command("start") & filters.private)(self._start)
        self.app.on_message(filters.command("reset") & filters.private)(self._reset)
        self.app.on_message(filters.command("cancel") & filters.private)(self._cancel_session)
        self.app.on_message(filters.command("model"))(self._model)
        self.app.on_message(filters.command("models"))(self._models)
        self.app.on_message(filters.command("adduser"))(self._adduser)
        self.app.on_message(filters.command("removeuser"))(self._removeuser)
        self.app.on_message(filters.command("users"))(self._list_users)
        self.app.on_message(filters.command("addapi"))(self._addapi)
        self.app.on_message(filters.command("removeapi"))(self._removeapi)
        self.app.on_message(filters.command("apis"))(self._list_apis)
        self.app.on_message(filters.command("search"))(self._search)
        self.app.on_message(filters.command("help"))(self._help)
        self.app.on_message(
            filters.text & filters.private & ~filters.command(["start", "reset", "cancel", "model", "models", "help", "adduser", "removeuser", "users", "addapi", "removeapi", "apis", "search"])
        )(self._handle_message)
        self.app.on_message(filters.text & filters.group)(self._handle_group_message)
        self.app.on_message(filters.sticker)(self._handle_sticker)
        self.app.on_inline_query()(self._handle_inline)
        self.app.on_callback_query(filters.regex(r"^gen:"))(self._handle_generate_callback)
        self.app.on_callback_query(filters.regex(r"^models_page:"))(self._handle_models_page_callback)
        self.app.on_callback_query(filters.regex(r"^models_cancel$"))(self._handle_models_cancel_callback)
        self.app.on_callback_query(filters.regex(r"^setmodel:"))(self._handle_set_model_callback)
        self.app.on_callback_query(filters.regex(r"^removeapi:"))(self._handle_remove_api_callback)

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self.config.admin_user_ids

    @staticmethod
    def _sanitize_utf8(text: str) -> str:
        """Sanitize text by removing invalid Unicode surrogate characters (U+D800-U+DFFF)."""
        if not text:
            return ""
        return re.sub(r'[\uD800-\uDFFF]', '', text)

    @staticmethod
    def _filter_links(text: str) -> str:
        """Format domain names with a space before extension (e.g. instagram .com) to prevent Telegram auto-hyperlinking."""
        if not text:
            return ""
        text = re.sub(r'[\uD800-\uDFFF]', '', text)
        cleaned = re.sub(r'https?://\S+|www\.\S+|t\.me/\S+', '', text)
        cleaned = re.sub(r'\[([^\]]*)\]\(\s*\)', r'\1', cleaned)
        cleaned = re.sub(
            r'\b([a-zA-Z0-9-]+)\.(com|org|net|io|me|ai|co|app|dev|xyz|info|edu|gov)\b',
            r'\1 .\2',
            cleaned,
            flags=re.IGNORECASE,
        )
        return cleaned.strip()

    async def _is_bot_admin_in_chat(self, chat_id: int) -> bool:
        """Check if the bot has administrator status in the given chat."""
        try:
            member = await self.app.get_chat_member(chat_id, "me")
            return member.status in (enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER)
        except Exception as e:
            logger.debug(f"Failed to check bot admin status in chat {chat_id}: {e}")
            return False

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
        user_id = message.from_user.id
        is_admin = self._is_admin(user_id)

        text = (
            "🤖 **GGQT BOT HELP & COMMANDS**\n\n"
            "💬 **Chat & Interaction**\n"
            "• Direct message me anytime to start chatting.\n"
            "• In groups, trigger me with `/chat <message>` or reply directly to my messages.\n"
            "• Mention or reply to existing messages for contextual conversations.\n\n"
            "⚙️ **User Commands**\n"
            "• `/start` — Welcome message & bot status\n"
            "• `/models` — Interactive menu to browse & pick AI models (9 per page)\n"
            "• `/model <name>` — Direct model selector\n"
            "• `/reset` — Clear your conversation memory history\n"
            "• `/help` — Display this help menu\n\n"
        )

        if is_admin:
            text += (
                "👑 **Admin Commands**\n"
                "• `/addapi` — Interactive wizard to add a custom OpenAI-compatible API\n"
                "• `/removeapi` — Remove custom API endpoints via interactive buttons\n"
                "• `/apis` — List all registered custom API endpoints\n"
                "• `/adduser <id>` — Grant user access\n"
                "• `/removeuser <id>` — Revoke user access\n"
                "• `/users` — List allowed users\n"
                "• `/cancel` — Cancel an active setup session\n\n"
            )

        text += "💡 **Pro Tip**: Use `/models` to switch between OpenRouter free models & custom endpoints!"
        await message.reply_text(text)

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

    async def _cancel_session(self, client: Client, message: Message):
        user_id = message.from_user.id
        if user_id in self._addapi_sessions:
            del self._addapi_sessions[user_id]
            await message.reply_text("❌ API setup session cancelled.")
        else:
            await message.reply_text("No active setup session to cancel.")

    async def _addapi(self, client: Client, message: Message):
        if not self._is_admin(message.from_user.id):
            await message.reply_text("Only admins can add API endpoints.")
            return

        parts = message.text.split(maxsplit=3)
        # Direct one-shot mode if arguments supplied: /addapi <url> [api_key] [name]
        if len(parts) >= 2 and parts[1].startswith(("http://", "https://")):
            url = parts[1].strip()
            api_key = parts[2].strip() if len(parts) > 2 else ""
            name = parts[3].strip() if len(parts) > 3 else ""
            await self._save_api_endpoint(message, url, api_key, name)
            return

        # Start interactive session
        user_id = message.from_user.id
        self._addapi_sessions[user_id] = {
            "step": "awaiting_url",
            "started_at": time.time(),
        }
        await message.reply_text(
            "🌐 **Interactive API Endpoint Setup** (Step 1/3)\n\n"
            "Please send the OpenAI-compatible API base URL.\n"
            "*(Must include `/v1` endpoint, e.g. `https://api.openai.com/v1` or `https://my-api.com/v1`)*:\n\n"
            "*(Type `/cancel` to cancel. Session expires after 3 minutes of inactivity)*"
        )

    async def _handle_addapi_step(self, message: Message) -> bool:
        user_id = message.from_user.id
        session = self._addapi_sessions.get(user_id)
        if not session:
            return False

        now = time.time()
        started_at = session.get("started_at", now)
        if now - started_at > 180:
            del self._addapi_sessions[user_id]
            await message.reply_text(
                "⏰ **API Setup Session Expired**\n\n"
                "The setup session timed out due to inactivity (3 minutes limit).\n"
                "Please type `/addapi` to start a new session."
            )
            return True

        session["started_at"] = now
        text = message.text.strip()
        step = session.get("step")

        if text.lower() == "/cancel":
            del self._addapi_sessions[user_id]
            await message.reply_text("❌ API setup session cancelled.")
            return True

        if step == "awaiting_url":
            if not text.startswith(("http://", "https://")):
                await message.reply_text("⚠️ Invalid URL. URL must start with `http://` or `https://`. Please try again:")
                return True

            url = text
            if not url.endswith("/v1") and "/v1/" not in url:
                url = url.rstrip("/") + "/v1"

            session["url"] = url
            session["step"] = "awaiting_key"
            await message.reply_text(
                f"URL set to: `{url}`\n\n"
                "🔑 **API Key** (Step 2/3)\n\n"
                "Please send the API Key for this endpoint:\n\n"
                "*(Reply `skip` or `none` if no API key is required)*"
            )
            return True

        elif step == "awaiting_key":
            api_key = "" if text.lower() in ("skip", "none", "no") else text
            session["api_key"] = api_key
            session["step"] = "awaiting_name"
            key_preview = "No Key" if not api_key else f"`{api_key[:4]}...`"
            await message.reply_text(
                f"API Key set ({key_preview}).\n\n"
                "🏷️ **API Friendly Name** (Step 3/3)\n\n"
                "Please send a name for this API endpoint:\n\n"
                "*(Reply `skip` or `auto` to auto-generate a name)*"
            )
            return True

        elif step == "awaiting_name":
            url = session.get("url", "")
            api_key = session.get("api_key", "")
            del self._addapi_sessions[user_id]
            name = "" if text.lower() in ("skip", "auto") else text
            await self._save_api_endpoint(message, url, api_key, name)
            return True

        return False

    async def _save_api_endpoint(self, message: Message, url: str, api_key: str = "", name: str = ""):
        # Ensure /v1 endpoint
        if not url.endswith("/v1") and "/v1/" not in url:
            url = url.rstrip("/") + "/v1"

        # Auto-name generation if name is omitted
        if not name:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            host = parsed.netloc.replace(":", "-").replace(".", "-").lower()
            name = host or "custom-api"

        await self.db.add_api_endpoint(name, url, api_key)
        self.openai._models_fetched_at = None

        key_info = f"`{api_key[:6]}...`" if api_key else "*None*"
        await message.reply_text(
            "✅ **API Endpoint Successfully Added!**\n\n"
            f"• **Name**: `{name}`\n"
            f"• **Base URL**: `{url}`\n"
            f"• **API Key**: {key_info}\n\n"
            "Discovered models are now active and listed in `/models`!"
        )

    async def _removeapi(self, client: Client, message: Message):
        if not self._is_admin(message.from_user.id):
            await message.reply_text("Only admins can remove API endpoints.")
            return

        parts = message.text.split(maxsplit=1)
        if len(parts) >= 2:
            name = parts[1].strip()
            removed = await self.db.remove_api_endpoint(name)
            self.openai._models_fetched_at = None
            if removed:
                await message.reply_text(f"✅ API endpoint `{name}` removed.")
            else:
                await message.reply_text(f"API endpoint `{name}` not found.")
            return

        # If no argument supplied, show interactive endpoint buttons to remove!
        endpoints = await self.db.get_api_endpoints()
        if not endpoints:
            await message.reply_text(
                "ℹ️ **No Custom API Endpoints**\n\n"
                "There are no custom API endpoints registered to remove.\n"
                "OpenRouter free models are active by default."
            )
            return

        buttons = []
        for ep in endpoints:
            ep_name = ep.get("name", "unknown")
            buttons.append([InlineKeyboardButton(f"🗑️ Remove {ep_name}", callback_data=f"removeapi:{ep_name}")])
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="models_cancel")])

        markup = InlineKeyboardMarkup(buttons)
        await message.reply_text(
            "🗑️ **Remove Custom API Endpoint**\n\n"
            "Click any endpoint below to remove it, or use `/removeapi <name>`:",
            reply_markup=markup,
        )

    async def _handle_remove_api_callback(self, client: Client, callback_query):
        if not self._is_admin(callback_query.from_user.id):
            await callback_query.answer("Only admins can remove API endpoints.", show_alert=True)
            return

        data = callback_query.data  # "removeapi:<name>"
        try:
            name = data.split(":", 1)[1].strip()
            removed = await self.db.remove_api_endpoint(name)
            self.openai._models_fetched_at = None
        except Exception as e:
            logger.error(f"Failed to remove API endpoint via callback: {e}")
            await callback_query.answer("Failed to remove endpoint.", show_alert=True)
            return

        try:
            await callback_query.message.delete()
        except Exception:
            pass

        if removed:
            await callback_query.answer(f"Removed API endpoint '{name}'", show_alert=False)
            await client.send_message(
                chat_id=callback_query.message.chat.id,
                text=f"✅ API endpoint `{name}` successfully removed."
            )
        else:
            await callback_query.answer(f"API endpoint '{name}' not found.", show_alert=True)

    async def _list_apis(self, client: Client, message: Message):
        if not self._is_admin(message.from_user.id):
            await message.reply_text("Only admins can view API endpoints.")
            return
        endpoints = await self.db.get_api_endpoints()
        if not endpoints:
            await message.reply_text("No custom API endpoints registered yet. OpenRouter free models are active by default.")
            return
        text = "Registered Custom API Endpoints:\n\n"
        for ep in endpoints:
            text += f"• `{ep.get('name')}`: `{ep.get('base_url')}`\n"
        await message.reply_text(text)

    async def _search(self, client: Client, message: Message):
        if not await self._is_allowed(message.from_user.id):
            await self._deny_access(message)
            return
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply_text("Usage: /search <query>")
            return
        query = parts[1].strip()
        status_msg = await message.reply_text("🔍 Searching the web...")
        results = await WebSearch.search(query, max_results=4)
        if not results:
            await status_msg.edit_text(f"No web results found for `{query}`.")
            return

        chat_type = str(message.chat.type).split(".")[-1].lower() if hasattr(message.chat, "type") else "private"
        is_private = (chat_type == "private")
        is_admin = await self._is_bot_admin_in_chat(message.chat.id)
        allow_links = is_private or is_admin

        text = f"🌐 **Search Results for** `{query}`:\n\n"
        for idx, r in enumerate(results, 1):
            if allow_links and r.get("link"):
                text += f"{idx}. **[{r['title']}]({r['link']})**\n{r['snippet']}\n\n"
            else:
                clean_title = self._filter_links(r['title'])
                clean_snippet = self._filter_links(r['snippet'])
                text += f"{idx}. **{clean_title}**\n{clean_snippet}\n\n"

        await status_msg.edit_text(text)

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

    def _build_models_keyboard(self, models: list[str], current_model: str, page: int = 0) -> InlineKeyboardMarkup:
        """Build a paginated inline keyboard menu showing 9 models per page with Prev, Cancel, and Next controls."""
        page_size = 9
        total_models = len(models)
        total_pages = (total_models + page_size - 1) // page_size if total_models > 0 else 1
        page = max(0, min(page, total_pages - 1))

        start_idx = page * page_size
        end_idx = min(start_idx + page_size, total_models)

        buttons = []
        for idx in range(start_idx, end_idx):
            m = models[idx]
            is_active = (m == current_model)
            prefix = "✅ " if is_active else "🤖 "
            label = f"{prefix}{m}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"setmodel:{idx}")])

        # Navigation row: Prev | Cancel | Next
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"models_page:{page - 1}"))

        nav_row.append(InlineKeyboardButton("❌ Cancel", callback_data="models_cancel"))

        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"models_page:{page + 1}"))

        buttons.append(nav_row)
        return InlineKeyboardMarkup(buttons)

    async def _models(self, client: Client, message: Message):
        if not await self._is_allowed(message.from_user.id):
            await self._deny_access(message)
            return
        models = await self.openai.get_models()
        user_id = message.from_user.id
        user_model = await self.db.get_user_model(user_id)
        current = user_model or self.openai.get_current_model()
        total_pages = (len(models) + 8) // 9 if models else 1

        text = (
            "🤖 **Select Your AI Model**\n\n"
            f"Active model: `{current}`\n"
            f"Page `1/{total_pages}` (Total: {len(models)} models)\n\n"
            "Click any model below to select it:"
        )
        markup = self._build_models_keyboard(models, current, page=0)
        await message.reply_text(text, reply_markup=markup)

    async def _handle_models_page_callback(self, client: Client, callback_query):
        user_id = callback_query.from_user.id
        if not await self._is_allowed(user_id):
            await callback_query.answer("Access denied.", show_alert=True)
            return

        try:
            page = int(callback_query.data.split(":", 1)[1])
        except ValueError:
            page = 0

        models = await self.openai.get_models()
        user_model = await self.db.get_user_model(user_id)
        current = user_model or self.openai.get_current_model()
        total_pages = (len(models) + 8) // 9 if models else 1

        text = (
            "🤖 **Select Your AI Model**\n\n"
            f"Active model: `{current}`\n"
            f"Page `{page + 1}/{total_pages}` (Total: {len(models)} models)\n\n"
            "Click any model below to select it:"
        )
        markup = self._build_models_keyboard(models, current, page=page)
        try:
            await callback_query.edit_message_text(text, reply_markup=markup)
        except Exception:
            await callback_query.answer()

    async def _handle_models_cancel_callback(self, client: Client, callback_query):
        try:
            await callback_query.message.delete()
        except Exception as e:
            logger.debug(f"Failed to delete models message on cancel: {e}")
        await callback_query.answer("Cancelled model selection.")

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
        if await self._handle_addapi_step(message):
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

        # Respond if: trigger keyword, mentions bot, replies to bot, OR random spontaneous chance
        text_lower = message.text.lower()
        is_triggered = (
            message.text.startswith(trigger)
            or (bot_username and f"@{bot_username.lower()}" in text_lower)
            or is_reply_to_bot
        )

        is_random_spontaneous = False
        if not is_triggered:
            chat_id = message.chat.id
            now = time.time()
            last_chat = self._last_random_chat[chat_id]
            if (
                now - last_chat >= self.config.random_group_chat_cooldown_seconds
                and random.random() < self.config.random_group_chat_chance
            ):
                is_random_spontaneous = True
                self._last_random_chat[chat_id] = now
                logger.info(f"Triggered spontaneous random group chat response in group {chat_id}")

        if not is_triggered and not is_random_spontaneous:
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

        # Use feminine persona when replying to bot's message or spontaneous chat
        if is_reply_to_bot or is_random_spontaneous:
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

        # 4. Check Bot Admin Status for Dynamic Link Permission
        is_private = (chat_type_str == "PRIVATE")
        is_bot_admin = await self._is_bot_admin_in_chat(chat_id)
        allow_links = is_private or is_bot_admin

        # 5. Real-time Date & Time Context
        now_dt = datetime.now()
        realtime_str = now_dt.strftime("%A, %B %d, %Y at %I:%M:%S %p")
        realtime_context = f"📅 Real-Time Current Date & Time: {realtime_str}"

        # 6. Dynamic Web Search Context
        search_keywords = ("search", "who won", "latest", "today", "weather", "news", "price", "score", "current", "what is happening", "when did", "find out")
        search_context = ""
        if any(k in user_text.lower() for k in search_keywords):
            search_results = await WebSearch.search(user_text, max_results=3)
            if search_results:
                if allow_links:
                    snippets = "\n".join([f"• [{r['title']}]({r['link']}): {r['snippet']}" if r.get('link') else f"• {r['title']}: {r['snippet']}" for r in search_results])
                else:
                    from bot.web_search import clean_text_no_links
                    snippets = "\n".join([f"• {clean_text_no_links(r['title'])}: {clean_text_no_links(r['snippet'])}" for r in search_results])
                search_context = f"\n\n🌐 Live Web Search Results:\n{snippets}"

        link_rule = (
            "LINK PERMISSION: Links/URLs ARE ALLOWED in this chat. You may include relevant URLs when helpful."
            if allow_links
            else "LINK PERMISSION: Full URLs (http:// or https://) ARE STRICTLY BANNED in this chat because bot is not an admin. When referring to website addresses, format them with a space before the extension like 'instagram .com' or 'google .com' so Telegram does not auto-link them."
        )

        context_prompt = (
            f"### Current Real-Time Context:\n"
            f"- {realtime_context}\n"
            f"- {chat_context}\n"
            f"- {user_context}\n"
            f"- {link_rule}"
            f"{reply_context}"
            f"{search_context}\n\n"
            "Use this real-time information naturally to provide precise, accurate, and up-to-date answers."
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
                    display_text = full_response if allow_links else self._filter_links(full_response)
                    display_text = self._sanitize_utf8(display_text)
                    if len(display_text) > 3900:
                        display_text = display_text[:3900]
                    await reply.edit_text(display_text + " ▌", parse_mode=enums.ParseMode.DISABLED)
                    last_edit = now

            # Final edit (sanitize UTF-8 surrogates, filter links if not admin, split if exceeding limit)
            final_text = full_response if allow_links else self._filter_links(full_response)
            final_text = self._sanitize_utf8(final_text)
            if not final_text:
                await reply.edit_text("(empty response)", parse_mode=enums.ParseMode.DISABLED)
            elif len(final_text) <= 4000:
                await reply.edit_text(final_text, parse_mode=enums.ParseMode.DISABLED)
            else:
                first_chunk = final_text[:4000]
                await reply.edit_text(first_chunk, parse_mode=enums.ParseMode.DISABLED)
                remaining = final_text[4000:]
                while remaining:
                    chunk = remaining[:4000]
                    remaining = remaining[4000:]
                    await message.reply_text(chunk, parse_mode=enums.ParseMode.DISABLED)

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
        # Launch background model discovery non-blockingly so bot starts instantly!
        asyncio.create_task(self.openai.start_background_model_discovery())
        logger.info(f"Bot starting immediately with default fallback model: '{self.openai.get_current_model()}'")
        await self.app.start()
        logger.info("Bot started and ready!")
        await asyncio.Event().wait()  # run forever
