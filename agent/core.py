"""
OMNI AGENT - Core Orchestrator
Connects all subsystems: memory, multi-model LLM, tools, skills, hooks,
RAG, cache, prompt templates, pipelines, conversation summarizer, and social layer.
"""
import json
import logging
import asyncio
import time
import ast
import csv
import hashlib
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional
from agent.memory import MemoryDB
from agent.multi_model_client import MultiModelClient
from agent.hooks import hooks, Event, EventType
from agent.skills_manager import SkillsManager
from agent.scheduler import Scheduler, HeartbeatMonitor
from agent.tools import WebScraper, CodeExecutor, SemanticAnalyzer, SecurityToolkit
from agent.social import ConversationManager
from agent.collaboration import CollaborationManager
from agent.doc_generator import DocGenerator
from agent.rag import RAGPipeline, VectorStore
from agent.cache import CacheClient
from agent.prompt_templates import PromptTemplateRegistry
from agent.pipeline import (
    PipelineExecutor,
    build_research_pipeline,
    build_code_pipeline,
    build_job_search_pipeline,
)
from agent.summarizer import ConversationSummarizer
from agent.structured_output import StructuredOutputParser
from agent.tools_registry import build_default_tools
from agent.tracing import SpanKind, tracer
from agent.workflow import WorkflowManager
from agent.streaming import bus, BusMessage, EventBusEvent
from agent.evaluation import Evaluator
from agent.persona import PersonaRegistry, PersonaManager
from agent.knowledge_graph import KnowledgeGraph
from agent.config_manager import ConfigManager
from agent.sandbox import Sandbox
from agent.notifications import Notifier
from agent.multimodal import VisionPipeline
from agent.auth import AuthManager
from agent.export import Exporter
from agent.security_audit import build_memory_audit_callback
from config import CONFIG

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are OMNI Agent — a highly capable, modular AI assistant.
You have access to the following tools and can invoke them by reasoning step-by-step:

TOOLS:
- web_search(query) — Search the internet
- web_scrape(url) — Fetch and read a webpage
- execute_python(code) — Run Python code
- analyze_text(text) — Semantic analysis
- remember(key, value) — Store to memory
- recall(key) — Retrieve from memory
- search_memory(query) — Search memories

