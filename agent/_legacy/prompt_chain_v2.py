"""OMNI Agent — Prompt Chain V2: chaining with branching, memory, output parsing."""
from __future__ import annotations
import json, re, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class ChainNodeType(str, Enum):
    PROMPT   = "prompt"
    BRANCH   = "branch"
    MEMORY   = "memory"
    PARSER   = "parser"
    TRANSFORM = "transform"
    STOP     = "stop"


class OutputFormat(str, Enum):
    TEXT    = "text"
    JSON    = "json"
    LIST    = "list"
    BOOL    = "bool"
    NUMBER  = "number"


@dataclass
class ChainNode:
    node_id: str
    name: str
    node_type: ChainNodeType = ChainNodeType.PROMPT
    prompt_template: str = ""
    output_format: OutputFormat = OutputFormat.TEXT
    next_node_id: Optional[str] = None
    branch_fn: Optional[Callable[[Any], str]] = None  # returns next_node_id
    transform_fn: Optional[Callable[[Any, Dict], Any]] = None
    parser_fn: Optional[Callable[[str], Any]] = None
    memory_key: Optional[str] = None   # store output under this key
    memory_inject: List[str] = field(default_factory=list)  # keys to inject
    max_tokens: int = 500
    temperature: float = 0.7
    metadata: Dict[str, Any] = field(default_factory=dict)

    def render(self, variables: Dict[str, Any]) -> str:
        template = self.prompt_template
        for k, v in variables.items():
            template = template.replace(f"{{{k}}}", str(v))
        return template

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "type": self.node_type.value,
            "output_format": self.output_format.value,
        }


@dataclass
class ChainRun:
    run_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    chain_id: str = ""
    status: str = "pending"
    nodes_visited: List[str] = field(default_factory=list)
    outputs: Dict[str, Any] = field(default_factory=dict)
    memory: Dict[str, Any] = field(default_factory=dict)
    final_output: Any = None
    errors: Dict[str, str] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    input_vars: Dict[str, Any] = field(default_factory=dict)
    token_usage: int = 0

    @property
    def duration_ms(self) -> float:
        end = self.finished_at or time.time()
        return (end - self.started_at) * 1000

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "nodes_visited": self.nodes_visited,
            "duration_ms": round(self.duration_ms, 2),
            "token_usage": self.token_usage,
        }


@dataclass
class PromptChainDef:
    chain_id: str
    name: str
    description: str = ""
    entry_node_id: str = ""
    nodes: Dict[str, ChainNode] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    run_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "name": self.name,
            "nodes": len(self.nodes),
            "run_count": self.run_count,
        }


