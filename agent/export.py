"""
OMNI AGENT - Export System
Export agent data (conversations, memories, traces, knowledge graph,
eval results) to JSON, Markdown, CSV, and styled HTML reports.

Supported exports:
  - Conversations  → Markdown chat log / JSON / HTML
  - Memories       → JSON / CSV
  - Traces         → JSON / CSV timeline / HTML flamegraph
  - Knowledge Graph→ JSON / DOT (Graphviz) / Cypher (Neo4j)
  - Eval results   → Markdown report / JSON / CSV
  - Full dump      → ZIP archive of all data

All exports are returned as strings or bytes — the caller decides
where to write them (file, response, attachment, etc.).
"""
import io
import csv
import json
import time
import zipfile
import textwrap
import logging
from typing import Any, Dict, List, Optional
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class ExportFormat(str, Enum):
    JSON     = "json"
    MARKDOWN = "markdown"
    CSV      = "csv"
    HTML     = "html"
    DOT      = "dot"
    CYPHER   = "cypher"
    ZIP      = "zip"


# ══════════════════════════════════════════════════════════════════════════════
# CONVERSATION EXPORT
# ══════════════════════════════════════════════════════════════════════════════

class ConversationExporter:

    def to_markdown(self, messages: List[Dict], session_id: str = "",
                    title: str = "") -> str:
        lines = []
        if title:
            lines.append(f"# {title}\n")
        else:
            lines.append(f"# Conversation Export\n")
        if session_id:
            lines.append(f"**Session:** `{session_id}`  \n")
        lines.append(f"**Exported:** {_ts()}  \n")
        lines.append(f"**Messages:** {len(messages)}\n")
        lines.append("\n---\n")

        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            ts = msg.get("timestamp", "")
            ts_str = f" *({_fmt_time(ts)})*" if ts else ""
            if role == "user":
                lines.append(f"\n**🧑 User**{ts_str}\n\n{content}\n")
            elif role == "assistant":
                lines.append(f"\n**🤖 Assistant**{ts_str}\n\n{content}\n")
            else:
                lines.append(f"\n**{role.title()}**{ts_str}\n\n{content}\n")
            lines.append("\n---\n")

        return "\n".join(lines)

    def to_json(self, messages: List[Dict], session_id: str = "",
                metadata: Dict = None) -> str:
        payload = {
            "session_id": session_id,
            "exported_at": time.time(),
            "message_count": len(messages),
            "metadata": metadata or {},
            "messages": messages,
        }
        return json.dumps(payload, indent=2, ensure_ascii=False)

    def to_html(self, messages: List[Dict], session_id: str = "",
                title: str = "") -> str:
        title = title or f"Conversation — {session_id or 'Export'}"
        rows = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "").replace("<", "&lt;").replace(">", "&gt;")
            content_html = content.replace("\n", "<br>")
            ts = msg.get("timestamp", "")
            ts_str = f'<span class="ts">{_fmt_time(ts)}</span>' if ts else ""
            cls = "user" if role == "user" else ("assistant" if role == "assistant" else "system")
            rows.append(f"""
        <div class="message {cls}">
          <div class="role">{role.title()} {ts_str}</div>
          <div class="content">{content_html}</div>
        </div>""")

        body = "\n".join(rows)
        return _html_wrap(title, f'<div class="conversation">{body}</div>', _CONV_CSS)

    def to_csv(self, messages: List[Dict]) -> str:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=["index", "role", "content", "timestamp"],
                                extrasaction="ignore")
        writer.writeheader()
        for i, msg in enumerate(messages):
            writer.writerow({
                "index": i,
                "role": msg.get("role", ""),
                "content": msg.get("content", "")[:500],
                "timestamp": _fmt_time(msg.get("timestamp", "")),
            })
        return buf.getvalue()


# ══════════════════════════════════════════════════════════════════════════════
# MEMORY EXPORT
# ══════════════════════════════════════════════════════════════════════════════

