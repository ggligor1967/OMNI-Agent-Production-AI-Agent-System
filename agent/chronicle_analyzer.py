"""
OMNI AGENT - Chronicle Analyzer
Analyzes session history and usage patterns to provide personalized tips.

Features:
- Session pattern analysis: length, frequency, time distribution
- Task type classification based on query content
- Model preference detection
- Domain/topic extraction
- Actionable tip generation with confidence scores
"""
import re
import time
import json
import logging
from typing import Dict, List, Tuple, Optional, Set
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class SessionStats:
    """Statistics for a single session."""
    session_id: str
    message_count: int
    user_turns: int
    total_length: int
    avg_message_length: int
    created_at: float
    updated_at: float
    task_types: Dict[str, int]
    domains: List[str]


@dataclass
class UsagePattern:
    """A detected usage pattern with confidence."""
    pattern: str
    confidence: float
    frequency: int
    evidence: str


class ChronicleAnalyzer:
    """Analyzes session history and generates personalized tips."""

    # Task type keywords for classification
    TASK_KEYWORDS = {
        "code": [
            "python", "javascript", "java", "function", "class", "import", "def ",
            "let ", "const ", "var ", "write code", "implement", "coding",
            "algorithm", "function", "method", "api", "framework", "library",
            "debug", "error", "exception", "test", "unit test"
        ],
        "math": [
            "equation", "integral", "derivative", "solve", "calculate", "formula",
            "algebra", "calculus", "geometry", "statistics", "probability",
            "∫", "∑", "∂", "∇", "π", "mathematical", "matrix", "vector"
        ],
        "translation": [
            "translate", "from english to", "from french to", "from spanish to",
            "how do you say", "language", "convert", "interpret", "pronunciation"
        ],
        "creative": [
            "write", "compose", "create", "poem", "story", "song", "haiku",
            "fiction", "novel", "script", "dialogue", "character", "plot",
            "rhyme", "verse", "stanza", "creative writing"
        ],
        "reasoning": [
            "why", "because", "analyze", "compare", "pros and cons", "advantages",
            "disadvantages", "explain", "interpret", "justify", "debate",
            "versus", "vs", "better than", "worse than"
        ],
        "vision": [
            "image", "photo", "screenshot", "describe this", "ocr", "visual",
            "detect", "object detection", "read from image", "analyze image"
        ],
        "search": [
            "search", "find", "look up", "research", "web", "information",
            "news", "latest", "current", "trending", "upcoming"
        ]
    }

    # Domain keywords
    DOMAIN_KEYWORDS = {
        "web_dev": [
            "react", "vue", "angular", "nodejs", "express", "html", "css",
            "javascript", "typescript", "frontend", "backend", "web"
        ],
        "data_science": [
            "pandas", "numpy", "machine learning", "ml", "model", "dataset",
            "training", "prediction", "data analysis", "statistics", "sklearn"
        ],
        "devops": [
            "docker", "kubernetes", "cicd", "github", "gitlab", "jenkins",
            "deployment", "infrastructure", "cloud", "aws", "gcp", "azure"
        ],
        "database": [
            "sql", "database", "query", "schema", "table", "postgres", "mysql",
            "mongodb", "redis", "orm", "migration"
        ],
        "security": [
            "security", "encryption", "hash", "auth", "oauth", "jwt", "ssl",
            "vulnerability", "penetration", "firewall", "protection"
        ],
        "mobile": [
            "ios", "android", "react native", "flutter", "mobile app",
            "swift", "kotlin", "app development"
        ]
    }

    def __init__(self, memory_db):
        """Initialize analyzer with access to memory database."""
        self.memory = memory_db

    def analyze_all_sessions(self) -> Dict:
        """Analyze all sessions and generate personalized tips."""
        session_ids = self.memory.list_sessions()

        if not session_ids:
            return {
                "tips": ["Start a conversation to get personalized tips!"],
                "stats": {"total_sessions": 0},
                "patterns": []
            }

        # Collect statistics from all sessions
        all_stats: List[SessionStats] = []
        for sid in session_ids:
            stats = self._analyze_session(sid)
            if stats:
                all_stats.append(stats)

        if not all_stats:
            return {
                "tips": ["No conversation history found. Start chatting to unlock personalized tips!"],
                "stats": {"total_sessions": len(session_ids)},
                "patterns": []
            }

        # Generate insights and tips
        tips = self._generate_tips(all_stats)
        patterns = self._detect_patterns(all_stats)
        stats = self._compute_aggregate_stats(all_stats)

        return {
            "tips": tips,
            "patterns": patterns,
            "stats": stats
        }

    def _analyze_session(self, session_id: str) -> Optional[SessionStats]:
        """Analyze a single session."""
        history = self.memory.get_history(session_id, limit=1000)

        if not history:
            return None

        user_messages = [m for m in history if m.get("role") == "user"]
        if not user_messages:
            return None

        # Extract metadata
        created_at = min((m.get("ts", time.time()) for m in history), default=time.time())
        updated_at = max((m.get("ts", time.time()) for m in history), default=time.time())

        # Calculate statistics
        message_count = len(history)
        user_turns = len(user_messages)
        total_length = sum(len(m.get("content", "")) for m in history)
        avg_message_length = total_length // max(1, message_count)

        # Classify task types
        task_types = self._classify_messages(user_messages)

        # Extract domains
        domains = self._extract_domains(user_messages)

        return SessionStats(
            session_id=session_id,
            message_count=message_count,
            user_turns=user_turns,
            total_length=total_length,
            avg_message_length=avg_message_length,
            created_at=created_at,
            updated_at=updated_at,
            task_types=task_types,
            domains=domains
        )

    def _classify_messages(self, messages: List[Dict]) -> Dict[str, int]:
        """Classify messages by task type."""
        task_counts: Dict[str, int] = defaultdict(int)
        text = " ".join(m.get("content", "") for m in messages).lower()

        for task_type, keywords in self.TASK_KEYWORDS.items():
            for keyword in keywords:
                if keyword in text:
                    task_counts[task_type] += text.count(keyword)

        # Normalize: find the most likely task type
        if task_counts:
            total = sum(task_counts.values())
            task_counts = {k: round(v / total * 100) for k, v in task_counts.items()}

        return dict(task_counts)

    def _extract_domains(self, messages: List[Dict]) -> List[str]:
        """Extract application domains from messages."""
        text = " ".join(m.get("content", "") for m in messages).lower()
        domains: Set[str] = set()

        for domain, keywords in self.DOMAIN_KEYWORDS.items():
            for keyword in keywords:
                if keyword in text:
                    domains.add(domain)
                    break

        return sorted(list(domains))

    def _generate_tips(self, all_stats: List[SessionStats]) -> List[str]:
        """Generate personalized tips based on usage patterns."""
        tips: List[str] = []

        # Analyze task type preferences
        task_counter: Dict[str, int] = defaultdict(int)
        for stats in all_stats:
            for task_type, count in stats.task_types.items():
                task_counter[task_type] += count

        if task_counter:
            top_task = max(task_counter.items(), key=lambda x: x[1])
            if top_task[0] == "code":
                tips.append(
                    "💡 You write a lot of code! Try using /route to preview which model "
                    "will handle your coding tasks best."
                )
                tips.append(
                    "🚀 Pro tip: Use /compare to run your code prompts on multiple models "
                    "and see which produces better results."
                )
            elif top_task[0] == "math":
                tips.append(
                    "📐 Math is your specialty! Consider pinning to a mathematical reasoning model "
                    "with /model for faster responses."
                )
            elif top_task[0] == "reasoning":
                tips.append(
                    "🧠 You enjoy analytical tasks. Use /template to save your favorite analysis "
                    "prompts for quick reuse."
                )
            elif top_task[0] == "creative":
                tips.append(
                    "✨ You're creative! Experiment with /compare across models to find which one "
                    "matches your creative style best."
                )

        # Analyze session patterns
        total_sessions = len(all_stats)
        total_turns = sum(s.user_turns for s in all_stats)
        avg_turns = total_turns / max(1, total_sessions)

        if avg_turns > 20:
            tips.append(
                "📚 You have long, detailed conversations. Try /summarize to compress history "
                "when context gets full."
            )
        elif avg_turns < 3:
            tips.append(
                "⚡ You prefer quick interactions. Consider using /pipelines to batch multiple "
                "related tasks."
            )

        # Domain-based tips
        all_domains: Dict[str, int] = defaultdict(int)
        for stats in all_stats:
            for domain in stats.domains:
                all_domains[domain] += 1

        if all_domains:
            top_domain = max(all_domains.items(), key=lambda x: x[1])[0]
            if top_domain == "web_dev":
                tips.append(
                    "🌐 Web development is a focus area. Use /load to ingest framework documentation "
                    "for context-aware help."
                )
            elif top_domain == "data_science":
                tips.append(
                    "📊 Data science projects detected. Try /rag for retrieval-augmented queries "
                    "over your datasets."
                )
            elif top_domain == "devops":
                tips.append(
                    "⚙️ Infrastructure and deployment are your focus. Create saved /templates for "
                    "common DevOps tasks."
                )

        # Session timing analysis
        times_of_day: Dict[str, int] = defaultdict(int)
        for stats in all_stats:
            hour = datetime.fromtimestamp(stats.created_at).hour
            period = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
            times_of_day[period] += 1

        if times_of_day:
            peak_period = max(times_of_day.items(), key=lambda x: x[1])[0]
            if peak_period == "evening":
                tips.append(
                    "🌙 You're most active in the evening. Save useful responses with /use template "
                    "to speed up future sessions."
                )

        # Model usage guidance
        if avg_turns > 10:
            tips.append(
                "🎯 With your conversation depth, model choice matters. Use /route to understand "
                "task routing before starting complex queries."
            )

        # Diversify tips - don't repeat patterns
        if not tips:
            tips.append(
                "🎓 Keep exploring! Try /help to discover all available commands and features."
            )

        return tips[:5]  # Return top 5 tips

    def _detect_patterns(self, all_stats: List[SessionStats]) -> List[Dict]:
        """Detect patterns in usage."""
        patterns: List[Dict] = []

        # Pattern 1: Session length distribution
        short_sessions = sum(1 for s in all_stats if s.user_turns <= 3)
        medium_sessions = sum(1 for s in all_stats if 3 < s.user_turns <= 10)
        long_sessions = sum(1 for s in all_stats if s.user_turns > 10)
        total = len(all_stats)

        if total > 0:
            patterns.append({
                "name": "Session Length Distribution",
                "short": round(short_sessions / total * 100),
                "medium": round(medium_sessions / total * 100),
                "long": round(long_sessions / total * 100),
                "confidence": 0.95
            })

        # Pattern 2: Task type distribution
        task_counter: Dict[str, int] = defaultdict(int)
        for stats in all_stats:
            for task_type, count in stats.task_types.items():
                task_counter[task_type] += count

        if task_counter:
            top_3_tasks = sorted(task_counter.items(), key=lambda x: x[1], reverse=True)[:3]
            patterns.append({
                "name": "Primary Task Types",
                "tasks": [{"type": t[0], "frequency": t[1]} for t in top_3_tasks],
                "confidence": 0.9
            })

        # Pattern 3: Domain specialization
        domain_counter: Dict[str, int] = defaultdict(int)
        for stats in all_stats:
            for domain in stats.domains:
                domain_counter[domain] += 1

        if domain_counter:
            patterns.append({
                "name": "Specialized Domains",
                "domains": sorted(list(domain_counter.keys())),
                "confidence": 0.85
            })

        return patterns

    def _compute_aggregate_stats(self, all_stats: List[SessionStats]) -> Dict:
        """Compute aggregate statistics across all sessions."""
        total_sessions = len(all_stats)
        total_messages = sum(s.message_count for s in all_stats)
        total_turns = sum(s.user_turns for s in all_stats)
        total_chars = sum(s.total_length for s in all_stats)

        return {
            "total_sessions": total_sessions,
            "total_messages": total_messages,
            "total_turns": total_turns,
            "total_characters": total_chars,
            "avg_session_length": round(total_messages / max(1, total_sessions)),
            "avg_turns_per_session": round(total_turns / max(1, total_sessions)),
            "avg_message_length": round(total_chars / max(1, total_messages)) if total_messages else 0
        }
