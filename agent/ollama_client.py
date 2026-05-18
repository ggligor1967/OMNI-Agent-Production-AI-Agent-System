"""
OMNI AGENT - Ollama Integration
Local LLM inference, embeddings, streaming, and model management.
"""
import json
import logging
import aiohttp
from typing import AsyncIterator, Dict, List, Optional, Any
from config import CONFIG

logger = logging.getLogger(__name__)


class OllamaClient:
    """Full Ollama API client: chat, generate, embed, list models."""

    def __init__(self, base_url: str = None, model: str = None):
        self.base_url = (base_url or CONFIG.OLLAMA_BASE_URL).rstrip("/")
        self.model = model or CONFIG.OLLAMA_MODEL
        self.embed_model = CONFIG.OLLAMA_EMBEDDING_MODEL
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120)
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Health ────────────────────────────────────────────────────────────────

    async def is_available(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(f"{self.base_url}/api/tags") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def list_models(self) -> List[str]:
        session = await self._get_session()
        async with session.get(f"{self.base_url}/api/tags") as resp:
            data = await resp.json()
        return [m["name"] for m in data.get("models", [])]

    async def pull_model(self, model: str) -> AsyncIterator[Dict]:
        session = await self._get_session()
        async with session.post(
            f"{self.base_url}/api/pull",
            json={"name": model, "stream": True}
        ) as resp:
            async for line in resp.content:
                if line.strip():
                    yield json.loads(line)

    # ── Chat ──────────────────────────────────────────────────────────────────

    async def chat(
        self,
        messages: List[Dict[str, str]],
        model: str = None,
        temperature: float = 0.7,
        system: str = None,
        tools: List[Dict] = None,
        stream: bool = False,
    ) -> Dict[str, Any]:
        """Send chat messages to Ollama. Returns full response."""
        payload = {
            "model": model or self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = tools

        session = await self._get_session()
        async with session.post(
            f"{self.base_url}/api/chat", json=payload
        ) as resp:
            if resp.status != 200:
                error = await resp.text()
                raise RuntimeError(f"Ollama error {resp.status}: {error}")
            data = await resp.json()

        msg = data.get("message", {})
        return {
            "role": msg.get("role", "assistant"),
            "content": msg.get("content", ""),
            "tool_calls": msg.get("tool_calls", []),
            "model": data.get("model"),
            "done": data.get("done", True),
            "prompt_eval_count": data.get("prompt_eval_count", 0),
            "eval_count": data.get("eval_count", 0),
        }

    async def chat_stream(
        self,
        messages: List[Dict[str, str]],
        model: str = None,
        temperature: float = 0.7,
        system: str = None,
    ) -> AsyncIterator[str]:
        """Streaming chat - yields content chunks."""
        payload = {
            "model": model or self.model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": temperature},
        }
        if system:
            payload["system"] = system

        session = await self._get_session()
        async with session.post(
            f"{self.base_url}/api/chat", json=payload
        ) as resp:
            async for line in resp.content:
                if line.strip():
                    chunk = json.loads(line)
                    content = chunk.get("message", {}).get("content", "")
                    if content:
                        yield content
                    if chunk.get("done"):
                        break

    # ── Generate (raw) ────────────────────────────────────────────────────────

    async def generate(self, prompt: str, model: str = None,
                       temperature: float = 0.7, max_tokens: int = 2048) -> str:
        payload = {
            "model": model or self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens}
        }
        session = await self._get_session()
        async with session.post(f"{self.base_url}/api/generate", json=payload) as resp:
            data = await resp.json()
        return data.get("response", "")

    # ── Embeddings ────────────────────────────────────────────────────────────

    async def embed(self, text: str, model: str = None) -> List[float]:
        """Generate text embeddings for semantic search."""
        payload = {"model": model or self.embed_model, "input": text}
        session = await self._get_session()
        async with session.post(f"{self.base_url}/api/embed", json=payload) as resp:
            data = await resp.json()
        embeddings = data.get("embeddings", [[]])
        return embeddings[0] if embeddings else []

    async def embed_batch(self, texts: List[str], model: str = None) -> List[List[float]]:
        return [await self.embed(t, model) for t in texts]

    # ── Tool-use helper ───────────────────────────────────────────────────────

    def build_tool_spec(self, name: str, description: str,
                        parameters: Dict) -> Dict:
        """Build Ollama-compatible tool definition."""
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": parameters,
                    "required": list(parameters.keys())
                }
            }
        }
