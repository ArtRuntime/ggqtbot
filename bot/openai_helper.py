import asyncio
import logging
from datetime import datetime, timedelta
from typing import AsyncGenerator

import httpx
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from bot.config import Config

logger = logging.getLogger(__name__)


class OpenAIHelper:
    def __init__(self, config: Config):
        self.config = config
        self.client = AsyncOpenAI(
            api_key=config.openai_api_key,
            base_url=config.openai_base_url,
        )
        self._available_models: list[str] = []
        self._models_fetched_at: datetime | None = None

    async def get_models(self) -> list[str]:
        """Fetch available models from the endpoint."""
        if (
            self._models_fetched_at
            and datetime.now() - self._models_fetched_at < timedelta(minutes=5)
        ):
            return self._available_models

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{self.config.openai_base_url}/models",
                    headers={"Authorization": f"Bearer {self.config.openai_api_key}"},
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
                # Filter to chat-capable models only
                excluded_prefixes = ("elevenlabs", "tts", "whisper", "dall-e")
                self._available_models = [
                    m["id"] for m in data.get("data", [])
                    if not m["id"].lower().startswith(excluded_prefixes)
                ]
                self._models_fetched_at = datetime.now()
        except Exception as e:
            logger.error(f"Failed to fetch models: {e}")
            if not self._available_models:
                self._available_models = [self.config.openai_model or "gpt-4.1-mini"]

        return self._available_models

    def get_current_model(self) -> str:
        return self.config.openai_model or (
            self._available_models[0] if self._available_models else "gpt-4.1-mini"
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def chat_completion_stream(
        self, messages: list[dict], model: str | None = None
    ) -> AsyncGenerator[str, None]:
        """Stream a chat completion response."""
        use_model = model or self.get_current_model()
        stream = await self.client.chat.completions.create(
            model=use_model,
            messages=messages,
            max_tokens=self.config.openai_max_tokens,
            stream=True,
        )

        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    async def chat_completion(
        self, messages: list[dict], max_tokens: int | None = None, model: str | None = None
    ) -> str:
        """Get a full chat completion response."""
        use_model = model or self.get_current_model()
        response = await self.client.chat.completions.create(
            model=use_model,
            messages=messages,
            max_tokens=max_tokens or self.config.openai_max_tokens,
        )
        return response.choices[0].message.content or ""
