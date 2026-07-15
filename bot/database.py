import logging
from datetime import datetime, timedelta

from motor.motor_asyncio import AsyncIOMotorClient

from bot.config import Config

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, config: Config):
        self.config = config
        self.client = AsyncIOMotorClient(config.mongodb_uri)
        self.db = self.client[config.mongodb_db]
        self.conversations = self.db["conversations"]
        self.users = self.db["users"]

    async def init(self):
        """Create indexes."""
        await self.conversations.create_index("chat_id")
        await self.conversations.create_index("updated_at")
        await self.users.create_index("user_id", unique=True)

    async def get_conversation(self, chat_id: int) -> list[dict]:
        """Get conversation history for a chat."""
        cutoff = datetime.now() - timedelta(
            minutes=self.config.max_conversation_age_minutes
        )
        doc = await self.conversations.find_one(
            {"chat_id": chat_id, "updated_at": {"$gte": cutoff}}
        )
        if not doc:
            return []
        messages = doc.get("messages", [])
        return messages[-self.config.max_history_size :]

    async def add_message(self, chat_id: int, role: str, content: str):
        """Add a message to conversation history."""
        message = {"role": role, "content": content}
        await self.conversations.update_one(
            {"chat_id": chat_id},
            {
                "$push": {
                    "messages": {
                        "$each": [message],
                        "$slice": -self.config.max_history_size,
                    }
                },
                "$set": {"updated_at": datetime.now()},
            },
            upsert=True,
        )

    async def reset_conversation(self, chat_id: int):
        """Reset conversation history for a chat."""
        await self.conversations.delete_one({"chat_id": chat_id})

    async def track_user(self, user_id: int, username: str | None):
        """Track user info."""
        await self.users.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "user_id": user_id,
                    "username": username,
                    "last_seen": datetime.now(),
                },
                "$inc": {"message_count": 1},
            },
            upsert=True,
        )

    async def get_user_model(self, user_id: int) -> str | None:
        """Get the user's chosen model, or None if not set."""
        doc = await self.users.find_one({"user_id": user_id}, {"model": 1})
        if doc:
            return doc.get("model")
        return None

    async def set_user_model(self, user_id: int, model_name: str):
        """Set the user's preferred model."""
        await self.users.update_one(
            {"user_id": user_id},
            {"$set": {"model": model_name}},
            upsert=True,
        )

    async def is_allowed_user(self, user_id: int) -> bool:
        """Check if user is allowed to use the bot."""
        doc = await self.users.find_one({"user_id": user_id}, {"allowed": 1})
        if doc:
            return doc.get("allowed", False)
        return False

    async def add_allowed_user(self, user_id: int):
        """Allow a user to use the bot."""
        await self.users.update_one(
            {"user_id": user_id},
            {"$set": {"allowed": True, "user_id": user_id}},
            upsert=True,
        )

    async def remove_allowed_user(self, user_id: int):
        """Revoke user access."""
        await self.users.update_one(
            {"user_id": user_id},
            {"$set": {"allowed": False}},
        )

    async def get_allowed_users(self) -> list[dict]:
        """Get all allowed users."""
        cursor = self.users.find({"allowed": True}, {"user_id": 1, "username": 1})
        return await cursor.to_list(length=100)
