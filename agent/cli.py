"""
OMNI AGENT - Enhanced CLI
Rich-powered interactive terminal with:
- Live model selector and routing display
- Syntax-highlighted code output
- Session management and history
- Pipeline runner
- Inline RAG document loading
- Stats dashboard
"""
import asyncio
import sys
import os
import re
import time
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from agent.core import OmniAgent

# ── Rich imports (graceful degradation if not installed) ──────────────────────
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.syntax import Syntax
    from rich.markdown import Markdown
    from rich.prompt import Prompt
    from rich.live import Live
    from rich.spinner import Spinner
    from rich.columns import Columns
    from rich.text import Text
    from rich import box
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

console = Console() if RICH_AVAILABLE else None


def _print(text: str, style: str = ""):
    if RICH_AVAILABLE:
        console.print(text, style=style if style else None)
    else:
        print(text)


def _panel(content: str, title: str = "", style: str = "cyan"):
    if RICH_AVAILABLE:
        console.print(Panel(content, title=title, border_style=style))
    else:
        print(f"\n── {title} ──\n{content}\n")


def _table(title: str, columns: list, rows: list) -> None:
    if RICH_AVAILABLE:
        tbl = Table(title=title, box=box.ROUNDED, border_style="dim cyan")
        for col in columns:
            tbl.add_column(col, style="white")
        for row in rows:
            tbl.add_row(*[str(c) for c in row])
        console.print(tbl)
    else:
        print(f"\n{title}")
        print(" | ".join(columns))
        for row in rows:
            print(" | ".join(str(c) for c in row))


# ══════════════════════════════════════════════════════════════════════════════
# RESPONSE RENDERER
# ══════════════════════════════════════════════════════════════════════════════

def render_response(text: str, routed_to: str = "", task_type: str = "",
                    latency_ms: float = 0, cached: bool = False):
    """Render agent response with syntax highlighting for code blocks."""
    if not RICH_AVAILABLE:
        print(f"\nAgent: {text}\n")
        return

    # Extract and highlight code blocks
    code_pattern = re.compile(r'```(\w+)?\n(.*?)```', re.DOTALL)
    parts = []
    last_end = 0

    for m in code_pattern.finditer(text):
        # Text before code block
        before = text[last_end:m.start()].strip()
        if before:
            parts.append(("text", before))
        lang = m.group(1) or "text"
        code = m.group(2)
        parts.append(("code", code, lang))
        last_end = m.end()

    after = text[last_end:].strip()
    if after:
        parts.append(("text", after))

    # If no code blocks found, just render as markdown
    if not parts:
        parts = [("text", text)]

    for part in parts:
        if part[0] == "text":
            try:
                console.print(Markdown(part[1]))
            except Exception:
                console.print(part[1])
        elif part[0] == "code":
            try:
                console.print(Syntax(part[2], part[1], theme="monokai",
                                    line_numbers=True,
                                    word_wrap=True))
            except Exception:
                console.print(f"```\n{part[2]}\n```")

    # Routing metadata footer
    if routed_to:
        cache_badge = " [cached]" if cached else ""
        meta = f"[dim]→ {routed_to} | {task_type} | {latency_ms:.0f}ms{cache_badge}[/dim]"
        console.print(meta)


# ══════════════════════════════════════════════════════════════════════════════
# CLI COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

