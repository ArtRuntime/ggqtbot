import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, AsyncGenerator

import httpx
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from bot.config import Config

try:
    from openrouter import OpenRouter
    HAS_OPENROUTER_SDK = True
except ImportError:
    HAS_OPENROUTER_SDK = False

logger = logging.getLogger(__name__)


class OpenAIHelper:
    def __init__(self, config: Config, db: Any = None):
        self.config = config
        self.db = db
        self.client = AsyncOpenAI(
            api_key=config.openai_api_key or "placeholder",
            base_url=config.openai_base_url or "https://openrouter.ai/api/v1",
        )
        self.openrouter_client: AsyncOpenAI | None = None
        if config.openrouter_api_key or "openrouter.ai" in config.openrouter_base_url:
            self.openrouter_client = AsyncOpenAI(
                api_key=config.openrouter_api_key or "free",
                base_url=config.openrouter_base_url,
                default_headers={
                    "HTTP-Referer": "https://github.com/ggqtbot",
                    "X-Title": "ggqtbot",
                },
            )
        self._available_models: list[str] = []
        self._model_client_map: dict[str, AsyncOpenAI] = {}
        self._models_fetched_at: datetime | None = None
        self._default_working_model: str | None = None

    async def get_models(self) -> list[str]:
        """Fetch available models dynamically from OpenRouter free models and all registered custom API endpoints."""
        if (
            self._models_fetched_at
            and datetime.now() - self._models_fetched_at < timedelta(minutes=5)
        ):
            return self._available_models

        combined_models: list[str] = []
        self._model_client_map.clear()

        # 1. OpenRouter free models (Default Primary & Fallback)
        if self.openrouter_client:
            openrouter_free = await self.get_openrouter_free_models()
            for om in openrouter_free:
                if om not in combined_models:
                    combined_models.append(om)
                self._model_client_map[om] = self.openrouter_client

        # 2. Custom API endpoints registered in MongoDB
        if self.db and hasattr(self.db, "get_api_endpoints"):
            try:
                endpoints = await self.db.get_api_endpoints()
                for ep in endpoints:
                    base_url = ep.get("base_url")
                    api_key = ep.get("api_key") or ""
                    if not base_url:
                        continue
                    try:
                        ep_client = AsyncOpenAI(api_key=api_key or "placeholder", base_url=base_url)
                        async with httpx.AsyncClient() as http_c:
                            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
                            resp = await http_c.get(f"{base_url}/models", headers=headers, timeout=5)
                            if resp.status_code == 200:
                                data = resp.json()
                                excluded = ("elevenlabs", "tts", "whisper", "dall-e")
                                discovered = [
                                    m["id"] for m in data.get("data", [])
                                    if not m["id"].lower().startswith(excluded)
                                ]
                                for dm in discovered:
                                    if dm not in combined_models:
                                        combined_models.append(dm)
                                    self._model_client_map[dm] = ep_client
                    except Exception as ep_err:
                        logger.warning(f"Failed to fetch models for endpoint '{ep.get('name')}': {ep_err}")
            except Exception as db_err:
                logger.warning(f"Failed to fetch DB endpoints: {db_err}")

        # 3. Environment Configured OpenAI base_url (if specified & valid)
        if (
            self.config.openai_base_url
            and "0.0.0.0" not in self.config.openai_base_url
            and "openrouter.ai" not in self.config.openai_base_url
        ):
            try:
                async with httpx.AsyncClient() as http_c:
                    headers = {"Authorization": f"Bearer {self.config.openai_api_key}"} if self.config.openai_api_key else {}
                    resp = await http_c.get(f"{self.config.openai_base_url}/models", headers=headers, timeout=5)
                    if resp.status_code == 200:
                        data = resp.json()
                        excluded = ("elevenlabs", "tts", "whisper", "dall-e")
                        discovered = [
                            m["id"] for m in data.get("data", [])
                            if not m["id"].lower().startswith(excluded)
                        ]
                        for dm in discovered:
                            if dm not in combined_models:
                                combined_models.append(dm)
                            self._model_client_map[dm] = self.client
            except Exception as e:
                logger.debug(f"Config base_url model fetch failed: {e}")

        if combined_models:
            self._available_models = combined_models
            self._models_fetched_at = datetime.now()
        elif not self._available_models:
            self._available_models = ["google/gemini-2.0-flash-exp:free", "openrouter/free"]

        return self._available_models

    def _get_client_for_model(self, model: str) -> AsyncOpenAI:
        """Return appropriate client for a model, checking registered map, OpenRouter, or default client."""
        if model in self._model_client_map:
            return self._model_client_map[model]
        if (":free" in model or "openrouter" in model.lower()) and self.openrouter_client:
            return self.openrouter_client
        return self.client

    async def test_model(self, model: str, target_client: AsyncOpenAI | None = None) -> bool:
        """Test if a model responds successfully to a minimal completion request."""
        use_client = target_client or self._get_client_for_model(model)
        try:
            logger.info(f"Testing model: '{model}'...")
            response = await asyncio.wait_for(
                use_client.chat.completions.create(
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

    async def get_openrouter_free_models(self) -> list[str]:
        """Fetch available models from OpenRouter and filter strictly for models containing ':free'."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{self.config.openrouter_base_url}/models",
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
                free_models = [
                    m["id"] for m in data.get("data", [])
                    if ":free" in m.get("id", "").lower()
                ]
                if free_models:
                    logger.info(f"Discovered {len(free_models)} free OpenRouter models containing ':free'.")
                    return free_models
        except Exception as e:
            logger.warning(f"Failed to fetch OpenRouter free models: {e}")

        # Fallback list of known :free models
        return [
            "google/gemini-2.0-flash-exp:free",
            "meta-llama/llama-3.3-70b-instruct:free",
            "deepseek/deepseek-r1:free",
            "qwen/qwen-2.5-coder-32b-instruct:free",
            "mistralai/mistral-7b-instruct:free",
        ]

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

        # If primary endpoint models fail, test OpenRouter fallback models containing ':free'
        if self.openrouter_client:
            logger.warning("No primary models passed test. Testing OpenRouter free models...")
            openrouter_models = await self.get_openrouter_free_models()
            for om in openrouter_models:
                if await self.test_model(om):
                    self._default_working_model = om
                    logger.info(f"Selected OpenRouter free model '{om}' as default fallback.")
                    return om

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

    async def get_working_fallback_model(
        self, exclude_models: set[str] | None = None
    ) -> tuple[str, AsyncOpenAI]:
        """Find and return a guaranteed working model and client, checking primary endpoint first and OpenRouter as ultimate fallback."""
        excluded = exclude_models or set()

        # 1. Check current default working model on primary client
        current = self.get_current_model()
        current_client = self._get_client_for_model(current)
        if current not in excluded and await self.test_model(current, current_client):
            return (current, current_client)

        # 2. Re-scan primary models from endpoint
        models = await self.get_models()
        for m in models:
            m_client = self._get_client_for_model(m)
            if m not in excluded and await self.test_model(m, m_client):
                self._default_working_model = m
                logger.info(f"Updated default working fallback model to '{m}'.")
                return (m, m_client)

        # 3. Fallback to OpenRouter API if primary endpoint models fail (strictly models containing ':free')
        if self.openrouter_client:
            logger.warning("Primary endpoint models failed. Attempting OpenRouter ':free' model fallback...")
            openrouter_models = await self.get_openrouter_free_models()
            for om in openrouter_models:
                if om not in excluded and await self.test_model(om, self.openrouter_client):
                    logger.info(f"Selected OpenRouter free fallback model '{om}'")
                    return (om, self.openrouter_client)

        return (current, self.client)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def chat_completion_stream(
        self, messages: list[dict], model: str | None = None
    ) -> AsyncGenerator[tuple[str, str], None]:
        """Stream a chat completion response with robust primary & OpenRouter fallback handling. Yields (chunk, model_used)."""
        use_model = model or self.get_current_model()
        client_to_use = self._get_client_for_model(use_model)
        kwargs = {
            "model": use_model,
            "messages": messages,
            "stream": True,
        }
        if self.config.openai_max_tokens:
            kwargs["max_tokens"] = self.config.openai_max_tokens

        try:
            stream = await client_to_use.chat.completions.create(**kwargs)
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield (chunk.choices[0].delta.content, use_model)
        except Exception as e:
            logger.warning(f"Streaming failed with model '{use_model}' ({e}). Searching for primary or OpenRouter fallback model...")
            fallback_model, fallback_client = await self.get_working_fallback_model(exclude_models={use_model})
            if fallback_model != use_model or fallback_client != client_to_use:
                logger.info(f"Retrying stream with verified fallback model '{fallback_model}'...")
                fallback_kwargs = {
                    "model": fallback_model,
                    "messages": messages,
                    "stream": True,
                }
                if self.config.openai_max_tokens:
                    fallback_kwargs["max_tokens"] = self.config.openai_max_tokens
                stream = await fallback_client.chat.completions.create(**fallback_kwargs)
                async for chunk in stream:
                    if chunk.choices and chunk.choices[0].delta.content:
                        yield (chunk.choices[0].delta.content, fallback_model)
            else:
                raise e

    async def chat_completion(
        self, messages: list[dict], max_tokens: int | None = None, model: str | None = None
    ) -> str:
        """Get a full chat completion response with robust primary & OpenRouter fallback handling."""
        use_model = model or self.get_current_model()
        client_to_use = self._get_client_for_model(use_model)
        tokens = max_tokens or self.config.openai_max_tokens
        kwargs = {
            "model": use_model,
            "messages": messages,
        }
        if tokens:
            kwargs["max_tokens"] = tokens

        try:
            response = await client_to_use.chat.completions.create(**kwargs)
            return response.choices[0].message.content or ""
        except Exception as e:
            logger.warning(f"Model '{use_model}' failed ({e}). Searching for primary or OpenRouter fallback model...")
            fallback_model, fallback_client = await self.get_working_fallback_model(exclude_models={use_model})
            if fallback_model != use_model or fallback_client != client_to_use:
                logger.info(f"Retrying completion with verified fallback model '{fallback_model}'...")
                fallback_kwargs = {
                    "model": fallback_model,
                    "messages": messages,
                }
                if tokens:
                    fallback_kwargs["max_tokens"] = tokens
                response = await fallback_client.chat.completions.create(**fallback_kwargs)
                return response.choices[0].message.content or ""
            raise e

    def openrouter_sdk_completion(self, messages: list[dict], model: str | None = None) -> str:
        """Execute chat completion using the official OpenRouter Python SDK."""
        if not HAS_OPENROUTER_SDK:
            raise RuntimeError("OpenRouter Python SDK ('openrouter') is not installed.")
        use_model = model or self.config.openrouter_default_model
        api_key = self.config.openrouter_api_key or ""
        with OpenRouter(api_key=api_key) as open_router:
            res = open_router.chat.send(
                model=use_model,
                messages=messages,
                stream=False,
            )
            if hasattr(res, "choices") and res.choices:
                return res.choices[0].message.content or ""
            elif isinstance(res, dict) and "choices" in res:
                return res["choices"][0]["message"]["content"] or ""
            return str(res)

