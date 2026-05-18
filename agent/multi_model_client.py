"""
OMNI AGENT - Multi-Model Client
Unified async interface that wraps OllamaClient with:
- Automatic routing via ModelRouter
- Fallback chain execution
- Per-model latency & error tracking
- Streaming support for all models
- Vision/multimodal payload building
- Model health probing
"""
import time
import base64
import logging
import asyncio
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from pathlib import Path

from agent.model_registry import MODELS, ModelSpec, get_model
from agent.model_router import ModelRouter, RouteDecision, TaskType, TASK_TO_CAPABILITY
from agent.ollama_client import OllamaClient
from config import CONFIG

logger = logging.getLogger(__name__)


class MultiModelClient:
    """
    Drop-in replacement for OllamaClient that routes across all 27 cloud models.
    Fully backward-compatible: existing code calling .chat() keeps working.
    """

    def __init__(self, ollama_base_url: str = None):
        self.base_url = (ollama_base_url or CONFIG.OLLAMA_BASE_URL).rstrip("/")
        self.router = ModelRouter(
            default_model=CONFIG.OLLAMA_MODEL
        )
        # One OllamaClient per distinct endpoint (most share base_url)
        self._clients: Dict[str, OllamaClient] = {}
        self._probe_cache: Dict[str, Tuple[bool, float]] = {}  # model_id -> (ok, ts)
        self._available_models_cache: Optional[Tuple[set[str], float]] = None

    def _get_client(self, model_spec: ModelSpec) -> OllamaClient:
        """Return (or create) an OllamaClient for this model's endpoint."""
        url = getattr(model_spec, 'endpoint_override', None) or self.base_url
        if url not in self._clients:
            self._clients[url] = OllamaClient(base_url=url, model=model_spec.id)
        return self._clients[url]

    # ── Availability ──────────────────────────────────────────────────────────

    async def is_available(self, model_id: str = None) -> bool:
        """Check if Ollama endpoint is reachable."""
        spec = get_model(model_id or CONFIG.OLLAMA_MODEL)
        client = self._get_client(spec) if spec else OllamaClient(self.base_url)
        return await client.is_available()

    async def probe_model(self, model_id: str,
                          cache_seconds: int = 120) -> bool:
        """
        Lightweight probe: send a short ping chat to verify model is ready.
        Results cached for cache_seconds to avoid spam.
        """
        now = time.time()
        cached = self._probe_cache.get(model_id)
        if cached and (now - cached[1]) < cache_seconds:
            return cached[0]

        spec = get_model(model_id)
        if not spec:
            return False

        try:
            client = self._get_client(spec)
            resp = await asyncio.wait_for(
                client.chat(
                    messages=[{"role": "user", "content": "ping"}],
                    model=model_id,
                    temperature=0.0,
                ),
                timeout=10.0,
            )
            ok = bool(resp.get("content"))
        except Exception as e:
            logger.debug(f"Probe failed [{model_id}]: {e}")
            ok = False

        self._probe_cache[model_id] = (ok, now)
        if not ok:
            self.router.mark_unavailable(model_id, cooldown_seconds=60)
        return ok

    async def probe_all(self) -> Dict[str, bool]:
        """Probe all 24 models concurrently. Returns {model_id: available}."""
        results = await asyncio.gather(
            *[self.probe_model(mid) for mid in MODELS],
            return_exceptions=True
        )
        return {
            mid: (r is True)
            for mid, r in zip(MODELS.keys(), results)
        }

    async def _list_ollama_models(self, cache_seconds: int = 30) -> set[str]:
        """Return model names currently available in Ollama."""
        now = time.time()
        cached = self._available_models_cache
        if cached and (now - cached[1]) < cache_seconds:
            return set(cached[0])

        client = self._clients.get(self.base_url)
        if client is None:
            client = OllamaClient(base_url=self.base_url)
            self._clients[self.base_url] = client

        try:
            models = set(await client.list_models())
        except Exception as exc:
            logger.debug("Unable to list Ollama models: %s", exc)
            models = set()

        self._available_models_cache = (set(models), now)
        return set(models)

    async def available_registered_models(self, cache_seconds: int = 30) -> List[str]:
        """Return registered model IDs that are currently available in Ollama."""
        available = await self._list_ollama_models(cache_seconds=cache_seconds)
        return [model_id for model_id in MODELS if model_id in available]

    def _build_attempt_order(
        self,
        decision: RouteDecision,
        available_ids: List[str],
    ) -> List[str]:
        """Build a deduplicated attempt order with availability-aware fallbacks."""
        attempt_order: List[str] = []
        for model_id in [decision.model_id, *decision.fallback_chain]:
            if model_id not in attempt_order:
                attempt_order.append(model_id)

        if not available_ids:
            return attempt_order

        preferred: List[str] = []
        if CONFIG.OLLAMA_MODEL in available_ids:
            preferred.append(CONFIG.OLLAMA_MODEL)

        capability = TASK_TO_CAPABILITY.get(decision.task_type)
        if capability:
            preferred.extend(
                model_id for model_id in available_ids
                if capability in MODELS[model_id].capabilities and model_id not in preferred
            )

        primary_spec = MODELS.get(decision.model_id)
        if primary_spec is not None:
            preferred.extend(
                model_id for model_id in available_ids
                if MODELS[model_id].tier == primary_spec.tier and model_id not in preferred
            )

        preferred.extend(model_id for model_id in available_ids if model_id not in preferred)

        for model_id in preferred:
            if model_id not in attempt_order:
                attempt_order.append(model_id)
        return attempt_order

    # ── Core Chat (with routing + fallback) ───────────────────────────────────

    async def chat(
        self,
        messages: List[Dict[str, str]],
        model: str = None,
        temperature: float = 0.7,
        system: str = None,
        tools: List[Dict] = None,
        session_id: str = "",
        auto_route: bool = True,
        image_path: str = None,
        image_b64: str = None,
    ) -> Dict[str, Any]:
        """
        Send messages to the best-fit model (or specified model).
        Falls back through fallback_chain on error.

        Args:
            messages:    Chat history
            model:       Override model ID (skips routing)
            temperature: Sampling temperature
            system:      System prompt
            tools:       Tool definitions
            session_id:  Session for routing override lookup
            auto_route:  If True, classify task and pick best model
            image_path:  Path to image file (enables vision routing)
            image_b64:   Base64-encoded image (enables vision routing)
        """
        has_image = bool(image_path or image_b64)

        # Build messages with optional image
        enriched_messages = self._enrich_messages(
            messages, image_path=image_path, image_b64=image_b64
        )

        # Determine model to use
        if model and model in MODELS:
            decision = RouteDecision(
                model_id=model,
                model_spec=MODELS[model],
                task_type=TaskType.GENERAL,
                confidence=1.0,
                reason="Manual override",
                fallback_chain=self.router._get_fallback_chain(model),
            )
        elif auto_route:
            user_text = self._extract_user_text(messages)
            decision = self.router.route(
                user_text, session_id=session_id, has_image=has_image
            )
        else:
            default_id = CONFIG.OLLAMA_MODEL
            spec = get_model(default_id) or list(MODELS.values())[0]
            decision = RouteDecision(
                model_id=default_id,
                model_spec=spec,
                task_type=TaskType.GENERAL,
                confidence=0.5,
                reason="Default model",
                fallback_chain=[],
            )

        # Execute with fallback
        return await self._execute_with_fallback(
            decision=decision,
            messages=enriched_messages,
            temperature=temperature,
            system=system,
            tools=tools,
        )

    async def _execute_with_fallback(
        self,
        decision: RouteDecision,
        messages: List[Dict],
        temperature: float,
        system: Optional[str],
        tools: Optional[List],
    ) -> Dict[str, Any]:
        """Try primary model, then fallback chain."""
        available_ids = await self.available_registered_models()
        attempt_order = self._build_attempt_order(decision, available_ids)

        last_error = None
        for model_id in attempt_order:
            spec = get_model(model_id)
            if not spec:
                continue

            if available_ids and model_id not in available_ids:
                last_error = RuntimeError(f"Model '{model_id}' is not available in Ollama.")
                self.router.mark_unavailable(model_id, cooldown_seconds=60)
                logger.warning("Model %s is not installed in Ollama. Trying fallback...", model_id)
                continue

            client = self._get_client(spec)
            start = time.time()
            try:
                resp = await client.chat(
                    messages=messages,
                    model=model_id,
                    temperature=temperature,
                    system=system,
                    tools=tools,
                )
                latency_ms = (time.time() - start) * 1000
                self.router.record_call(model_id, success=True, latency_ms=latency_ms)
                self.router.mark_available(model_id)

                # Annotate response with routing metadata
                resp["_routed_to"] = model_id
                resp["_model_display"] = spec.display_name
                resp["_latency_ms"] = round(latency_ms, 1)
                resp["_route_reason"] = decision.reason
                resp["_task_type"] = decision.task_type.value

                if model_id != decision.model_id:
                    logger.info(f"Fallback succeeded: {model_id} "
                               f"(primary was {decision.model_id})")
                return resp

            except Exception as e:
                latency_ms = (time.time() - start) * 1000
                self.router.record_call(model_id, success=False, error=str(e))
                last_error = e
                logger.warning(f"Model {model_id} failed: {e}. "
                              f"Trying fallback...")

        # All failed — return error response
        logger.error(f"All models failed. Last error: {last_error}")
        return {
            "role": "assistant",
            "content": (
                f"⚠️ All models in the fallback chain are unavailable. "
                f"Last error: {last_error}. "
                f"Tried: {', '.join(attempt_order)}"
            ),
            "_routed_to": "none",
            "_error": str(last_error),
        }

    # ── Streaming ─────────────────────────────────────────────────────────────

    async def chat_stream(
        self,
        messages: List[Dict[str, str]],
        model: str = None,
        temperature: float = 0.7,
        system: str = None,
        session_id: str = "",
    ) -> AsyncIterator[str]:
        """Streaming chat. Yields content chunks."""
        user_text = self._extract_user_text(messages)
        decision = self.router.route(user_text, session_id=session_id)

        target_model = model or decision.model_id
        spec = get_model(target_model)
        if not spec:
            yield "⚠️ Model not found."
            return

        client = self._get_client(spec)
        try:
            async for chunk in client.chat_stream(
                messages=messages,
                model=target_model,
                temperature=temperature,
                system=system,
            ):
                yield chunk
            self.router.record_call(target_model, success=True)
        except Exception as e:
            self.router.record_call(target_model, success=False, error=str(e))
            yield f"\n⚠️ Stream error ({target_model}): {e}"

    # ── Multi-Model Parallel ──────────────────────────────────────────────────

    async def chat_parallel(
        self,
        messages: List[Dict[str, str]],
        model_ids: List[str],
        temperature: float = 0.7,
        system: str = None,
        timeout: float = 30.0,
    ) -> Dict[str, Any]:
        """
        Send the same prompt to multiple models simultaneously.
        Returns {model_id: response} for each model.
        Useful for comparison, ensemble, or A/B testing.
        """
        async def call_one(mid: str) -> Tuple[str, Dict]:
            spec = get_model(mid)
            if not spec:
                return mid, {"error": "Unknown model"}
            client = self._get_client(spec)
            try:
                resp = await asyncio.wait_for(
                    client.chat(messages=messages, model=mid,
                               temperature=temperature, system=system),
                    timeout=timeout,
                )
                self.router.record_call(mid, success=True)
                return mid, resp
            except Exception as e:
                self.router.record_call(mid, success=False, error=str(e))
                return mid, {"error": str(e), "content": ""}

        results = await asyncio.gather(*[call_one(mid) for mid in model_ids])
        return dict(results)

    # ── Embeddings ────────────────────────────────────────────────────────────

    async def embed(self, text: str, model: str = None) -> List[float]:
        """Generate embeddings using Ollama embed endpoint."""
        embed_model = model or CONFIG.OLLAMA_EMBEDDING_MODEL
        spec = get_model(embed_model)
        client = self._get_client(spec) if spec else OllamaClient(self.base_url)
        return await client.embed(text, model=embed_model)

    # ── Image Helpers ─────────────────────────────────────────────────────────

    def _enrich_messages(
        self,
        messages: List[Dict],
        image_path: str = None,
        image_b64: str = None,
    ) -> List[Dict]:
        """Inject image into the last user message for vision models."""
        if not image_path and not image_b64:
            return messages

        if image_path:
            data = Path(image_path).read_bytes()
            image_b64 = base64.b64encode(data).decode()

        enriched = list(messages)
        # Find last user message and add image
        for i in range(len(enriched) - 1, -1, -1):
            if enriched[i].get("role") == "user":
                msg = dict(enriched[i])
                msg["images"] = [image_b64]
                enriched[i] = msg
                break
        return enriched

    def _extract_user_text(self, messages: List[Dict]) -> str:
        """Get the last user message content for routing."""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, list):
                    # Handle multimodal content blocks
                    return " ".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                return str(content)
        return ""

    # ── List / Status ─────────────────────────────────────────────────────────

    async def list_models(self) -> List[str]:
        """Return all registered model IDs."""
        return list(MODELS.keys())

    def get_stats(self) -> List[Dict]:
        return self.router.get_stats()

    def get_router_summary(self) -> Dict:
        return self.router.router_summary()

    async def close(self):
        for client in self._clients.values():
            await client.close()
