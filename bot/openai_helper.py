import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, AsyncGenerator

import httpx
from openai import AsyncOpenAI

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
        self._models_lock = asyncio.Lock()
        self._default_working_model: str = config.openai_model or "openrouter/free"

    async def get_models(self) -> list[str]:
        """Fetch available models dynamically from OpenRouter free models and all registered custom API endpoints."""
        if (
            self._models_fetched_at
            and datetime.now() - self._models_fetched_at < timedelta(minutes=5)
        ):
            return self._available_models

        async with self._models_lock:
            # Double-check after acquiring lock (another task may have refreshed)
            if (
                self._models_fetched_at
                and datetime.now() - self._models_fetched_at < timedelta(minutes=5)
            ):
                return self._available_models

            return await self._fetch_models_locked()

    async def _fetch_models_locked(self) -> list[str]:
        """Internal model fetcher — must be called under _models_lock."""
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

    def is_free_model(self, model: str) -> bool:
        """Check if a model is an OpenRouter free tier model available to free users."""
        if not model:
            return True
        model_lower = model.lower().strip()
        if ":free" in model_lower or model_lower in ("openrouter/free", "auto"):
            return True
        # If model is explicitly mapped to a custom endpoint client (not openrouter), it is Premium
        if model in self._model_client_map and self.openrouter_client and self._model_client_map[model] != self.openrouter_client:
            return False
        # If openrouter model without :free tag (e.g. paid openrouter model)
        if "openrouter" in model_lower:
            return False
        return False

    async def test_model(self, model: str, target_client: AsyncOpenAI | None = None) -> bool:
        """Test if a model responds successfully to a completion request without retrying on 429/403 errors."""
        use_client = target_client or self._get_client_for_model(model)
        try:
            logger.info(f"Testing model: '{model}'...")
            res = await asyncio.wait_for(
                use_client.chat.completions.with_raw_response.create(
                    model=model,
                    messages=[{"role": "user", "content": "hi"}],
                    max_tokens=5,
                ),
                timeout=5.0,
            )
            if res.status_code == 200:
                logger.info(f"Model '{model}' passed test (HTTP 200).")
                return True
            logger.warning(f"Model '{model}' failed test (HTTP {res.status_code}).")
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

    async def start_background_model_discovery(self):
        """Asynchronously discover and test models in parallel background task without delaying bot startup."""
        logger.info(f"Starting background async model discovery. Initial fallback model: '{self._default_working_model}'")
        try:
            models = await self.get_models()
            if not models:
                logger.info(f"No models found from endpoints. Retaining fallback model: '{self._default_working_model}'")
                return

            test_queue = list(models)
            if self.config.openai_model and self.config.openai_model in test_queue:
                test_queue.remove(self.config.openai_model)
                test_queue.insert(0, self.config.openai_model)

            # Test up to top 15 candidate models concurrently
            candidates = test_queue[:15]
            sem = asyncio.Semaphore(4)

            async def check_candidate(m: str) -> str | None:
                async with sem:
                    if await self.test_model(m):
                        return m
                    return None

            tasks = [asyncio.create_task(check_candidate(m)) for m in candidates]
            for completed_task in asyncio.as_completed(tasks):
                working = await completed_task
                if working:
                    self._default_working_model = working
                    logger.info(f"Background model discovery complete. Active working default model set to: '{working}'")
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    break
        except Exception as e:
            logger.warning(f"Background model discovery error: {e}")

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

    @staticmethod
    def _trim_messages_for_payload(messages: list[dict], max_chars: int = 15000) -> list[dict]:
        """Trim conversation messages if payload exceeds model limits, keeping system prompt and latest user prompt intact."""
        if not messages:
            return messages
        total_len = sum(len(m.get("content", "")) for m in messages)
        if total_len <= max_chars:
            return messages

        # Always retain system prompt (first) and latest user prompt (last)
        system_msg = messages[0] if messages[0].get("role") == "system" else None
        last_msg = messages[-1]

        # Truncate system_msg content if it alone exceeds safe threshold
        if system_msg:
            sys_content = system_msg.get("content", "")
            if len(sys_content) > max_chars // 2:
                sys_content = sys_content[:max_chars // 2] + "\n...[System prompt truncated for model context limit]"
                system_msg = {"role": "system", "content": sys_content}

        # Truncate last_msg content if it alone exceeds max_chars
        last_content = last_msg.get("content", "")
        max_user_chars = max_chars - len(system_msg.get("content", "") if system_msg else "") - 1000
        if len(last_content) > max_user_chars:
            last_content = last_content[:max_user_chars] + "\n...[Content truncated for model context limit]"
            last_msg = {"role": last_msg.get("role", "user"), "content": last_content}

        trimmed = []
        if system_msg:
            trimmed.append(system_msg)

        # Add most recent history from the end backwards until max_chars is reached
        history_msgs = messages[1:-1] if system_msg else messages[:-1]
        current_len = len(system_msg.get("content", "") if system_msg else "") + len(last_msg.get("content", ""))
        kept_history = []
        for msg in reversed(history_msgs):
            m_len = len(msg.get("content", ""))
            if current_len + m_len > max_chars:
                break
            kept_history.insert(0, msg)
            current_len += m_len

        trimmed.extend(kept_history)
        trimmed.append(last_msg)
        return trimmed

    async def _get_fallback_pipeline(self) -> list[tuple[AsyncOpenAI, str]]:
        """Get ordered list of (client, model_name) fallbacks: Custom DB models first, then OpenRouter candidates, and lastly OPENROUTER_DEFAULT_MODEL."""
        pipeline: list[tuple[AsyncOpenAI, str]] = []
        seen: set[tuple[str, str]] = set()

        # 1. Custom fallback models from database (Highest priority fallback before openrouter/free)
        if self.db and hasattr(self.db, "get_fallback_models"):
            try:
                db_fallbacks = await self.db.get_fallback_models()
                for fb in db_fallbacks:
                    base_url = fb.get("base_url")
                    api_key = fb.get("api_key") or ""
                    model_name = fb.get("model_name")
                    if base_url and model_name:
                        key = (base_url, model_name)
                        if key not in seen:
                            seen.add(key)
                            client = AsyncOpenAI(api_key=api_key or "placeholder", base_url=base_url)
                            pipeline.append((client, model_name))
            except Exception as e:
                logger.warning(f"Failed to fetch DB fallback models: {e}")

        # 2. OpenRouter free high-context candidates
        if self.openrouter_client:
            openrouter_cands = [
                "google/gemini-2.0-flash-exp:free",
                "meta-llama/llama-3.3-70b-instruct:free",
                "qwen/qwen-2.5-coder-32b-instruct:free",
                "mistralai/mistral-7b-instruct:free",
            ]
            for om in openrouter_cands:
                key = (self.config.openrouter_base_url, om)
                if key not in seen:
                    seen.add(key)
                    pipeline.append((self.openrouter_client, om))

        # 3. Ultimate Fallback: OPENROUTER_DEFAULT_MODEL (e.g. openrouter/free)
        default_fb_model = self.config.openrouter_default_model or "openrouter/free"
        if self.openrouter_client:
            key = (self.config.openrouter_base_url, default_fb_model)
            if key not in seen:
                seen.add(key)
                pipeline.append((self.openrouter_client, default_fb_model))

        return pipeline

    async def chat_completion_stream(
        self, messages: list[dict], model: str | None = None
    ) -> AsyncGenerator[tuple[str, str], None]:
        """Stream a chat completion response with robust multi-model fallback and 413 Payload Too Large mitigation."""
        use_model = model or self.get_current_model()
        client_to_use = self._get_client_for_model(use_model)
        active_messages = messages

        kwargs = {
            "model": use_model,
            "messages": active_messages,
            "stream": True,
        }
        if self.config.openai_max_tokens:
            kwargs["max_tokens"] = self.config.openai_max_tokens

        try:
            stream = await client_to_use.chat.completions.create(**kwargs)
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield (chunk.choices[0].delta.content, use_model)
            return
        except Exception as e:
            err_str = str(e).lower()
            logger.warning(f"Streaming failed with model '{use_model}' ({e}). Initiating fallback sequence...")

            # If payload was too large (HTTP 413 / request_too_large), adaptively trim payload
            if "413" in err_str or "too large" in err_str or "payload" in err_str or "context" in err_str:
                active_messages = self._trim_messages_for_payload(messages, max_chars=14000)

            pipeline = await self._get_fallback_pipeline()
            for fb_client, cand in pipeline:
                if cand == use_model:
                    continue
                try:
                    logger.info(f"Attempting fallback stream with model '{cand}'...")
                    fb_kwargs = {
                        "model": cand,
                        "messages": active_messages,
                        "stream": True,
                    }
                    if self.config.openai_max_tokens:
                        fb_kwargs["max_tokens"] = self.config.openai_max_tokens
                    stream = await fb_client.chat.completions.create(**fb_kwargs)
                    streamed_any = False
                    async for chunk in stream:
                        if chunk.choices and chunk.choices[0].delta.content:
                            streamed_any = True
                            yield (chunk.choices[0].delta.content, cand)
                    if streamed_any:
                        self._default_working_model = cand
                        return
                except Exception as fb_err:
                    logger.warning(f"Fallback candidate '{cand}' failed: {fb_err}")
                    continue

            # If all streaming candidates failed, raise original error
            raise e

    async def chat_completion(
        self, messages: list[dict], max_tokens: int | None = None, model: str | None = None
    ) -> str:
        """Get a full chat completion response with robust multi-model fallback and 413 Payload Too Large mitigation."""
        use_model = model or self.get_current_model()
        client_to_use = self._get_client_for_model(use_model)
        tokens = max_tokens or self.config.openai_max_tokens
        active_messages = messages

        kwargs = {
            "model": use_model,
            "messages": active_messages,
        }
        if tokens:
            kwargs["max_tokens"] = tokens

        try:
            response = await client_to_use.chat.completions.create(**kwargs)
            return response.choices[0].message.content or ""
        except Exception as e:
            err_str = str(e).lower()
            logger.warning(f"Model '{use_model}' failed ({e}). Initiating fallback sequence...")

            if "413" in err_str or "too large" in err_str or "payload" in err_str or "context" in err_str:
                active_messages = self._trim_messages_for_payload(messages, max_chars=14000)

            pipeline = await self._get_fallback_pipeline()
            for fb_client, cand in pipeline:
                if cand == use_model:
                    continue
                try:
                    logger.info(f"Attempting fallback completion with model '{cand}'...")
                    fb_kwargs = {
                        "model": cand,
                        "messages": active_messages,
                    }
                    if tokens:
                        fb_kwargs["max_tokens"] = tokens
                    response = await fb_client.chat.completions.create(**fb_kwargs)
                    content = response.choices[0].message.content or ""
                    if content:
                        self._default_working_model = cand
                        return content
                except Exception as fb_err:
                    logger.warning(f"Fallback candidate '{cand}' failed: {fb_err}")
                    continue

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