HELP_TEXT = """
[bold cyan]OMNI Agent CLI — Commands[/bold cyan]

[bold]Chat:[/bold]
  <message>              Send a message (auto-routed)
  /clear                 Clear conversation history
  /history               Show conversation history
  /summarize             Compress conversation history

[bold]Models:[/bold]
  /models                List all 24 cloud models
  /model                 Show current model
  /model <id>            Pin to a specific model
  /model auto            Restore auto-routing
  /route <text>          Preview routing decision
  /compare <prompt>      Run prompt on 3 models and compare

[bold]RAG:[/bold]
  /load <file>           Ingest a document into RAG
  /loaddir <dir>         Ingest all docs in a directory
  /docs                  List ingested documents
  /rag <question>        RAG-augmented question answering

[bold]Pipelines:[/bold]
  /pipelines             List registered pipelines
  /run <name> [json]     Execute a named pipeline
  /runs                  Show recent pipeline runs

[bold]Templates:[/bold]
  /templates             List prompt templates
  /template <name>       Show template details
  /use <name> [vars]     Use a template (vars as JSON)

[bold]System:[/bold]
  /status                System status and stats
  /stats                 Model usage statistics
  /skills                List loaded skills
  /memory <query>        Search memory
  /chronicle tips        Review session history and get personalized tips
  /help                  Show this help
  /quit  /exit           Exit
"""


