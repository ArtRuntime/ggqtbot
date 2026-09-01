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
        self.api_endpoints = self.db["api_endpoints"]
        self.fallback_models = self.db["fallback_models"]

    async def init(self):
        """Create indexes."""
        await self.conversations.create_index("chat_id")
        await self.conversations.create_index("updated_at")
        await self.users.create_index("user_id", unique=True)
        await self.users.create_index("premium_until")
        await self.api_endpoints.create_index("name", unique=True)
        await self.fallback_models.create_index([("model_name", 1), ("base_url", 1)], unique=True)

    async def add_fallback_model(self, base_url: str, api_key: str, model_name: str, name: str = "") -> dict:
        """Add or update a fallback model in the fallback chain."""
        if not base_url.endswith("/v1") and "/v1/" not in base_url and not base_url.endswith("/v1beta"):
            base_url = base_url.rstrip("/") + "/v1"

        if not name:
            name = model_name

        data = {
            "name": name,
            "base_url": base_url,
            "api_key": api_key,
            "model_name": model_name,
            "updated_at": datetime.now(),
        }
        await self.fallback_models.update_one(
            {"model_name": model_name, "base_url": base_url},
            {"$set": data, "$setOnInsert": {"created_at": datetime.now()}},
            upsert=True,
        )
        return data

    async def remove_fallback_model(self, identifier: str) -> bool:
        """Remove a fallback model by model_name or name."""
        res = await self.fallback_models.delete_many(
            {"$or": [{"model_name": identifier}, {"name": identifier}]}
        )
        return res.deleted_count > 0

    async def get_fallback_models(self) -> list[dict]:
        """Get all custom fallback models ordered by creation time."""
        cursor = self.fallback_models.find().sort("created_at", 1)
        return await cursor.to_list(length=100)

    async def add_api_endpoint(self, name: str, base_url: str, api_key: str = ""):
        """Add or update a custom OpenAI-compatible API endpoint."""
        await self.api_endpoints.update_one(
            {"name": name},
            {
                "$set": {
                    "name": name,
                    "base_url": base_url,
                    "api_key": api_key,
                    "updated_at": datetime.now(),
                }
            },
            upsert=True,
        )

    async def remove_api_endpoint(self, name: str) -> bool:
        """Remove a custom API endpoint."""
        res = await self.api_endpoints.delete_one({"name": name})
        return res.deleted_count > 0

    async def get_api_endpoints(self) -> list[dict]:
        """Get all custom registered API endpoints."""
        cursor = self.api_endpoints.find()
        return await cursor.to_list(length=100)

    async def get_conversation(self, chat_id: int, is_premium: bool = False) -> list[dict]:
        """Get conversation history for a chat with dynamic retention based on tier."""
        max_age = (
            self.config.premium_max_conversation_age_minutes
            if is_premium
            else self.config.max_conversation_age_minutes
        )
        cutoff = datetime.now() - timedelta(minutes=max_age)
        doc = await self.conversations.find_one(
            {"chat_id": chat_id, "updated_at": {"$gte": cutoff}}
        )
        if not doc:
            return []
        messages = doc.get("messages", [])
        limit = (
            self.config.premium_max_history_size
            if is_premium
            else self.config.max_history_size
        )
        return messages[-limit:]

    async def add_message(self, chat_id: int, role: str, content: str, is_premium: bool = False):
        """Add a message to conversation history with tier-aware history limits and expiration."""
        max_age = (
            self.config.premium_max_conversation_age_minutes
            if is_premium
            else self.config.max_conversation_age_minutes
        )
        cutoff = datetime.now() - timedelta(minutes=max_age)
        doc = await self.conversations.find_one({"chat_id": chat_id}, {"updated_at": 1})
        if doc and doc.get("updated_at") and doc["updated_at"] < cutoff:
            await self.reset_conversation(chat_id)

        limit = (
            self.config.premium_max_history_size
            if is_premium
            else self.config.max_history_size
        )
        message = {"role": role, "content": content}
        await self.conversations.update_one(
            {"chat_id": chat_id},
            {
                "$push": {
                    "messages": {
                        "$each": [message],
                        "$slice": -limit,
                    }
                },
                "$set": {"updated_at": datetime.now()},
            },
            upsert=True,
        )

    async def reset_conversation(self, chat_id: int):
        """Reset conversation history for a chat."""
        await self.conversations.delete_one({"chat_id": chat_id})

    async def track_user(self, user_id: int, username: str | None = None):
        """Track user info and ensure new users default to Free (is_premium: False)."""
        await self.users.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "user_id": user_id,
                    "username": username,
                    "last_seen": datetime.now(),
                },
                "$setOnInsert": {
                    "is_premium": False,
                    "created_at": datetime.now(),
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

    async def is_premium(self, user_id: int) -> bool:
        """Check if user has active premium status with automatic 90-day expiration check."""
        if not user_id:
            return False
        if user_id in self.config.admin_user_ids:
            return True
        doc = await self.users.find_one({"user_id": user_id}, {"is_premium": 1, "premium_until": 1})
        if not doc or not doc.get("is_premium"):
            return False

        # If user is marked premium, verify expiration date
        premium_until = doc.get("premium_until")
        if not premium_until:
            await self.users.update_one({"user_id": user_id}, {"$set": {"is_premium": False}})
            return False

        if datetime.now() > premium_until:
            await self.users.update_one({"user_id": user_id}, {"$set": {"is_premium": False}})
            return False

        return True

    async def get_premium_info(self, user_id: int) -> dict | None:
        """Get premium subscription details (remaining days, expiration) for a user."""
        if not user_id:
            return None
        if user_id in self.config.admin_user_ids:
            return {"is_premium": True, "is_admin": True, "remaining_days": 9999, "premium_until": None}
        doc = await self.users.find_one({"user_id": user_id}, {"is_premium": 1, "premium_until": 1, "premium_since": 1})
        if not doc or not doc.get("is_premium"):
            return None
        premium_until = doc.get("premium_until")
        if not premium_until:
            await self.users.update_one({"user_id": user_id}, {"$set": {"is_premium": False}})
            return None
        if datetime.now() > premium_until:
            await self.users.update_one({"user_id": user_id}, {"$set": {"is_premium": False}})
            return None
        remaining = (premium_until - datetime.now()).days
        return {
            "is_premium": True,
            "is_admin": False,
            "remaining_days": max(0, remaining),
            "premium_until": premium_until,
            "premium_since": doc.get("premium_since"),
        }

    async def set_premium(self, user_id: int, is_premium: bool, days: int = 90):
        """Set or revoke premium tier for a user with expiration date (default 90 days)."""
        now = datetime.now()
        if is_premium:
            until = now + timedelta(days=days)
            await self.users.update_one(
                {"user_id": user_id},
                {
                    "$set": {
                        "is_premium": True,
                        "user_id": user_id,
                        "premium_since": now,
                        "premium_until": until,
                    },
                    "$unset": {
                        "notified_expiring_3d": "",
                        "notified_expiring_1d": "",
                        "notified_expired": "",
                    },
                },
                upsert=True,
            )
        else:
            await self.users.update_one(
                {"user_id": user_id},
                {"$set": {"is_premium": False}},
                upsert=True,
            )

    async def get_all_active_premium_candidates(self) -> list[dict]:
        """Get all active non-admin premium users for expiration monitoring and notifications."""
        cursor = self.users.find(
            {
                "is_premium": True,
                "user_id": {"$nin": self.config.admin_user_ids},
            }
        )
        return await cursor.to_list(length=500)

    async def get_premium_users(self) -> list[dict]:
        """Get all active premium users, cleaning up any expired subscriptions."""
        await self.cleanup_expired_premium_users()
        cursor = self.users.find({"is_premium": True}, {"user_id": 1, "username": 1, "premium_until": 1, "premium_since": 1})
        return await cursor.to_list(length=100)

    async def cleanup_expired_premium_users(self) -> int:
        """Batch cleanup of all expired premium subscriptions (auto-revert to non-premium)."""
        now = datetime.now()
        # Clean up any non-admin users where premium_until is missing or expired
        res = await self.users.update_many(
            {
                "is_premium": True,
                "user_id": {"$nin": self.config.admin_user_ids},
                "$or": [
                    {"premium_until": {"$lte": now}},
                    {"premium_until": {"$exists": False}},
                    {"premium_until": None},
                ],
            },
            {"$set": {"is_premium": False}},
        )
        if res.modified_count > 0:
            logger.info(f"Auto-expired and reverted {res.modified_count} premium subscriptions back to Free status.")
        return res.modified_count

    async def get_stats(self) -> dict:
        """Get aggregate database statistics."""
        try:
            total_users = await self.users.count_documents({})
            total_premium = await self.users.count_documents({"is_premium": True})
            total_conversations = await self.conversations.count_documents({})
            total_endpoints = await self.api_endpoints.count_documents({})

            pipeline = [{"$group": {"_id": None, "total_messages": {"$sum": "$message_count"}}}]
            agg = await self.users.aggregate(pipeline).to_list(length=1)
            total_messages = agg[0]["total_messages"] if agg else 0

            return {
                "total_users": total_users,
                "total_premium": total_premium,
                "total_conversations": total_conversations,
                "total_endpoints": total_endpoints,
                "total_messages": total_messages,
            }
        except Exception as e:
            logger.error(f"Failed to get stats from DB: {e}")
            return {
                "total_users": 0,
                "total_premium": 0,
                "total_conversations": 0,
                "total_endpoints": 0,
                "total_messages": 0,
            }