INSTRUCTIONS:
- Think step-by-step before answering
- Use tools when you need current information or computation
- Be concise and accurate
- Cite sources when using web results
- If you invoke a tool, format it as: [TOOL: tool_name(args)]
"""


class _NullTraceSpan:
    def __init__(self):
        self.attributes: Dict[str, Any] = {}
        self.events: List[Dict[str, Any]] = []

    def set(self, key: str, value: Any):
        return None

    def add_event(self, name: str, attrs: Optional[Dict[str, Any]] = None):
        return None


class OmniAgent:
    """Main agent orchestrator with full tool and memory integration."""

    def __init__(self):
        self.memory = MemoryDB(CONFIG.DB_PATH)
        self.llm = MultiModelClient()
        self.router = self.llm.router
        self.scraper = WebScraper()
        self.executor = CodeExecutor()
        self.analyzer = SemanticAnalyzer()
        self.security = SecurityToolkit()
        self.skills = SkillsManager(self.memory)
        self.scheduler = Scheduler()
        self.heartbeat = HeartbeatMonitor()
        self.conversations = ConversationManager()
        self.collab = CollaborationManager(self.memory)
        self.doc_gen = DocGenerator(root_dir=".")
        # RAG pipeline with embed function wired to LLM client
        self.rag = RAGPipeline(
            vector_store=VectorStore("data/rag.db"),
            embed_fn=self._embed,
        )
        # Cache (Redis with in-memory fallback)
        self.cache = CacheClient(CONFIG.REDIS_URL)
        # Prompt template registry
        self.templates = PromptTemplateRegistry(memory=self.memory)
        # Pipeline executor with built-in pipelines
        self.pipeline_executor = PipelineExecutor()
        # Conversation summarizer
        self.summarizer = ConversationSummarizer(
            llm=self.llm,
            threshold=CONFIG.MEMORY_SUMMARY_THRESHOLD,
            keep_recent=6,
        )
        # Structured output parser
        self.structured_parser = StructuredOutputParser(self.llm)
        # Formal tool registry
        self.tool_registry = build_default_tools(self)
        # Tracing
        self.tracer = tracer
        # Workflow manager
        self.workflows = WorkflowManager(self)
        # Global event bus
        self.bus = bus
        # Evaluation framework
        self.evaluator = Evaluator(llm=self.llm, embed_fn=self._embed)
        # Persona system
        self.persona_registry = PersonaRegistry()
        self.persona_manager = PersonaManager(self.persona_registry, self.memory)
        # Knowledge graph
        self.knowledge_graph = KnowledgeGraph(db_path="data/knowledge_graph.db")
        # Config manager (hot-reload wrapper)
        self.config_mgr = ConfigManager()
        # Secure code sandbox
        self.sandbox = Sandbox(
            max_seconds=15,
            allow_shell=False,
            audit_callback=build_memory_audit_callback(self.memory),
        )
        # Notifications
        self.notifier = Notifier(db_path="data/notifications.db")
        # Vision pipeline
        self.vision = VisionPipeline(llm=self.llm)
        # Auth manager
        self.auth = AuthManager(
            secret=CONFIG.SECRET_KEY,
            db_path="data/auth.db",
            enforce_auth=CONFIG.AUTH_ENFORCE,
            bootstrap_token=CONFIG.AUTH_BOOTSTRAP_TOKEN,
        )
        # Export system
        self.exporter = Exporter(agent=self)
        self._register_default_hooks()
        self._register_default_jobs()
        self._register_health_checks()

    # ── Initialization ────────────────────────────────────────────────────────

    async def start(self):
        logger.info("OMNI Agent starting...")
        self.skills.load_from_directory()
        self.skills.load_from_db()
        self.memory.set_state("skills_list", self.skills.list_skills())
        self.memory.set_state("agent_status", {"state": "running", "started": time.time()})
        await self.scheduler.start()
        await self.heartbeat.start()
        # Connect cache (falls back to memory if Redis unavailable)
        backend = await self.cache.connect()
        logger.info(f"Cache backend: {backend}")
        # Register built-in pipelines
        self.pipeline_executor.register(build_research_pipeline(self))
        self.pipeline_executor.register(build_code_pipeline(self))
        self.pipeline_executor.register(build_job_search_pipeline())
        # Publish start event to bus
        await self.bus.publish(BusMessage(EventBusEvent.SYSTEM, {"event": "agent_start"}))
        # Start config hot-reload watcher
        await self.config_mgr.start_watcher(".env", interval=10.0)
        await hooks.emit(Event(EventType.AGENT_START, {"ts": time.time()}))
        logger.info("OMNI Agent ready.")

    async def stop(self):
        await self.scheduler.stop()
        await self.heartbeat.stop()
        await self.llm.close()
        await self.cache.close()
        await self.config_mgr.stop_watcher()
        await hooks.emit(Event(EventType.AGENT_STOP, {"ts": time.time()}))
        logger.info("OMNI Agent stopped.")

    # ── Embed helper (wired to llm.embed) ─────────────────────────────────────

    async def _embed(self, text: str) -> List[float]:
        """Embedding bridge for RAG pipeline."""
        try:
            return await self.llm.embed(text)
        except Exception as e:
            logger.warning(f"Embedding failed for text[:{min(len(text), 50)}]: {e}")
            return []

    def _active_tracer(self):
        return getattr(self, "tracer", tracer)

    @staticmethod
    def _trace_hash(value: Any) -> str:
        text = str(value or "")
        if not text:
            return ""
        digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
        return f"sha256:{digest[:16]}"

    @asynccontextmanager
    async def _safe_trace_context(self, factory, label: str):
        manager = None
        span: Any = _NullTraceSpan()
        active = False

        try:
            manager = factory()
            span = await manager.__aenter__()
            active = True
        except Exception as exc:
            logger.debug("Tracing unavailable for %s: %s", label, exc)
            manager = None
            span = _NullTraceSpan()

        try:
            yield span
        except Exception as exc:
            if active and manager is not None:
                try:
                    suppress = await manager.__aexit__(type(exc), exc, exc.__traceback__)
                except Exception as trace_exc:
                    logger.debug("Tracing exit failed for %s: %s", label, trace_exc)
                    suppress = False
                if suppress:
                    return
            raise
        else:
            if active and manager is not None:
                try:
                    await manager.__aexit__(None, None, None)
                except Exception as trace_exc:
                    logger.debug("Tracing exit failed for %s: %s", label, trace_exc)

    @asynccontextmanager
    async def _trace_span(self, name: str, kind: SpanKind, **attrs):
        trace = self._active_tracer()
        async with self._safe_trace_context(
            lambda: trace.async_span(name, kind, **attrs),
            name,
        ) as span:
            yield span

    @asynccontextmanager
    async def _trace_llm_span(self, model_id: str, session_id: str, prompt_text: str):
        trace = self._active_tracer()
        async with self._safe_trace_context(
            lambda: trace.llm_span(model_id, session_id=session_id, prompt_text=prompt_text),
            f"llm.{model_id}",
        ) as span:
            yield span

    # ── Core Chat Interface ───────────────────────────────────────────────────

    async def chat(self, user_id: Any, session_id: str, user_text: str,
                   use_rag: bool = False, rag_doc_id: Optional[str] = None) -> str:
        """Main entry point: process a user message and return a response."""
        safe_text = self.security.sanitize_input(user_text)

        async with self._trace_span(
            "chat.request",
            SpanKind.PIPELINE,
            session_id_hash=self._trace_hash(session_id),
            user_id_hash=self._trace_hash(user_id),
            use_rag=bool(use_rag),
            rag_doc_id_present=bool(rag_doc_id),
            input_chars=len(safe_text),
        ) as chat_span:
            chat_span.add_event("chat.received", {"input_chars": len(safe_text)})

            blocked_response = await self._maybe_block_prompt_injection(user_id, safe_text)
            if blocked_response:
                chat_span.set("result_path", "security_block")
                chat_span.set("response_chars", len(blocked_response))
                return blocked_response

            blocked_response = await self._maybe_block_rate_limit(user_id)
            if blocked_response:
                chat_span.set("result_path", "rate_limited")
                chat_span.set("response_chars", len(blocked_response))
                return blocked_response

            self.memory.add_message(session_id, "user", safe_text)

            quick_response = self._maybe_return_quick_response(session_id, user_id, safe_text)
            if quick_response:
                chat_span.set("result_path", "quick_response")
                chat_span.set("response_chars", len(quick_response))
                return quick_response

            skill_response = await self._maybe_execute_triggered_skill(safe_text, session_id)
            if skill_response:
                chat_span.set("result_path", "skill_response")
                chat_span.set("response_chars", len(skill_response))
                return skill_response

            messages = self._history_messages(session_id)
            messages = await self._maybe_compress_messages(messages)
            await self._maybe_apply_rag(session_id, messages, safe_text, use_rag, rag_doc_id)

            cache_key = self._chat_cache_key(session_id, messages)
            cached_response = await self._maybe_return_cached_response(session_id, cache_key)
            if cached_response:
                chat_span.set("result_path", "cache_hit")
                chat_span.set("response_chars", len(cached_response))
                return cached_response

            try:
                raw_response = await self._generate_raw_response(
                    user_id=user_id,
                    session_id=session_id,
                    safe_text=safe_text,
                    messages=messages,
                    cache_key=cache_key,
                )
                chat_span.set("tool_directive_present", "[TOOL:" in raw_response)
                final_response = await self._process_tool_calls(raw_response, session_id)
                self._store_chat_response(session_id, safe_text, final_response)
                chat_span.set("result_path", "generated")
                chat_span.set("response_chars", len(final_response))
                return final_response

            except Exception as e:
                chat_span.set("result_path", "error")
                chat_span.add_event("chat.error", {"error": str(e)})
                logger.error(f"Chat error: {e}")
                await hooks.emit(Event(EventType.AGENT_ERROR, {"error": str(e)}))
                return f"⚠️ I encountered an error: {str(e)[:200]}"

    async def _maybe_block_prompt_injection(self, user_id: Any,
                                            safe_text: str) -> Optional[str]:
        injection_check = self.security.check_prompt_injection(safe_text)
        if injection_check["safe"]:
            return None

        logger.warning(f"Injection attempt from {user_id}: {injection_check['threats']}")
        self.memory.audit("security.injection", actor=str(user_id),
                         details=injection_check)
        await hooks.emit(Event(EventType.SECURITY_ALERT, {
            "user_id": user_id,
            "threats": injection_check["threats"],
        }))
        return "⚠️ I detected a potentially unsafe input and cannot process it."

    async def _maybe_block_rate_limit(self, user_id: Any) -> Optional[str]:
        rate = self.security.rate_check(str(user_id))
        if rate["allowed"]:
            return None

        await hooks.emit(Event(EventType.RATE_LIMIT_HIT, {"user_id": user_id}))
        return f"⏱️ Rate limit reached. Try again in {rate['retry_after']:.0f}s."

    def _maybe_return_quick_response(self, session_id: str,
                                     user_id: Any,
                                     safe_text: str) -> Optional[str]:
        conv = self.conversations.process(session_id, user_id, safe_text)
        quick_response = conv.get("quick_response")
        if not quick_response:
            return None

        self.memory.add_message(session_id, "assistant", quick_response)
        return quick_response

    async def _maybe_execute_triggered_skill(self, safe_text: str,
                                             session_id: str) -> Optional[str]:
        triggered = self.skills.find_by_trigger(safe_text)
        if not triggered:
            return None

        try:
            skill_result = await triggered[0].execute(safe_text, session_id)
            response = str(skill_result)
            self.memory.add_message(session_id, "assistant", response,
                                   {"via_skill": triggered[0].name})
            return response
        except Exception as e:
            logger.error(f"Skill trigger failed: {e}")
            return None

    def _history_messages(self, session_id: str) -> List[Dict[str, str]]:
        history = self.memory.get_history(session_id, limit=50)
        return [
            {"role": item["role"], "content": item["content"]}
            for item in history
            if item["role"] in ("user", "assistant")
        ]

    async def _maybe_compress_messages(self, messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
        if len(messages) < self.summarizer.threshold:
            return messages

        messages, summary_meta = await self.summarizer.maybe_compress(messages)
        if summary_meta:
            logger.info(f"Auto-compressed: {summary_meta.original_messages} → "
                       f"{summary_meta.compressed_messages} msgs "
                       f"({summary_meta.strategy})")
        return messages

    async def _maybe_apply_rag(self, session_id: str,
                               messages: List[Dict[str, str]],
                               safe_text: str,
                               use_rag: bool,
                               rag_doc_id: Optional[str]) -> None:
        if not (use_rag or rag_doc_id):
            return
        async with self._trace_span(
            "chat.rag_augment",
            SpanKind.RAG,
            session_id_hash=self._trace_hash(session_id),
            requested=True,
            doc_filter_present=bool(rag_doc_id),
        ) as rag_span:
            try:
                effective_text, rag_results = await self.rag.augment_prompt(
                    safe_text,
                    top_k=4,
                    doc_id=rag_doc_id,
                )
                rag_span.set("chunk_count", len(rag_results))
                rag_span.set("applied", bool(rag_results))
                if rag_results:
                    logger.debug(f"RAG: {len(rag_results)} chunks retrieved "
                                f"(top score={rag_results[0].score:.2f})")
                    if messages and messages[-1]["role"] == "user":
                        messages[-1]["content"] = effective_text
            except Exception as e:
                rag_span.add_event("rag.error", {"error": str(e)})
                logger.warning(f"RAG augmentation failed: {e}")

    def _chat_cache_key(self, session_id: str,
                        messages: List[Dict[str, str]]) -> str:
        return self.cache._response_key(
            self.router.get_session_model(session_id) or "auto",
            messages[-4:] if len(messages) > 4 else messages,
        )

    async def _maybe_return_cached_response(self, session_id: str,
                                            cache_key: str) -> Optional[str]:
        async with self._trace_span(
            "chat.cache_lookup",
            SpanKind.CACHE,
            session_id_hash=self._trace_hash(session_id),
        ) as cache_span:
            cached_resp = await self.cache.get(cache_key)
            if not cached_resp or not isinstance(cached_resp, dict):
                cache_span.set("hit", False)
                return None

            cached_content = cached_resp.get("response", {}).get("content", "")
            if not cached_content:
                cache_span.set("hit", False)
                return None

            cache_span.set("hit", True)
            cache_span.set("response_chars", len(cached_content))
            logger.debug(f"Cache HIT for session {session_id}")
            self.memory.add_message(session_id, "assistant", cached_content,
                                   {"cached": True})
            return cached_content

    async def _generate_raw_response(self, user_id: Any,
                                     session_id: str,
                                     safe_text: str,
                                     messages: List[Dict[str, str]],
                                     cache_key: str) -> str:
        llm_available = await self.llm.is_available()
        if not llm_available:
            return self._fallback_response(safe_text)

        effective_system = self.persona_manager.build_system_prompt(
            session_id,
            str(user_id),
            SYSTEM_PROMPT,
        )
        if self.config_mgr.flag("persona_auto"):
            self.persona_manager.auto_detect_and_set(session_id, safe_text)

        model_hint = self.router.get_session_model(session_id) or "auto"
        async with self._trace_llm_span(model_hint, session_id, safe_text) as llm_span:
            llm_span.set("auto_route", CONFIG.MODEL_AUTO_ROUTE)
            response_data = await self.llm.chat(
                messages=messages,
                system=effective_system,
                temperature=0.7,
                session_id=session_id,
                auto_route=CONFIG.MODEL_AUTO_ROUTE,
            )
            raw_response = response_data["content"]
            routed_to = response_data.get("_routed_to", "?")
            task_type = response_data.get("_task_type", "?")
            latency = response_data.get("_latency_ms", 0)
            llm_span.set("model", routed_to)
            llm_span.set("task_type", task_type)
            llm_span.set("latency_ms", latency)
            llm_span.set("response_chars", len(raw_response))
            output_tokens = response_data.get("eval_count") or response_data.get("output_tokens") or 0
            if output_tokens:
                llm_span.set("output_tokens", int(output_tokens))
            logger.debug(f"[{task_type}] → {routed_to} ({latency}ms)")

            await self.cache.set(cache_key, {
                "response": response_data,
                "cached_at": time.time(),
            }, ttl=3600)
            llm_span.set("cache_write", True)
            return raw_response

    def _store_chat_response(self, session_id: str,
                             safe_text: str,
                             final_response: str) -> None:
        self.memory.add_message(session_id, "assistant", final_response)
        self._auto_extract_memories(session_id, safe_text, final_response)

    # ── Tool Processing ───────────────────────────────────────────────────────

    async def _process_tool_calls(self, response: str, session_id: str) -> str:
        """Parse and execute [TOOL: ...] directives in LLM output."""
        import re
        async with self._trace_span(
            "chat.tool_processing",
            SpanKind.TOOL,
            session_id_hash=self._trace_hash(session_id),
            response_chars=len(response),
        ) as tool_span:
            pattern = r'\[TOOL:\s*(\w+)\(([^)]*)\)\]'
            matches = re.findall(pattern, response)
            tool_span.set("tool_directive_count", len(matches))

            if not matches:
                return response

            augmented = response
            for tool_name, args_str in matches:
                tool_result = await self._execute_tool(tool_name, args_str, session_id)
                placeholder = f"[TOOL: {tool_name}({args_str})]"
                augmented = augmented.replace(placeholder,
                                             f"\n📎 *{tool_name} result:* {str(tool_result)[:500]}\n")

            tool_span.set("executed_tool_count", len(matches))
            return augmented

    @staticmethod
    def _coerce_tool_value(raw_value: str) -> Any:
        candidate = raw_value.strip()
        if not candidate:
            return ""
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            try:
                return ast.literal_eval(candidate)
            except (ValueError, SyntaxError):
                return candidate.strip('"\'')

    @staticmethod
    def _split_tool_args(args_str: str) -> List[str]:
        reader = csv.reader([args_str], skipinitialspace=True)
        return [part.strip() for part in next(reader, []) if part.strip()]

    def _build_tool_call(self, name: str, args_str: str, session_id: str):
        from agent.tools_registry import ToolCall

        tool = self.tool_registry.get(name) if hasattr(self, "tool_registry") else None
        param_names = [param.name for param in tool.params] if tool else []
        args_text = args_str.strip()
        arguments: Dict[str, Any] = {}

        if args_text:
            if args_text.startswith(("{", "[")):
                parsed = self._coerce_tool_value(args_text)
            else:
                parts = self._split_tool_args(args_text)
                if parts and all("=" in part for part in parts):
                    parsed = {
                        key.strip(): self._coerce_tool_value(value)
                        for key, value in (part.split("=", 1) for part in parts)
                    }
                elif len(parts) > 1:
                    parsed = [self._coerce_tool_value(part) for part in parts]
                elif parts:
                    parsed = self._coerce_tool_value(parts[0])
                else:
                    parsed = {}

            if isinstance(parsed, dict):
                arguments = parsed
            else:
                values = list(parsed) if isinstance(parsed, (list, tuple)) else [parsed]
                arguments = {
                    param_name: values[idx]
                    for idx, param_name in enumerate(param_names)
                    if idx < len(values)
                }
                if not arguments and values:
                    arguments = {"value": values[0]} if len(values) == 1 else {"values": values}

        return ToolCall(tool_name=name, arguments=arguments, session_id=session_id)

    async def _execute_tool(self, name: str, args_str: str, session_id: str) -> Any:
        """Execute a named tool exclusively through the canonical tool registry."""
        async with self._trace_span(
            f"tool.{name}",
            SpanKind.TOOL,
            session_id_hash=self._trace_hash(session_id),
            tool_name=name,
        ) as tool_span:
            if not hasattr(self, "tool_registry"):
                error = "Tool registry unavailable"
                tool_span.set("success", False)
                await hooks.emit(Event(EventType.TOOL_ERROR, {
                    "tool": name, "error": error
                }))
                return f"Error in {name}: {error}"

            call = self._build_tool_call(name, args_str, session_id)
            tool_span.set("arg_count", len(call.arguments))
            preview = json.dumps(call.arguments, default=str)[:100]
            await hooks.emit(Event(EventType.TOOL_CALLED, {"tool": name, "args": preview}))

            result = await self.tool_registry.call(call)
            tool_span.set("success", result.success)
            if result.success:
                tool_span.set("result_type", type(result.output).__name__)
                await hooks.emit(Event(EventType.TOOL_RESULT, {
                    "tool": name, "success": True
                }))
                return result.output

            tool_span.add_event("tool.error", {"error": result.error})
            await hooks.emit(Event(EventType.TOOL_ERROR, {
                "tool": name, "error": result.error
            }))
            return f"Error in {name}: {result.error}"

    # ── Memory Utilities ──────────────────────────────────────────────────────

    def _auto_extract_memories(self, session_id: str, user_text: str, response: str):
        """Extract and persist important facts from conversation."""
        import re
        # Simple heuristic: look for factual statements about the user
        name_match = re.search(r"(?:my name is|i am|i'm)\s+([A-Z][a-z]+)", user_text, re.I)
        if name_match:
            self.memory.save_memory(
                f"user_name_{session_id}", name_match.group(1),
                category="user", importance=8
            )

    def _fallback_response(self, text: str) -> str:
        """Simple fallback when Ollama is unavailable."""
        analysis = self.analyzer.analyze(text)
        return (
            f"Ollama LLM is currently unavailable. I analyzed your input:\n"
            f"• Words: {analysis['word_count']}\n"
            f"• Sentiment: {analysis['sentiment']['label']}\n"
            f"• Keywords: {', '.join(analysis['keywords'][:5])}\n\n"
            "Please ensure Ollama is running: `ollama serve`"
        )

    # ── Default Setup ─────────────────────────────────────────────────────────

    def _register_default_hooks(self):
        async def on_error(event: Event):
            self.memory.audit("agent.error", details=event.data)
        hooks.on(EventType.AGENT_ERROR, on_error, name="log_errors")

        async def on_security(event: Event):
            self.memory.audit("security.alert", details=event.data)
        hooks.on(EventType.SECURITY_ALERT, on_security, name="log_security")

    def _register_default_jobs(self):
        async def memory_cleanup():
            """Periodically summarize old conversations."""
            sessions = self.memory.list_sessions()
            logger.info(f"Memory cleanup: {len(sessions)} sessions checked.")

        async def health_report():
            status = {
                "ollama": await self.llm.is_available(),
                "jobs": len(self.scheduler.list_jobs()),
                "ts": time.time()
            }
            self.memory.set_state("health_report", status)

        self.scheduler.add_job("memory_cleanup", "0 3 * * *", memory_cleanup)
        self.scheduler.add_job("health_report", "*/15 * * * *", health_report)

    def _register_health_checks(self):
        self.heartbeat.register_check("memory_db", lambda: self.memory.db_path is not None)
        self.heartbeat.register_check("ollama", self.llm.is_available)
        self.heartbeat.register_check("tracer", lambda: len(self.tracer) >= 0)
        self.heartbeat.register_check("event_bus", lambda: self.bus.subscriber_count >= 0)