class EnhancedCLI:
    """Interactive Rich CLI for OMNI Agent."""

    def __init__(self, agent: "OmniAgent"):
        self.agent = agent
        self.session_id = f"cli:{int(time.time())}"
        self._history: list = []
        self._running = True
        self._current_model = "auto"

    async def run(self):
        """Main REPL loop."""
        self._banner()

        while self._running:
            try:
                user_input = await asyncio.to_thread(self._prompt)
            except (EOFError, KeyboardInterrupt):
                _print("\n[dim]Goodbye.[/dim]")
                break

            if not user_input:
                continue

            self._history.append(user_input)

            if user_input.startswith("/"):
                await self._handle_command(user_input)
            else:
                await self._handle_message(user_input)

    def _prompt(self) -> str:
        model_label = (f"[{self._current_model.split(':')[0]}]"
                      if self._current_model != "auto" else "[auto]")
        if RICH_AVAILABLE:
            return console.input(f"[bold green]You{model_label}>[/bold green] ")
        return input(f"You{model_label}> ")

    def _banner(self):
        if RICH_AVAILABLE:
            console.print(Panel(
                "[bold green]OMNI Agent[/bold green] — 24 Cloud Models | RAG | Pipelines\n"
                "[dim]Type [bold]/help[/bold] for commands. [bold]/models[/bold] to browse models.[/dim]",
                border_style="green",
                padding=(0, 2),
            ))
        else:
            print("\n=== OMNI Agent CLI ===\nType /help for commands.\n")

    # ── Message Handling ──────────────────────────────────────────────────────

    async def _handle_message(self, text: str):
        start = time.time()

        if RICH_AVAILABLE:
            with console.status("[dim]Thinking...[/dim]", spinner="dots"):
                response = await self.agent.chat("cli", self.session_id, text)
                latency_ms = (time.time() - start) * 1000
        else:
            print("...")
            response = await self.agent.chat("cli", self.session_id, text)
            latency_ms = (time.time() - start) * 1000

        # Get routing metadata from last LLM call
        stats = self.agent.llm.get_stats()
        routed_to = ""
        task_type = ""
        cached = False

        if RICH_AVAILABLE:
            console.print()

        render_response(
            response,
            routed_to=routed_to,
            task_type=task_type,
            latency_ms=latency_ms,
            cached=cached,
        )

        if RICH_AVAILABLE:
            console.print()

    # ── Command Dispatcher ────────────────────────────────────────────────────

    async def _handle_command(self, cmd: str):
        parts = cmd.strip().split(None, 2)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        rest = parts[2] if len(parts) > 2 else ""

        dispatch = {
            "/help":       self._cmd_help,
            "/models":     self._cmd_models,
            "/model":      self._cmd_model,
            "/route":      self._cmd_route,
            "/compare":    self._cmd_compare,
            "/clear":      self._cmd_clear,
            "/history":    self._cmd_history,
            "/summarize":  self._cmd_summarize,
            "/load":       self._cmd_load,
            "/loaddir":    self._cmd_loaddir,
            "/docs":       self._cmd_docs,
            "/rag":        self._cmd_rag,
            "/pipelines":  self._cmd_pipelines,
            "/run":        self._cmd_run,
            "/runs":       self._cmd_runs,
            "/templates":  self._cmd_templates,
            "/template":   self._cmd_template,
            "/use":        self._cmd_use,
            "/status":     self._cmd_status,
            "/stats":      self._cmd_stats,
            "/skills":     self._cmd_skills,
            "/memory":     self._cmd_memory,
            "/chronicle":  self._cmd_chronicle,
            "/quit":       self._cmd_quit,
            "/exit":       self._cmd_quit,
        }

        handler = dispatch.get(command)
        if handler:
            try:
                await handler(args, rest)
            except Exception as e:
                _print(f"[red]Command error: {e}[/red]")
        else:
            _print(f"[yellow]Unknown command: {command}. Try /help[/yellow]")

    # ── Command Implementations ───────────────────────────────────────────────

    async def _cmd_help(self, *_):
        if RICH_AVAILABLE:
            console.print(HELP_TEXT)
        else:
            print(re.sub(r'\[.*?\]', '', HELP_TEXT))

    async def _cmd_models(self, *_):
        from agent.model_registry import MODELS, ModelTier
        tier_icons = {
            ModelTier.FLAGSHIP: "🏆",
            ModelTier.BALANCED: "⚖️",
            ModelTier.FAST:     "⚡",
            ModelTier.MICRO:    "🔹",
        }
        rows = []
        for spec in sorted(MODELS.values(), key=lambda s: s.provider):
            rows.append((
                f"{tier_icons.get(spec.tier,'')} {spec.id}",
                spec.provider,
                f"{spec.context_window//1000}k",
                "✓" if spec.supports_vision else "",
                spec.best_for[0] if spec.best_for else "",
            ))
        _table("24 Cloud Models", ["Model ID", "Provider", "Context", "Vision", "Best For"], rows)

    async def _cmd_model(self, model_id: str, *_):
        from agent.model_registry import get_model, MODELS
        if not model_id:
            _print(f"Current model: [bold]{self._current_model}[/bold]")
            return
        if model_id == "auto":
            self._current_model = "auto"
            self.agent.llm.router.clear_session_model(self.session_id)
            _print("[green]✓ Auto-routing restored[/green]")
            return
        spec = get_model(model_id)
        if not spec:
            _print(f"[red]Model not found: {model_id}[/red]")
            return
        self.agent.llm.router.set_session_model(self.session_id, model_id)
        self._current_model = model_id
        _print(f"[green]✓ Pinned to:[/green] [bold]{spec.display_name}[/bold] "
               f"({spec.provider}, {spec.context_window//1000}k ctx)")

    async def _cmd_route(self, text: str, *_):
        if not text:
            _print("[yellow]Usage: /route <text>[/yellow]")
            return
        from agent.model_router import classify_task
        task, conf = classify_task(text)
        decision = self.agent.llm.router.route(text)
        spec = decision.model_spec
        fb = " → ".join(decision.fallback_chain[:3]) or "none"
        _panel(
            f"Task type:  {task.value} (conf: {conf:.0%})\n"
            f"Selected:   {spec.display_name}\n"
            f"Provider:   {spec.provider}\n"
            f"Tier:       {spec.tier.value}\n"
            f"Reason:     {decision.reason}\n"
            f"Fallbacks:  {fb}",
            title="🧭 Routing Decision", style="blue"
        )

    async def _cmd_compare(self, text: str, *_):
        if not text:
            _print("[yellow]Usage: /compare <prompt>[/yellow]")
            return
        from config import CONFIG
        models = CONFIG.MODEL_COMPARE_IDS[:3]
        _print(f"[dim]Running on {len(models)} models: {', '.join(models)}[/dim]")
        messages = [{"role": "user", "content": text}]

        if RICH_AVAILABLE:
            with console.status("[dim]Querying models in parallel...[/dim]"):
                results = await self.agent.llm.chat_parallel(messages, model_ids=models, timeout=30)
        else:
            results = await self.agent.llm.chat_parallel(messages, model_ids=models, timeout=30)

        for mid, resp in results.items():
            content = resp.get("content", resp.get("error", "No response"))
            _panel(content[:800], title=f"🤖 {mid}", style="cyan")

    async def _cmd_clear(self, *_):
        self.agent.memory.clear_session(self.session_id)
        _print("[green]✓ Conversation history cleared[/green]")

    async def _cmd_history(self, *_):
        history = self.agent.memory.get_history(self.session_id, limit=30)
        if not history:
            _print("[dim]No history.[/dim]")
            return
        rows = [(m["role"], str(m["content"])[:80]) for m in history]
        _table("Conversation History", ["Role", "Content"], rows)

    async def _cmd_summarize(self, *_):
        from agent.summarizer import ConversationSummarizer
        history = self.agent.memory.get_history(self.session_id, limit=100)
        if len(history) < 4:
            _print("[dim]Not enough history to summarize.[/dim]")
            return
        summarizer = ConversationSummarizer(llm=self.agent.llm, threshold=4,
                                            keep_recent=4)
        compressed, meta = await summarizer.maybe_compress(history)
        if meta:
            _print(f"[green]✓ Compressed {meta.original_messages} → "
                  f"{meta.compressed_messages} messages "
                  f"(strategy: {meta.strategy})[/green]")
        else:
            _print("[dim]No compression needed.[/dim]")

    async def _cmd_load(self, path: str, *_):
        if not path:
            _print("[yellow]Usage: /load <file_path>[/yellow]")
            return
        if not hasattr(self.agent, 'rag'):
            _print("[yellow]RAG not initialized. Add RAGPipeline to OmniAgent.[/yellow]")
            return
        try:
            doc = await self.agent.rag.ingest_file(path.strip())
            _print(f"[green]✓ Loaded '{doc.title}' → {doc.total_chunks} chunks[/green]")
        except Exception as e:
            _print(f"[red]Load failed: {e}[/red]")

    async def _cmd_loaddir(self, path: str, *_):
        if not path or not hasattr(self.agent, 'rag'):
            _print("[yellow]Usage: /loaddir <directory_path>[/yellow]")
            return
        docs = await self.agent.rag.ingest_directory(path.strip())
        _print(f"[green]✓ Loaded {len(docs)} documents[/green]")

    async def _cmd_docs(self, *_):
        if not hasattr(self.agent, 'rag'):
            _print("[yellow]RAG not initialized.[/yellow]")
            return
        docs = self.agent.rag.list_documents()
        if not docs:
            _print("[dim]No documents ingested.[/dim]")
            return
        rows = [(d["id"], d["title"], d["doc_type"],
                 str(d["total_chunks"])) for d in docs]
        _table("Ingested Documents", ["ID", "Title", "Type", "Chunks"], rows)

    async def _cmd_rag(self, question: str, *_):
        if not question or not hasattr(self.agent, 'rag'):
            _print("[yellow]Usage: /rag <question>[/yellow]")
            return
        results = await self.agent.rag.retrieve(question, top_k=3)
        context = self.agent.rag.generate_context(results)
        if not context:
            _print("[yellow]No relevant documents found.[/yellow]")
            return
        augmented = (f"Answer using this context:\n{context}\n\nQuestion: {question}")
        await self._handle_message(augmented)

    async def _cmd_pipelines(self, *_):
        from agent.pipeline import PipelineExecutor
        if not hasattr(self.agent, 'pipeline_executor'):
            _print("[yellow]Pipeline executor not initialized.[/yellow]")
            return
        rows = [(p["name"], p["description"], str(p["steps"]))
                for p in self.agent.pipeline_executor.list_pipelines()]
        _table("Registered Pipelines", ["Name", "Description", "Steps"], rows)

    async def _cmd_run(self, name: str, args_str: str = ""):
        if not name or not hasattr(self.agent, 'pipeline_executor'):
            _print("[yellow]Usage: /run <pipeline_name> [{'key':'val'}][/yellow]")
            return
        import json as _json
        ctx = {}
        if args_str:
            try:
                ctx = _json.loads(args_str)
            except Exception:
                ctx = {"input": args_str}

        _print(f"[dim]Running pipeline '{name}'...[/dim]")
        run = await self.agent.pipeline_executor.run_by_name(name, ctx)
        if not run:
            _print(f"[red]Pipeline '{name}' not found.[/red]")
            return

        rows = [(s["name"], s["status"], f"{s['duration_ms']:.0f}ms",
                 s["error"][:40] if s["error"] else "")
                for s in run.steps]
        _table(f"Pipeline Run: {run.run_id}", ["Step", "Status", "Duration", "Error"], rows)
        _print(f"\nFinal status: [bold {'green' if run.status.value=='success' else 'red'}]"
              f"{run.status.value}[/bold] | {run.duration_ms:.0f}ms total")

    async def _cmd_runs(self, *_):
        if not hasattr(self.agent, 'pipeline_executor'):
            _print("[yellow]Pipeline executor not initialized.[/yellow]")
            return
        runs = self.agent.pipeline_executor.list_runs()[:10]
        rows = [(r["run_id"], r["pipeline"], r["status"],
                 f"{r['duration_ms']:.0f}ms") for r in runs]
        _table("Recent Pipeline Runs", ["Run ID", "Pipeline", "Status", "Duration"], rows)

    async def _cmd_templates(self, *_):
        if not hasattr(self.agent, 'templates'):
            _print("[yellow]Template registry not initialized.[/yellow]")
            return
        rows = [(t["name"], t["description"], ", ".join(t["variables"]),
                 t["model_hint"] or "auto") for t in self.agent.templates.list_templates()]
        _table("Prompt Templates", ["Name", "Description", "Variables", "Model Hint"], rows)

    async def _cmd_template(self, name: str, *_):
        if not name or not hasattr(self.agent, 'templates'):
            _print("[yellow]Usage: /template <name>[/yellow]")
            return
        tmpl = self.agent.templates.get(name)
        if not tmpl:
            _print(f"[red]Template '{name}' not found.[/red]")
            return
        _panel(
            f"[bold]Variables:[/bold] {list(tmpl.get_variables().keys())}\n"
            f"[bold]Model hint:[/bold] {tmpl.model_hint or 'auto'}\n"
            f"[bold]Tags:[/bold] {', '.join(tmpl.tags)}\n\n"
            f"[bold]Template:[/bold]\n{tmpl.template}",
            title=f"📝 {name} v{tmpl.version}", style="yellow"
        )

    async def _cmd_use(self, name: str, vars_str: str = ""):
        if not name or not hasattr(self.agent, 'templates'):
            _print("[yellow]Usage: /use <template_name> {'var': 'value'}[/yellow]")
            return
        import json as _json
        variables = {}
        if vars_str:
            try:
                variables = _json.loads(vars_str)
            except Exception:
                _print("[red]Variables must be valid JSON. Example: {'text': 'hello'}[/red]")
                return
        try:
            messages = self.agent.templates.render(name, variables)
            user_msg = messages[-1]["content"]
            await self._handle_message(user_msg)
        except (KeyError, ValueError) as e:
            _print(f"[red]{e}[/red]")

    async def _cmd_status(self, *_):
        status = self.agent.memory.get_state("agent_status") or {}
        health = self.agent.heartbeat.last_status
        router = self.agent.llm.get_router_summary()
        stats_str = (
            f"State:   {status.get('state','?')}\n"
            f"Models:  {router['total_models']} registered\n"
            f"Session: {self.session_id}\n"
            f"Model:   {self._current_model}\n"
            f"Health:  {'✓' if health.get('healthy', True) else '✗'} "
            f"{health.get('checks', {})}"
        )
        _panel(stats_str, title="📊 System Status", style="green")

    async def _cmd_stats(self, *_):
        stats = self.agent.llm.get_stats()
        if not stats:
            _print("[dim]No model calls recorded yet.[/dim]")
            return
        rows = [
            (s["model_id"].replace(":cloud",""),
             str(s["total_calls"]),
             str(s["successful_calls"]),
             f"{s['avg_latency_ms']:.0f}ms",
             f"{s['success_rate']:.0%}")
            for s in sorted(stats, key=lambda s: s["total_calls"], reverse=True)
        ]
        _table("Model Usage Statistics",
               ["Model", "Total", "Success", "Avg Latency", "Rate"], rows)

    async def _cmd_skills(self, *_):
        skills = self.agent.skills.list_skills()
        rows = [(s["name"], s["description"][:50], str(s["call_count"]),
                 "✓" if s["enabled"] else "✗") for s in skills]
        _table("Loaded Skills", ["Name", "Description", "Calls", "Enabled"], rows)

    async def _cmd_memory(self, query: str, *_):
        if not query:
            _print("[yellow]Usage: /memory <search query>[/yellow]")
            return
        results = self.agent.memory.search_memories(query, limit=10)
        if not results:
            _print("[dim]No memories found.[/dim]")
            return
        rows = [(r["key"], str(r["value"])[:60], r.get("category",""),
                 str(r.get("importance",""))) for r in results]
        _table(f"Memory Search: '{query}'",
               ["Key", "Value", "Category", "Importance"], rows)

    async def _cmd_chronicle(self, subcommand: str = "", *_):
        """Review session history and get personalized tips."""
        if subcommand.lower() != "tips" and subcommand != "":
            _print("[yellow]Usage: /chronicle tips[/yellow]")
            return

        from agent.chronicle_analyzer import ChronicleAnalyzer

        analyzer = ChronicleAnalyzer(self.agent.memory)
        result = analyzer.analyze_all_sessions()

        # Display personalized tips
        if result.get("tips"):
            _print("\n[bold cyan]✨ Personalized Tips for You:[/bold cyan]")
            for i, tip in enumerate(result["tips"], 1):
                _print(f"  {i}. {tip}")

        # Display patterns detected
        patterns = result.get("patterns", [])
        if patterns:
            _print("\n[bold cyan]📊 Usage Patterns Detected:[/bold cyan]")
            for pattern in patterns:
                name = pattern.get("name", "Unknown")
                _print(f"\n  [bold]{name}[/bold]")

                if "short" in pattern:
                    _print(f"    • Short sessions: {pattern['short']}%")
                    _print(f"    • Medium sessions: {pattern['medium']}%")
                    _print(f"    • Long sessions: {pattern['long']}%")
                elif "tasks" in pattern:
                    for task in pattern["tasks"]:
                        _print(f"    • {task['type']}: {task['frequency']} occurrences")
                elif "domains" in pattern:
                    _print(f"    • Focus areas: {', '.join(pattern['domains'])}")

        # Display aggregate statistics
        stats = result.get("stats", {})
        if stats:
            _print("\n[bold cyan]📈 Your Statistics:[/bold cyan]")
            rows = [
                ("Total Sessions", stats.get("total_sessions", 0)),
                ("Total Messages", stats.get("total_messages", 0)),
                ("Total Turns", stats.get("total_turns", 0)),
                ("Avg Session Length", f"{stats.get('avg_session_length', 0)} messages"),
                ("Avg Turns/Session", f"{stats.get('avg_turns_per_session', 0)} turns"),
            ]
            for key, value in rows:
                _print(f"  • {key}: {value}")

    async def _cmd_quit(self, *_):
        _print("[dim]Shutting down...[/dim]")
        self._running = False