class MemoryExporter:

    def to_json(self, memories: List[Dict]) -> str:
        return json.dumps({
            "exported_at": time.time(),
            "count": len(memories),
            "memories": memories,
        }, indent=2, ensure_ascii=False)

    def to_csv(self, memories: List[Dict]) -> str:
        if not memories:
            return "key,value,category,importance,created_at\n"
        fields = ["key", "value", "category", "importance", "created_at"]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for m in memories:
            row = {k: m.get(k, "") for k in fields}
            if isinstance(row.get("value"), (dict, list)):
                row["value"] = json.dumps(row["value"])
            writer.writerow(row)
        return buf.getvalue()

    def to_markdown(self, memories: List[Dict]) -> str:
        lines = [
            "# Memory Export\n",
            f"**Count:** {len(memories)}  ",
            f"**Exported:** {_ts()}\n",
            "\n---\n",
        ]
        # Group by category
        by_cat: Dict[str, List] = {}
        for m in memories:
            cat = m.get("category", "general")
            by_cat.setdefault(cat, []).append(m)

        for cat, items in sorted(by_cat.items()):
            lines.append(f"\n## {cat.title()}\n")
            for m in items:
                key = m.get("key", "")
                val = m.get("value", "")
                imp = m.get("importance", "")
                imp_str = f" *(importance: {imp})*" if imp else ""
                if isinstance(val, (dict, list)):
                    val = f"```json\n{json.dumps(val, indent=2)}\n```"
                lines.append(f"**{key}**{imp_str}  \n{val}\n")

        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# TRACE EXPORT
# ══════════════════════════════════════════════════════════════════════════════

class TraceExporter:

    def to_json(self, spans: List[Dict]) -> str:
        return json.dumps({
            "exported_at": time.time(),
            "span_count": len(spans),
            "spans": spans,
        }, indent=2, ensure_ascii=False)

    def to_csv(self, spans: List[Dict]) -> str:
        fields = ["span_id", "trace_id", "parent_id", "name", "kind",
                  "status", "duration_ms", "model", "cost_usd", "started_at"]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for s in spans:
            row = {k: s.get(k, "") for k in fields}
            writer.writerow(row)
        return buf.getvalue()

    def to_markdown(self, spans: List[Dict], summary: Dict = None) -> str:
        lines = [
            "# Trace Export\n",
            f"**Spans:** {len(spans)}  ",
            f"**Exported:** {_ts()}\n",
        ]
        if summary:
            lines += [
                "\n## Summary\n",
                f"- Total spans: {summary.get('total_spans', len(spans))}",
                f"- Total cost: ${summary.get('total_cost_usd', 0):.4f}",
                f"- P50 latency: {summary.get('p50_latency_ms', 0):.0f}ms",
                f"- P95 latency: {summary.get('p95_latency_ms', 0):.0f}ms\n",
            ]

        lines.append("\n## Spans\n")
        lines.append("| Span | Name | Kind | Status | Duration | Model | Cost |")
        lines.append("|------|------|------|--------|----------|-------|------|")
        for s in spans:
            dur = f"{s.get('duration_ms', 0):.0f}ms"
            cost = f"${s.get('cost_usd', 0):.4f}" if s.get("cost_usd") else "-"
            lines.append(
                f"| {s.get('span_id','')[:8]} "
                f"| {s.get('name','')} "
                f"| {s.get('kind','')} "
                f"| {s.get('status','')} "
                f"| {dur} "
                f"| {s.get('model','-')} "
                f"| {cost} |"
            )

        return "\n".join(lines)

    def to_html_timeline(self, spans: List[Dict]) -> str:
        """Generate an HTML timeline visualization of spans."""
        if not spans:
            return _html_wrap("Trace Timeline", "<p>No spans.</p>", "")

        # Find time range
        starts = [s.get("started_at", 0) for s in spans if s.get("started_at")]
        if not starts:
            return _html_wrap("Trace Timeline", "<p>No timing data.</p>", "")

        t_min = min(starts)
        t_max = max(
            s.get("started_at", t_min) + s.get("duration_ms", 0) / 1000
            for s in spans
        )
        total_ms = max((t_max - t_min) * 1000, 1)

        rows = []
        for s in spans:
            name = s.get("name", "span")
            start_ms = (s.get("started_at", t_min) - t_min) * 1000
            dur_ms = s.get("duration_ms", 1)
            left_pct = start_ms / total_ms * 100
            width_pct = max(dur_ms / total_ms * 100, 0.5)
            status = s.get("status", "ok")
            color = "#e74c3c" if status == "error" else "#3498db"
            model = s.get("model", "")
            tooltip = f"{name} | {dur_ms:.0f}ms | {model}"
            rows.append(f"""
      <div class="span-row">
        <div class="span-label">{name[:30]}</div>
        <div class="span-track">
          <div class="span-bar" title="{tooltip}"
               style="left:{left_pct:.2f}%;width:{width_pct:.2f}%;background:{color}">
          </div>
        </div>
        <div class="span-dur">{dur_ms:.0f}ms</div>
      </div>""")

        body = f'<div class="timeline">{"".join(rows)}</div>'
        return _html_wrap("Trace Timeline", body, _TIMELINE_CSS)


