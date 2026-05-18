"""
OMNI AGENT - Prompt Template Engine
Named, versioned prompt templates with variable substitution,
few-shot example management, and DB persistence.
"""
import re
import json
import time
import hashlib
import logging
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
from agent.memory import MemoryDB

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# TEMPLATE MODEL
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class PromptTemplate:
    """
    A named prompt template with typed variables.

    Variables are declared as {{variable_name}} or {{variable_name:default_value}}.
    Few-shot examples are injected before the final user message.
    """
    name: str
    template: str
    description: str = ""
    version: str = "1.0.0"
    tags: List[str] = field(default_factory=list)
    few_shot_examples: List[Dict[str, str]] = field(default_factory=list)
    system_prompt: str = ""
    model_hint: str = ""        # suggested model for this template
    author: str = "system"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    use_count: int = 0

    # ── Variable Parsing ──────────────────────────────────────────────────────

    def get_variables(self) -> Dict[str, Optional[str]]:
        """
        Extract all {{var}} or {{var:default}} declarations.
        Returns {var_name: default_value_or_None}.
        """
        pattern = r"\{\{(\w+)(?::([^}]*))?\}\}"
        return {
            m.group(1): m.group(2)
            for m in re.finditer(pattern, self.template)
        }

    def validate(self, variables: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """
        Check all required variables (no default) are provided.
        Returns (is_valid, list_of_missing_vars).
        """
        declared = self.get_variables()
        missing = [
            name for name, default in declared.items()
            if default is None and name not in variables
        ]
        return len(missing) == 0, missing

    # ── Rendering ─────────────────────────────────────────────────────────────

    def render(self, variables: Dict[str, Any] = None) -> str:
        """
        Substitute variables into the template.
        Raises ValueError for missing required variables.
        """
        variables = variables or {}
        valid, missing = self.validate(variables)
        if not valid:
            raise ValueError(
                f"Template '{self.name}' missing required variables: {missing}"
            )

        result = self.template
        declared = self.get_variables()

        for name, default in declared.items():
            value = str(variables.get(name, default or ""))
            result = re.sub(
                r"\{\{" + re.escape(name) + r"(?::[^}]*)?\}\}",
                value.replace("\\", "\\\\"),
                result
            )
        return result

    def build_messages(self, variables: Dict[str, Any] = None,
                       history: List[Dict] = None) -> List[Dict[str, str]]:
        """
        Build a full message list: system + few-shot + history + rendered user.
        """
        messages: List[Dict[str, str]] = []

        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})

        # Few-shot examples
        for ex in self.few_shot_examples:
            if "user" in ex:
                messages.append({"role": "user", "content": ex["user"]})
            if "assistant" in ex:
                messages.append({"role": "assistant", "content": ex["assistant"]})

        # Conversation history
        if history:
            messages.extend(history)

        # Final rendered user turn
        rendered = self.render(variables)
        messages.append({"role": "user", "content": rendered})

        return messages

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "tags": self.tags,
            "few_shot_count": len(self.few_shot_examples),
            "system_prompt": bool(self.system_prompt),
            "model_hint": self.model_hint,
            "variables": list(self.get_variables().keys()),
            "author": self.author,
            "use_count": self.use_count,
            "created_at": self.created_at,
        }


# ══════════════════════════════════════════════════════════════════════════════
# TEMPLATE REGISTRY
# ══════════════════════════════════════════════════════════════════════════════

