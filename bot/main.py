import asyncio
import logging

from bot.config import Config
from bot.database import Database
from bot.openai_helper import OpenAIHelper
from bot.telegram_bot import TelegramBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def main():
    config = Config()
    if not config.bot_token:
        logger.error("TELEGRAM_BOT_TOKEN is required")
        return
    if not config.api_id or not config.api_hash:
        logger.error("TELEGRAM_API_ID and TELEGRAM_API_HASH are required")
        return

    openai = OpenAIHelper(config)
    db = Database(config)
    bot = TelegramBot(config, openai, db)

    logger.info("Starting ggqtbot...")
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