class PromptChainV2:
    """
    Prompt chain engine:
    - Define chains as graphs of prompt nodes
    - Node types: PROMPT / BRANCH / MEMORY / PARSER / TRANSFORM / STOP
    - Template variable injection ({var} substitution)
    - Branching (conditional next-node routing by output)
    - Memory: store outputs, inject into future prompts
    - Output parsing: text / JSON / list / bool / number
    - Pluggable LLM function per chain or globally
    - Token usage tracking
    - Max-depth protection (loop prevention)
    - Run history with full node trace
    - Named chains library
    - SQLite persistence
    """

    def __init__(self, llm_fn: Optional[Callable[[str, Dict], str]] = None,
                 db_path: str = ":memory:",
                 max_depth: int = 50):
        self._chains:  Dict[str, PromptChainDef] = {}
        self._runs:    List[ChainRun] = []
        self._llm_fn   = llm_fn
        self._max_depth = max_depth
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS pc_chains (
                chain_id TEXT PRIMARY KEY, name TEXT,
                description TEXT, run_count INTEGER, created_at REAL
            );
            CREATE TABLE IF NOT EXISTS pc_runs (
                run_id TEXT PRIMARY KEY, chain_id TEXT, status TEXT,
                nodes_visited INTEGER, token_usage INTEGER,
                started_at REAL, finished_at REAL
            );
        """)
        self._db.commit()

    # ── CHAIN BUILDER ─────────────────────────────────────────────────

    def create_chain(self, name: str,
                      description: str = "",
                      tags: Optional[List[str]] = None,
                      chain_id: Optional[str] = None) -> PromptChainDef:
        cid = chain_id or str(uuid.uuid4())[:8]
        c   = PromptChainDef(chain_id=cid, name=name,
                               description=description,
                               tags=list(tags or []))
        self._chains[cid] = c
        self._persist_chain(c)
        return c

    def add_node(self, chain_id: str,
                  name: str,
                  node_type: ChainNodeType = ChainNodeType.PROMPT,
                  prompt_template: str = "",
                  output_format: OutputFormat = OutputFormat.TEXT,
                  next_node_id: Optional[str] = None,
                  branch_fn: Optional[Callable] = None,
                  transform_fn: Optional[Callable] = None,
                  parser_fn: Optional[Callable] = None,
                  memory_key: Optional[str] = None,
                  memory_inject: Optional[List[str]] = None,
                  node_id: Optional[str] = None,
                  metadata: Optional[Dict] = None,
                  **kwargs) -> ChainNode:
        c   = self._chains.get(chain_id)
        if not c: raise KeyError(f"Chain {chain_id} not found")
        nid = node_id or str(uuid.uuid4())[:8]
        n   = ChainNode(
            node_id=nid, name=name, node_type=node_type,
            prompt_template=prompt_template,
            output_format=output_format,
            next_node_id=next_node_id,
            branch_fn=branch_fn, transform_fn=transform_fn,
            parser_fn=parser_fn, memory_key=memory_key,
            memory_inject=list(memory_inject or []),
            metadata=metadata or {},
            **{k: v for k, v in kwargs.items()
               if k in ('max_tokens', 'temperature')})
        c.nodes[nid] = n
        if not c.entry_node_id:
            c.entry_node_id = nid
        return n

    def set_entry(self, chain_id: str, node_id: str):
        c = self._chains.get(chain_id)
        if c: c.entry_node_id = node_id

    def get_chain(self, chain_id: str) -> Optional[PromptChainDef]:
        return self._chains.get(chain_id)

    def list_chains(self) -> List[Dict]:
        return [c.to_dict() for c in self._chains.values()]

    # ── EXECUTION ─────────────────────────────────────────────────────

    def run(self, chain_id: str,
             variables: Optional[Dict[str, Any]] = None,
             llm_fn: Optional[Callable] = None,
             run_id: Optional[str] = None) -> ChainRun:
        c = self._chains.get(chain_id)
        if not c: raise KeyError(f"Chain {chain_id} not found")
        if not c.entry_node_id:
            raise ValueError("Chain has no entry node")

        fn  = llm_fn or self._llm_fn
        run = ChainRun(run_id=run_id or str(uuid.uuid4())[:8],
                        chain_id=chain_id,
                        input_vars=dict(variables or {}))
        run.status = "running"
        vars_ctx   = dict(variables or {})
        cur_id: Optional[str] = c.entry_node_id
        depth      = 0

        while cur_id and depth < self._max_depth:
            node = c.nodes.get(cur_id)
            if not node: break
            depth    += 1
            run.nodes_visited.append(cur_id)
            last_output = run.outputs.get(
                run.nodes_visited[-2] if len(run.nodes_visited) > 1 else cur_id)

            # Inject memory into vars
            for mk in node.memory_inject:
                if mk in run.memory:
                    vars_ctx[mk] = run.memory[mk]

            try:
                output = self._exec_node(node, vars_ctx, run.memory,
                                          last_output, fn, run)
            except Exception as exc:
                run.errors[cur_id] = str(exc)
                run.status = "failed"
                break

            run.outputs[cur_id] = output

            # Store to memory
            if node.memory_key:
                run.memory[node.memory_key] = output
                vars_ctx[node.memory_key]   = output

            # Determine next node
            if node.node_type == ChainNodeType.STOP:
                break
            elif node.node_type == ChainNodeType.BRANCH and node.branch_fn:
                try:
                    cur_id = node.branch_fn(output)
                except Exception:
                    cur_id = node.next_node_id
            else:
                cur_id = node.next_node_id

        run.final_output = (run.outputs.get(run.nodes_visited[-1])
                             if run.nodes_visited else None)
        if run.status == "running":
            run.status = "done"
        run.finished_at = time.time()
        self._runs.append(run)
        c.run_count += 1
        self._persist_run(run)
        return run

    def _exec_node(self, node: ChainNode,
                    vars_ctx: Dict, memory: Dict,
                    prev_output: Any,
                    fn: Optional[Callable],
                    run: ChainRun) -> Any:

        if node.node_type == ChainNodeType.TRANSFORM:
            if node.transform_fn:
                return node.transform_fn(prev_output, vars_ctx)
            return prev_output

        if node.node_type == ChainNodeType.PARSER:
            if node.parser_fn:
                return node.parser_fn(prev_output)
            return self._parse_output(str(prev_output), node.output_format)

        if node.node_type == ChainNodeType.MEMORY:
            # Just store + pass through
            return prev_output

        if node.node_type == ChainNodeType.STOP:
            return prev_output

        # PROMPT / BRANCH node → call LLM
        prompt   = node.render(vars_ctx)
        if fn:
            raw  = fn(prompt, {"max_tokens": node.max_tokens,
                                "temperature": node.temperature})
            run.token_usage += len(prompt.split()) + len(str(raw).split())
        else:
            # No LLM: echo prompt (useful for testing)
            raw  = f"[echo] {prompt[:100]}"
        output = self._parse_output(str(raw), node.output_format)
        if node.parser_fn:
            try: output = node.parser_fn(output)
            except Exception: pass
        return output

    def _parse_output(self, raw: str, fmt: OutputFormat) -> Any:
        if fmt == OutputFormat.TEXT:
            return raw.strip()
        if fmt == OutputFormat.JSON:
            try:
                clean = re.sub(r"```json|```", "", raw).strip()
                return json.loads(clean)
            except Exception:
                return raw
        if fmt == OutputFormat.LIST:
            items = re.findall(r"[-*•]\s*(.+)", raw)
            return items if items else [l.strip() for l in raw.split("\n") if l.strip()]
        if fmt == OutputFormat.BOOL:
            return raw.lower().strip() in ("true", "yes", "1", "correct")
        if fmt == OutputFormat.NUMBER:
            nums = re.findall(r"-?\d+\.?\d*", raw)
            return float(nums[0]) if nums else 0.0
        return raw

    # ── QUERY ─────────────────────────────────────────────────────────

    def run_history(self, chain_id: Optional[str] = None,
                    limit: int = 20) -> List[Dict]:
        runs = self._runs
        if chain_id:
            runs = [r for r in runs if r.chain_id == chain_id]
        return [r.to_dict() for r in runs[-limit:]]

    def _persist_chain(self, c: PromptChainDef):
        self._db.execute(
            "INSERT OR REPLACE INTO pc_chains VALUES (?,?,?,?,?)",
            (c.chain_id, c.name, c.description, c.run_count, c.created_at))
        self._db.commit()

    def _persist_run(self, r: ChainRun):
        self._db.execute(
            "INSERT OR REPLACE INTO pc_runs VALUES (?,?,?,?,?,?,?)",
            (r.run_id, r.chain_id, r.status,
             len(r.nodes_visited), r.token_usage,
             r.started_at, r.finished_at))
        self._db.commit()

    def stats(self) -> Dict[str, Any]:
        return {
            "chains": len(self._chains),
            "runs": len(self._runs),
            "total_tokens": sum(r.token_usage for r in self._runs),
        }
