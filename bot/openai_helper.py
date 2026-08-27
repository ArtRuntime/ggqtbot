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
        self._default_working_model: str | None = None

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

    async def test_model(self, model: str) -> bool:
        """Test if a model responds successfully to a minimal completion request."""
        try:
            logger.info(f"Testing model: '{model}'...")
            response = await asyncio.wait_for(
                self.client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": "hi"}],
                    max_tokens=5,
                ),
                timeout=10.0,
            )
            if response.choices and len(response.choices) > 0:
                logger.info(f"Model '{model}' passed test.")
                return True
            logger.warning(f"Model '{model}' returned empty response during test.")
            return False
        except Exception as e:
            logger.warning(f"Model '{model}' failed test: {e}")
            return False

    async def find_working_default_model(self) -> str:
        """Fetch models, test them sequentially from the first, and set the first working model as default/fallback."""
        models = await self.get_models()
        if not models:
            logger.warning("No models found from endpoint.")
            default = self.config.openai_model or "gpt-4.1-mini"
            self._default_working_model = default
            return default

        test_queue = list(models)
        if self.config.openai_model and self.config.openai_model in test_queue:
            test_queue.remove(self.config.openai_model)
            test_queue.insert(0, self.config.openai_model)

        for model in test_queue:
            if await self.test_model(model):
                self._default_working_model = model
                logger.info(f"Selected '{model}' as default and fallback model.")
                return model

        fallback = self.config.openai_model or models[0]
        logger.warning(f"No models passed test. Falling back to '{fallback}'.")
        self._default_working_model = fallback
        return fallback

    def get_current_model(self) -> str:
        if self._default_working_model:
            return self._default_working_model
        return self.config.openai_model or (
            self._available_models[0] if self._available_models else "gpt-4.1-mini"
        )

    async def get_working_fallback_model(self, exclude_models: set[str] | None = None) -> str:
        """Find and return a guaranteed working model, excluding known failed models."""
        excluded = exclude_models or set()

        # Check current default working model first if not excluded
        current = self.get_current_model()
        if current not in excluded and await self.test_model(current):
            return current

        # Dynamic re-scan of all models to find an operational working model
        models = await self.get_models()
        for m in models:
            if m not in excluded and await self.test_model(m):
                self._default_working_model = m
                logger.info(f"Updated default working fallback model to '{m}'")
                return m

        # If none pass, return current model as last resort
        return current

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def chat_completion_stream(
        self, messages: list[dict], model: str | None = None
    ) -> AsyncGenerator[tuple[str, str], None]:
        """Stream a chat completion response with robust working-model fallback handling. Yields (chunk, model_used)."""
        use_model = model or self.get_current_model()
        try:
            stream = await self.client.chat.completions.create(
                model=use_model,
                messages=messages,
                max_tokens=self.config.openai_max_tokens,
                stream=True,
            )
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield (chunk.choices[0].delta.content, use_model)
        except Exception as e:
            logger.warning(f"Streaming failed with model '{use_model}' ({e}). Searching for a working fallback model...")
            fallback_model = await self.get_working_fallback_model(exclude_models={use_model})
            if fallback_model != use_model:
                logger.info(f"Retrying stream with verified working fallback model '{fallback_model}'...")
                stream = await self.client.chat.completions.create(
                    model=fallback_model,
                    messages=messages,
                    max_tokens=self.config.openai_max_tokens,
                    stream=True,
                )
                async for chunk in stream:
                    if chunk.choices and chunk.choices[0].delta.content:
                        yield (chunk.choices[0].delta.content, fallback_model)
            else:
                raise e

    async def chat_completion(
        self, messages: list[dict], max_tokens: int | None = None, model: str | None = None
    ) -> str:
        """Get a full chat completion response with robust working-model fallback handling."""
        use_model = model or self.get_current_model()
        try:
            response = await self.client.chat.completions.create(
                model=use_model,
                messages=messages,
                max_tokens=max_tokens or self.config.openai_max_tokens,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            logger.warning(f"Model '{use_model}' failed ({e}). Searching for a working fallback model...")
            fallback_model = await self.get_working_fallback_model(exclude_models={use_model})
            if fallback_model != use_model:
                logger.info(f"Retrying completion with verified working fallback model '{fallback_model}'...")
                response = await self.client.chat.completions.create(
                    model=fallback_model,
                    messages=messages,
                    max_tokens=max_tokens or self.config.openai_max_tokens,
                )
                return response.choices[0].message.content or ""
            raise e