# ══════════════════════════════════════════════════════════════════════════════
# KNOWLEDGE GRAPH EXPORT
# ══════════════════════════════════════════════════════════════════════════════

class KGExporter:

    def to_json(self, data: Dict) -> str:
        return json.dumps(data, indent=2, ensure_ascii=False)

    def to_dot(self, data: Dict, graph_name: str = "omni_kg") -> str:
        """Export as Graphviz DOT format."""
        lines = [f'digraph {graph_name} {{']
        lines.append('  rankdir=LR;')
        lines.append('  node [shape=box, style=filled, fillcolor=lightblue];')

        entity_map = {e["id"]: e["name"] for e in data.get("entities", [])}

        # Type-based colors
        type_colors = {
            "PERSON": "lightyellow",
            "ORG": "lightgreen",
            "LOCATION": "lightpink",
            "CONCEPT": "lightsalmon",
            "TECH": "lavender",
        }

        for e in data.get("entities", []):
            label = e["name"].replace('"', '\\"')
            color = type_colors.get(e.get("type", ""), "lightblue")
            eid = e["id"].replace("-", "_")
            lines.append(f'  {eid} [label="{label}", fillcolor="{color}"];')

        lines.append("")
        for r in data.get("relationships", []):
            src = r["source"].replace("-", "_")
            tgt = r["target"].replace("-", "_")
            label = r.get("label", "").replace('"', '\\"')
            lines.append(f'  {src} -> {tgt} [label="{label}"];')

        lines.append("}")
        return "\n".join(lines)

    def to_cypher(self, data: Dict) -> str:
        """Export as Neo4j Cypher CREATE statements."""
        lines = ["// OMNI Agent Knowledge Graph — Cypher Export", ""]

        for e in data.get("entities", []):
            name = e["name"].replace("'", "\\'")
            etype = e.get("type", "Entity")
            eid = e["id"]
            desc = e.get("description", "").replace("'", "\\'")
            lines.append(
                f"CREATE (e_{eid.replace('-','_')}:{etype} "
                f"{{id: '{eid}', name: '{name}', description: '{desc}'}})"
            )

        lines.append("")
        # We need to reference nodes by var names
        id_to_var = {e["id"]: f"e_{e['id'].replace('-','_')}"
                     for e in data.get("entities", [])}

        for r in data.get("relationships", []):
            src_var = id_to_var.get(r["source"], "")
            tgt_var = id_to_var.get(r["target"], "")
            if not src_var or not tgt_var:
                continue
            label = r.get("label", "RELATED_TO").replace(" ", "_").upper()
            desc = r.get("description", "").replace("'", "\\'")
            lines.append(
                f"CREATE ({src_var})-[:{label} {{description: '{desc}'}}]->({tgt_var})"
            )

        return "\n".join(lines)

    def to_html(self, data: Dict) -> str:
        """Simple HTML table of entities and relationships."""
        ents = data.get("entities", [])
        rels = data.get("relationships", [])
        entity_name = {e["id"]: e["name"] for e in ents}

        e_rows = "".join(
            f"<tr><td>{e.get('name','')}</td><td>{e.get('type','')}</td>"
            f"<td>{e.get('description','')[:80]}</td></tr>"
            for e in ents
        )
        r_rows = "".join(
            f"<tr><td>{entity_name.get(r.get('source',''),r.get('source',''))}</td>"
            f"<td><strong>{r.get('label','')}</strong></td>"
            f"<td>{entity_name.get(r.get('target',''),r.get('target',''))}</td>"
            f"<td>{r.get('description','')[:60]}</td></tr>"
            for r in rels
        )
        body = f"""
        <h2>Entities ({len(ents)})</h2>
        <table border="1" cellpadding="6">
          <tr><th>Name</th><th>Type</th><th>Description</th></tr>
          {e_rows}
        </table>
        <h2>Relationships ({len(rels)})</h2>
        <table border="1" cellpadding="6">
          <tr><th>Source</th><th>Relation</th><th>Target</th><th>Description</th></tr>
          {r_rows}
        </table>"""
        return _html_wrap("Knowledge Graph Export", body, _TABLE_CSS)