class PromptTemplateRegistry:
    """
    Manages named prompt templates with memory-DB persistence.
    Built-in templates cover common agent tasks.
    """

    def __init__(self, memory: MemoryDB = None):
        self.memory = memory
        self._templates: Dict[str, PromptTemplate] = {}
        self._load_builtins()
        if memory:
            self._load_from_db()

    # ── Built-in Templates ────────────────────────────────────────────────────

    def _load_builtins(self):
        builtins = [
            PromptTemplate(
                name="summarize",
                description="Summarize any text to a target length",
                template=(
                    "Summarize the following text in {{style:bullet points}}.\n"
                    "Target length: {{length:3-5 sentences}}.\n\n"
                    "TEXT:\n{{text}}"
                ),
                system_prompt=(
                    "You are a precise summarization assistant. "
                    "Extract key information and present it clearly."
                ),
                tags=["summarization", "writing"],
                few_shot_examples=[
                    {
                        "user": "Summarize in bullet points. Target length: 2-3 sentences.\n\nTEXT:\nPython is a programming language...",
                        "assistant": "• Python is a high-level, interpreted programming language.\n• Known for readability and rapid development.\n• Used in web, AI, data science, and automation."
                    }
                ],
            ),
            PromptTemplate(
                name="code_review",
                description="Review code for bugs, style, and improvements",
                template=(
                    "Review the following {{language:Python}} code.\n"
                    "Focus on: {{focus:bugs, performance, readability}}.\n\n"
                    "```{{language:Python}}\n{{code}}\n```\n\n"
                    "Provide specific, actionable feedback."
                ),
                system_prompt=(
                    "You are a senior software engineer conducting thorough code reviews. "
                    "Be specific, constructive, and prioritize critical issues."
                ),
                model_hint="devstral-2:123b-cloud",
                tags=["code", "review", "engineering"],
            ),
            PromptTemplate(
                name="explain_concept",
                description="Explain a concept at a specific level",
                template=(
                    "Explain {{concept}} for a {{audience:software developer}} audience.\n"
                    "Use {{style:analogies and concrete examples}}.\n"
                    "Keep the explanation {{length:concise (under 200 words)}}."
                ),
                system_prompt=(
                    "You are an expert teacher who adapts explanations to the audience. "
                    "Prioritize clarity over completeness."
                ),
                tags=["education", "explanation"],
            ),
            PromptTemplate(
                name="translate",
                description="Translate text preserving tone and nuance",
                template=(
                    "Translate the following text from {{source_lang:English}} "
                    "to {{target_lang}}.\n"
                    "Preserve the {{tone:original tone and style}}.\n\n"
                    "TEXT:\n{{text}}"
                ),
                model_hint="mistral-large-3:675b-cloud",
                tags=["translation", "multilingual"],
            ),
            PromptTemplate(
                name="write_tests",
                description="Generate unit tests for a function or class",
                template=(
                    "Write comprehensive {{framework:pytest}} tests for the following "
                    "{{language:Python}} code.\n"
                    "Include: {{coverage:happy path, edge cases, and error cases}}.\n\n"
                    "```{{language:Python}}\n{{code}}\n```"
                ),
                system_prompt=(
                    "You are a test-driven development expert. "
                    "Write thorough, readable tests that serve as documentation."
                ),
                model_hint="qwen3-coder-next:cloud",
                tags=["code", "testing", "engineering"],
            ),
            PromptTemplate(
                name="debug",
                description="Diagnose and fix a code error",
                template=(
                    "Debug the following {{language:Python}} error.\n\n"
                    "ERROR:\n```\n{{error}}\n```\n\n"
                    "CODE:\n```{{language:Python}}\n{{code}}\n```\n\n"
                    "Explain the root cause and provide a corrected version."
                ),
                model_hint="devstral-2:123b-cloud",
                tags=["code", "debugging"],
            ),
            PromptTemplate(
                name="rag_answer",
                description="Answer a question using retrieved context (RAG)",
                template=(
                    "Using ONLY the provided context, answer the question.\n"
                    "If the context doesn't contain enough information, say so.\n\n"
                    "CONTEXT:\n{{context}}\n\n"
                    "QUESTION:\n{{question}}"
                ),
                system_prompt=(
                    "You are a precise assistant that answers strictly from provided context. "
                    "Never add information not found in the context."
                ),
                tags=["rag", "qa", "retrieval"],
            ),
            PromptTemplate(
                name="chain_of_thought",
                description="Solve a problem with explicit step-by-step reasoning",
                template=(
                    "Solve the following problem step by step.\n\n"
                    "PROBLEM:\n{{problem}}\n\n"
                    "Think carefully through each step before reaching a conclusion."
                ),
                system_prompt=(
                    "You are a meticulous analytical thinker. "
                    "Show ALL your reasoning steps explicitly. "
                    "Label each step clearly."
                ),
                model_hint="cogito-2.1:671b-cloud",
                tags=["reasoning", "math", "analysis"],
                few_shot_examples=[
                    {
                        "user": "Solve step by step.\n\nPROBLEM:\nIf a train travels 60 mph for 2.5 hours, how far does it travel?",
                        "assistant": "**Step 1:** Identify the formula.\nDistance = Speed × Time\n\n**Step 2:** Substitute values.\nDistance = 60 mph × 2.5 hours\n\n**Step 3:** Calculate.\nDistance = 150 miles\n\n**Answer:** The train travels **150 miles**."
                    }
                ],
            ),
            PromptTemplate(
                name="compare_models",
                description="Compare outputs from multiple models",
                template=(
                    "You have received responses from multiple AI models on the same question.\n\n"
                    "QUESTION: {{question}}\n\n"
                    "RESPONSES:\n{{responses}}\n\n"
                    "Compare the responses and identify:\n"
                    "1. Which is most accurate?\n"
                    "2. Which is most comprehensive?\n"
                    "3. Key differences and unique insights from each."
                ),
                tags=["meta", "comparison", "evaluation"],
            ),
            PromptTemplate(
                name="agent_plan",
                description="Generate a step-by-step plan for an agentic task",
                template=(
                    "Create a detailed execution plan for the following task.\n\n"
                    "TASK: {{task}}\n\n"
                    "Available tools: {{tools:web_search, code_execution, memory, file_operations}}\n"
                    "Constraints: {{constraints:none}}\n\n"
                    "Output a numbered plan with:\n"
                    "- Each step clearly described\n"
                    "- Which tool to use\n"
                    "- Expected output\n"
                    "- Potential failure modes"
                ),
                model_hint="devstral-2:123b-cloud",
                tags=["agents", "planning"],
            ),
        ]

        for t in builtins:
            self._templates[t.name] = t
        logger.info(f"Loaded {len(builtins)} built-in templates")

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def register(self, template: PromptTemplate) -> PromptTemplate:
        self._templates[template.name] = template
        if self.memory:
            self._save_to_db(template)
        logger.info(f"Template registered: '{template.name}' v{template.version}")
        return template

    def get(self, name: str) -> Optional[PromptTemplate]:
        return self._templates.get(name)

    def delete(self, name: str) -> bool:
        if name in self._templates:
            del self._templates[name]
            if self.memory:
                self.memory.save_memory(
                    f"template_deleted:{name}", True, category="templates"
                )
            return True
        return False

    def list_templates(self, tag: Optional[str] = None) -> List[Dict]:
        templates = list(self._templates.values())
        if tag:
            templates = [t for t in templates if tag in t.tags]
        return [t.to_dict() for t in sorted(templates, key=lambda t: t.name)]

    def search(self, query: str) -> List[PromptTemplate]:
        q = query.lower()
        return [
            t for t in self._templates.values()
            if q in t.name.lower()
            or q in t.description.lower()
            or any(q in tag for tag in t.tags)
        ]

    # ── Rendering ─────────────────────────────────────────────────────────────

    def render(self, name: str, variables: Dict[str, Any] = None,
               history: List[Dict] = None) -> List[Dict[str, str]]:
        """Get a template by name and render it into a messages list."""
        tmpl = self.get(name)
        if not tmpl:
            raise KeyError(f"Template '{name}' not found. "
                          f"Available: {list(self._templates.keys())}")
        tmpl.use_count += 1
        return tmpl.build_messages(variables, history)

    def quick_render(self, name: str, **kwargs) -> List[Dict[str, str]]:
        """Shorthand: registry.quick_render('summarize', text='...', style='...')"""
        return self.render(name, variables=kwargs)

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save_to_db(self, template: PromptTemplate):
        if not self.memory:
            return
        self.memory.save_memory(
            f"template:{template.name}",
            json.dumps({
                "name": template.name,
                "template": template.template,
                "description": template.description,
                "version": template.version,
                "tags": template.tags,
                "few_shot_examples": template.few_shot_examples,
                "system_prompt": template.system_prompt,
                "model_hint": template.model_hint,
                "author": template.author,
            }),
            category="templates",
            importance=6,
        )

    def _load_from_db(self):
        if not self.memory:
            return
        memories = self.memory.get_memories_by_category("templates")
        for m in memories:
            if m["key"].startswith("template:"):
                try:
                    data = json.loads(m["value"])
                    tmpl = PromptTemplate(**data)
                    self._templates[tmpl.name] = tmpl
                except Exception as e:
                    logger.warning(f"Failed to load template from DB: {e}")

    def export_json(self, path: str):
        """Export all templates to a JSON file."""
        data = {
            name: {
                "template": t.template,
                "description": t.description,
                "version": t.version,
                "tags": t.tags,
                "few_shot_examples": t.few_shot_examples,
                "system_prompt": t.system_prompt,
                "model_hint": t.model_hint,
            }
            for name, t in self._templates.items()
        }
        Path(path).write_text(json.dumps(data, indent=2))
        logger.info(f"Exported {len(data)} templates to {path}")

    def import_json(self, path: str) -> int:
        """Import templates from a JSON file. Returns count imported."""
        data = json.loads(Path(path).read_text())
        count = 0
        for name, spec in data.items():
            tmpl = PromptTemplate(name=name, **spec)
            self.register(tmpl)
            count += 1
        return count
