"""
OMNI AGENT - Core Orchestrator
Connects all subsystems: memory, multi-model LLM, tools, skills, hooks,
RAG, cache, prompt templates, pipelines, conversation summarizer, and social layer.
"""
import json
import logging
import asyncio
import time
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
from agent.tracing import tracer
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
        self.sandbox = Sandbox(max_seconds=15, allow_shell=False)
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

    # ── Core Chat Interface ───────────────────────────────────────────────────

    async def chat(self, user_id: Any, session_id: str, user_text: str,
                   use_rag: bool = False, rag_doc_id: Optional[str] = None) -> str:
        """Main entry point: process a user message and return a response."""

        # Security checks
        safe_text = self.security.sanitize_input(user_text)
        injection_check = self.security.check_prompt_injection(safe_text)
        if not injection_check["safe"]:
            logger.warning(f"Injection attempt from {user_id}: {injection_check['threats']}")
            self.memory.audit("security.injection", actor=str(user_id),
                             details=injection_check)
            await hooks.emit(Event(EventType.SECURITY_ALERT, {
                "user_id": user_id, "threats": injection_check["threats"]
            }))
            return "⚠️ I detected a potentially unsafe input and cannot process it."

        rate = self.security.rate_check(str(user_id))
        if not rate["allowed"]:
            await hooks.emit(Event(EventType.RATE_LIMIT_HIT, {"user_id": user_id}))
            return f"⏱️ Rate limit reached. Try again in {rate['retry_after']:.0f}s."

        # Save user message
        self.memory.add_message(session_id, "user", safe_text)

        # Conversation manager: intent detection + quick responses
        conv = self.conversations.process(session_id, user_id, safe_text)
        if conv["quick_response"]:
            self.memory.add_message(session_id, "assistant", conv["quick_response"])
            return conv["quick_response"]

        # Check skill triggers
        triggered = self.skills.find_by_trigger(safe_text)
        if triggered:
            try:
                skill_result = await triggered[0].execute(safe_text, session_id)
                response = str(skill_result)
                self.memory.add_message(session_id, "assistant", response,
                                       {"via_skill": triggered[0].name})
                return response
            except Exception as e:
                logger.error(f"Skill trigger failed: {e}")

        # Build context from history, with auto-summarization
        history = self.memory.get_history(session_id, limit=50)
        messages = [{"role": m["role"], "content": m["content"]}
                   for m in history if m["role"] in ("user", "assistant")]

        # Auto-compress if conversation is getting long
        if len(messages) >= self.summarizer.threshold:
            messages, summary_meta = await self.summarizer.maybe_compress(messages)
            if summary_meta:
                logger.info(f"Auto-compressed: {summary_meta.original_messages} → "
                           f"{summary_meta.compressed_messages} msgs "
                           f"({summary_meta.strategy})")

        # Optional RAG augmentation
        effective_text = safe_text
        if use_rag or rag_doc_id:
            try:
                effective_text, rag_results = await self.rag.augment_prompt(
                    safe_text, top_k=4, doc_id=rag_doc_id
                )
                if rag_results:
                    logger.debug(f"RAG: {len(rag_results)} chunks retrieved "
                                f"(top score={rag_results[0].score:.2f})")
                    # Replace last user message with augmented version
                    if messages and messages[-1]["role"] == "user":
                        messages[-1]["content"] = effective_text
            except Exception as e:
                logger.warning(f"RAG augmentation failed: {e}")

        # Check response cache
        cache_key = self.cache._response_key(
            self.router.get_session_model(session_id) or "auto",
            messages[-4:] if len(messages) > 4 else messages
        )
        cached_resp = await self.cache.get(cache_key)
        if cached_resp and isinstance(cached_resp, dict):
            cached_content = cached_resp.get("response", {}).get("content", "")
            if cached_content:
                logger.debug(f"Cache HIT for session {session_id}")
                self.memory.add_message(session_id, "assistant", cached_content,
                                       {"cached": True})
                return cached_content

        # LLM call
        try:
            llm_available = await self.llm.is_available()
            if llm_available:
                # Build persona-aware system prompt
                effective_system = self.persona_manager.build_system_prompt(
                    session_id, str(user_id), SYSTEM_PROMPT
                )
                # Auto-detect persona if enabled
                if self.config_mgr.flag("persona_auto"):
                    self.persona_manager.auto_detect_and_set(session_id, safe_text)
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
                latency   = response_data.get("_latency_ms", 0)
                logger.debug(f"[{task_type}] → {routed_to} ({latency}ms)")

                # Cache successful response (TTL: 1 hour)
                await self.cache.set(cache_key, {
                    "response": response_data,
                    "cached_at": time.time()
                }, ttl=3600)
            else:
                raw_response = self._fallback_response(safe_text)

            # Process tool calls in response
            final_response = await self._process_tool_calls(raw_response, session_id)

            # Save response
            self.memory.add_message(session_id, "assistant", final_response)

            # Auto-extract memorable facts
            self._auto_extract_memories(session_id, safe_text, final_response)

            return final_response

        except Exception as e:
            logger.error(f"Chat error: {e}")
            await hooks.emit(Event(EventType.AGENT_ERROR, {"error": str(e)}))
            return f"⚠️ I encountered an error: {str(e)[:200]}"

    # ── Tool Processing ───────────────────────────────────────────────────────

    async def _process_tool_calls(self, response: str, session_id: str) -> str:
        """Parse and execute [TOOL: ...] directives in LLM output."""
        import re
        pattern = r'\[TOOL:\s*(\w+)\(([^)]*)\)\]'
        matches = re.findall(pattern, response)

        if not matches:
            return response

        augmented = response
        for tool_name, args_str in matches:
            tool_result = await self._execute_tool(tool_name, args_str, session_id)
            placeholder = f"[TOOL: {tool_name}({args_str})]"
            augmented = augmented.replace(placeholder,
                                         f"\n📎 *{tool_name} result:* {str(tool_result)[:500]}\n")

        return augmented

    async def _execute_tool(self, name: str, args_str: str, session_id: str) -> Any:
        """Execute a named tool with string args."""
        args = args_str.strip().strip("\"'")
        await hooks.emit(Event(EventType.TOOL_CALLED, {"tool": name, "args": args[:100]}))

        try:
            result = None
            if name == "web_search":
                result = await self.scraper.search(args)
            elif name == "web_scrape":
                result = await self.scraper.fetch(args)
                result = result.get("body", "")[:1000]
            elif name == "execute_python":
                result = (await self.sandbox.run_python(args)).to_dict()
            elif name == "analyze_text":
                result = self.analyzer.analyze(args)
            elif name == "remember":
                k, _, v = args.partition(",")
                self.memory.save_memory(k.strip(), v.strip(), category="agent")
                result = f"Stored: {k.strip()}"
            elif name == "recall":
                result = self.memory.get_memory(args)
            elif name == "search_memory":
                result = self.memory.search_memories(args)
            else:
                # Try skills
                result = await self.skills.execute(name, args)

            await hooks.emit(Event(EventType.TOOL_RESULT, {
                "tool": name, "success": True
            }))
            return result
        except Exception as e:
            await hooks.emit(Event(EventType.TOOL_ERROR, {
                "tool": name, "error": str(e)
            }))
            return f"Error in {name}: {e}"

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