# ══════════════════════════════════════════════════════════════════════════════
# MASTER EXPORTER
# ══════════════════════════════════════════════════════════════════════════════

class Exporter:
    """
    Unified export interface for the OMNI Agent.

    Usage:
        exp = Exporter(agent)

        md = exp.export_conversation("session_123", fmt=ExportFormat.MARKDOWN)
        html = exp.export_traces(fmt=ExportFormat.HTML)
        dot = exp.export_kg(fmt=ExportFormat.DOT)
        archive = exp.full_dump()   # returns ZIP bytes
    """

    def __init__(self, agent=None):
        self.agent = agent
        self.conv_exp   = ConversationExporter()
        self.mem_exp    = MemoryExporter()
        self.trace_exp  = TraceExporter()
        self.kg_exp     = KGExporter()

    # ── Conversations ─────────────────────────────────────────────────────────

    def export_conversation(self, session_id: str,
                             fmt: ExportFormat = ExportFormat.MARKDOWN) -> str:
        messages = []
        if self.agent:
            try:
                history = self.agent.memory.get_history(session_id, limit=1000)
                messages = [{"role": m["role"], "content": m["content"],
                            "timestamp": m.get("timestamp")}
                           for m in history]
            except Exception as e:
                logger.warning(f"Could not load conversation: {e}")

        if fmt == ExportFormat.MARKDOWN:
            return self.conv_exp.to_markdown(messages, session_id)
        if fmt == ExportFormat.JSON:
            return self.conv_exp.to_json(messages, session_id)
        if fmt == ExportFormat.HTML:
            return self.conv_exp.to_html(messages, session_id)
        if fmt == ExportFormat.CSV:
            return self.conv_exp.to_csv(messages)
        return self.conv_exp.to_json(messages, session_id)

    # ── Memories ──────────────────────────────────────────────────────────────

    def export_memories(self, fmt: ExportFormat = ExportFormat.JSON,
                         category: str = None) -> str:
        memories = []
        if self.agent:
            try:
                memories = self.agent.memory.get_all_memories(category=category)
            except Exception as e:
                logger.warning(f"Could not load memories: {e}")

        if fmt == ExportFormat.JSON:
            return self.mem_exp.to_json(memories)
        if fmt == ExportFormat.CSV:
            return self.mem_exp.to_csv(memories)
        if fmt == ExportFormat.MARKDOWN:
            return self.mem_exp.to_markdown(memories)
        return self.mem_exp.to_json(memories)

    # ── Traces ────────────────────────────────────────────────────────────────

    def export_traces(self, fmt: ExportFormat = ExportFormat.JSON,
                       last_n: int = 500) -> str:
        spans = []
        summary = {}
        if self.agent:
            try:
                spans = [s.__dict__ if hasattr(s, "__dict__") else s
                        for s in self.agent.tracer.get_spans()[-last_n:]]
                summary = self.agent.tracer.summary()
            except Exception as e:
                logger.warning(f"Could not load traces: {e}")

        if fmt == ExportFormat.JSON:
            return self.trace_exp.to_json(spans)
        if fmt == ExportFormat.CSV:
            return self.trace_exp.to_csv(spans)
        if fmt == ExportFormat.MARKDOWN:
            return self.trace_exp.to_markdown(spans, summary)
        if fmt == ExportFormat.HTML:
            return self.trace_exp.to_html_timeline(spans)
        return self.trace_exp.to_json(spans)

    # ── Knowledge Graph ───────────────────────────────────────────────────────

    def export_kg(self, fmt: ExportFormat = ExportFormat.JSON) -> str:
        data = {"entities": [], "relationships": []}
        if self.agent:
            try:
                data = self.agent.knowledge_graph.export()
            except Exception as e:
                logger.warning(f"Could not export KG: {e}")

        if fmt == ExportFormat.JSON:
            return self.kg_exp.to_json(data)
        if fmt == ExportFormat.DOT:
            return self.kg_exp.to_dot(data)
        if fmt == ExportFormat.CYPHER:
            return self.kg_exp.to_cypher(data)
        if fmt == ExportFormat.HTML:
            return self.kg_exp.to_html(data)
        return self.kg_exp.to_json(data)

    # ── Full ZIP dump ─────────────────────────────────────────────────────────

    def full_dump(self, session_ids: List[str] = None) -> bytes:
        """
        Create a ZIP archive with all agent data.
        Returns raw bytes suitable for HTTP response or file write.
        """
        buf = io.BytesIO()
        ts = time.strftime("%Y%m%d_%H%M%S")

        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            # Memories
            zf.writestr(f"omni_export_{ts}/memories.json",
                        self.export_memories(ExportFormat.JSON))
            zf.writestr(f"omni_export_{ts}/memories.csv",
                        self.export_memories(ExportFormat.CSV))
            zf.writestr(f"omni_export_{ts}/memories.md",
                        self.export_memories(ExportFormat.MARKDOWN))

            # Traces
            zf.writestr(f"omni_export_{ts}/traces.json",
                        self.export_traces(ExportFormat.JSON))
            zf.writestr(f"omni_export_{ts}/traces.csv",
                        self.export_traces(ExportFormat.CSV))
            zf.writestr(f"omni_export_{ts}/traces_timeline.html",
                        self.export_traces(ExportFormat.HTML))

            # Knowledge Graph
            zf.writestr(f"omni_export_{ts}/knowledge_graph.json",
                        self.export_kg(ExportFormat.JSON))
            zf.writestr(f"omni_export_{ts}/knowledge_graph.dot",
                        self.export_kg(ExportFormat.DOT))
            zf.writestr(f"omni_export_{ts}/knowledge_graph.cypher",
                        self.export_kg(ExportFormat.CYPHER))

            # Conversations
            conv_dir = f"omni_export_{ts}/conversations"
            sessions = session_ids or self._list_sessions()
            for sid in sessions:
                safe = sid.replace("/", "_").replace(":", "_")
                zf.writestr(f"{conv_dir}/{safe}.md",
                            self.export_conversation(sid, ExportFormat.MARKDOWN))
                zf.writestr(f"{conv_dir}/{safe}.json",
                            self.export_conversation(sid, ExportFormat.JSON))

            # Manifest
            manifest = {
                "exported_at": time.time(),
                "format_version": "1.0",
                "contents": ["memories", "traces", "knowledge_graph", "conversations"],
                "session_count": len(sessions),
            }
            zf.writestr(f"omni_export_{ts}/manifest.json",
                        json.dumps(manifest, indent=2))

        return buf.getvalue()

    def _list_sessions(self) -> List[str]:
        if not self.agent:
            return []
        try:
            return self.agent.memory.list_sessions()
        except Exception:
            return []

    # ── REST API ──────────────────────────────────────────────────────────────

    def register_routes(self, app, prefix: str = ""):
        from aiohttp import web
        from agent.auth import auth_context_from_request, scoped_session_id, visible_session_ids

        def _forbidden(detail: str) -> web.Response:
            return web.json_response({"error": "forbidden", "detail": detail}, status=403)

        async def export_conv(request):
            ctx = auth_context_from_request(request)
            try:
                session_id = scoped_session_id(
                    ctx,
                    requested_session_id=request.match_info.get("session_id", ""),
                    default_session_id="conversation",
                )
            except PermissionError as exc:
                return _forbidden(str(exc))
            fmt_str = request.rel_url.query.get("format", "markdown")
            try:
                fmt = ExportFormat(fmt_str)
            except ValueError:
                fmt = ExportFormat.MARKDOWN
            content = self.export_conversation(session_id, fmt)
            ct = _content_type(fmt)
            return web.Response(text=content, content_type=ct)

        async def export_memories_ep(request):
            fmt_str = request.rel_url.query.get("format", "json")
            try:
                fmt = ExportFormat(fmt_str)
            except ValueError:
                fmt = ExportFormat.JSON
            content = self.export_memories(fmt)
            return web.Response(text=content, content_type=_content_type(fmt))

        async def export_traces_ep(request):
            fmt_str = request.rel_url.query.get("format", "json")
            last_n = int(request.rel_url.query.get("last_n", "500"))
            try:
                fmt = ExportFormat(fmt_str)
            except ValueError:
                fmt = ExportFormat.JSON
            content = self.export_traces(fmt, last_n=last_n)
            return web.Response(text=content, content_type=_content_type(fmt))

        async def export_kg_ep(request):
            fmt_str = request.rel_url.query.get("format", "json")
            try:
                fmt = ExportFormat(fmt_str)
            except ValueError:
                fmt = ExportFormat.JSON
            content = self.export_kg(fmt)
            return web.Response(text=content, content_type=_content_type(fmt))

        async def export_dump(request):
            ctx = auth_context_from_request(request)
            data = await request.json() if request.content_length else {}
            raw_sessions = data.get("sessions")
            try:
                if raw_sessions is None:
                    sessions = visible_session_ids(ctx, self._list_sessions())
                else:
                    requested_sessions = raw_sessions if isinstance(raw_sessions, list) else [str(raw_sessions)]
                    sessions = list(dict.fromkeys(
                        scoped_session_id(
                            ctx,
                            requested_session_id=str(session_id),
                            default_session_id="conversation",
                        )
                        for session_id in requested_sessions
                    ))
            except PermissionError as exc:
                return _forbidden(str(exc))
            archive = self.full_dump(session_ids=sessions)
            ts = time.strftime("%Y%m%d_%H%M%S")
            return web.Response(
                body=archive,
                content_type="application/zip",
                headers={"Content-Disposition":
                         f'attachment; filename="omni_export_{ts}.zip"'},
            )

        app.router.add_get(f"{prefix}/export/conversation/{{session_id}}", export_conv)
        app.router.add_get(f"{prefix}/export/memories", export_memories_ep)
        app.router.add_get(f"{prefix}/export/traces", export_traces_ep)
        app.router.add_get(f"{prefix}/export/kg", export_kg_ep)
        app.router.add_post(f"{prefix}/export/dump", export_dump)

        logger.info(f"Export routes registered at {prefix}/export/*")


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())


def _fmt_time(ts) -> str:
    if not ts:
        return ""
    try:
        return time.strftime("%H:%M:%S", time.gmtime(float(ts)))
    except Exception:
        return str(ts)


def _content_type(fmt: ExportFormat) -> str:
    return {
        ExportFormat.JSON:     "application/json",
        ExportFormat.MARKDOWN: "text/markdown",
        ExportFormat.CSV:      "text/csv",
        ExportFormat.HTML:     "text/html",
        ExportFormat.DOT:      "text/plain",
        ExportFormat.CYPHER:   "text/plain",
    }.get(fmt, "text/plain")


def _html_wrap(title: str, body: str, css: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
           margin: 0; padding: 20px; background: #f5f5f5; color: #333; }}
    h1, h2 {{ color: #2c3e50; }}
    {css}
  </style>
</head>
<body>
  <h1>{title}</h1>
  {body}
  <footer style="margin-top:40px;color:#999;font-size:12px">
    Generated by OMNI Agent — {_ts()}
  </footer>
</body>
</html>"""


_CONV_CSS = """
  .conversation { max-width: 800px; margin: 0 auto; }
  .message { margin: 12px 0; padding: 12px 16px; border-radius: 8px; }
  .user      { background: #e8f4fd; border-left: 4px solid #3498db; }
  .assistant { background: #eafaf1; border-left: 4px solid #27ae60; }
  .system    { background: #fef9e7; border-left: 4px solid #f39c12; }
  .role { font-weight: bold; font-size: 12px; color: #666; margin-bottom: 6px; }
  .content { white-space: pre-wrap; line-height: 1.5; }
  .ts { font-weight: normal; color: #aaa; }
"""

_TIMELINE_CSS = """
  .timeline { padding: 10px; }
  .span-row { display: flex; align-items: center; margin: 4px 0; height: 28px; }
  .span-label { width: 200px; font-size: 12px; color: #555; overflow: hidden;
                text-overflow: ellipsis; white-space: nowrap; padding-right: 8px; }
  .span-track { flex: 1; position: relative; height: 20px; background: #f0f0f0;
                border-radius: 3px; overflow: hidden; }
  .span-bar   { position: absolute; height: 100%; border-radius: 3px; opacity: 0.85; }
  .span-dur   { width: 70px; text-align: right; font-size: 11px; color: #888; padding-left: 6px; }
"""

_TABLE_CSS = """
  table { border-collapse: collapse; width: 100%; margin: 16px 0; }
  th { background: #2c3e50; color: white; padding: 8px 12px; text-align: left; }
  td { padding: 6px 12px; border-bottom: 1px solid #ddd; }
  tr:nth-child(even) { background: #f9f9f9; }
  tr:hover { background: #eaf4ff; }
"""
