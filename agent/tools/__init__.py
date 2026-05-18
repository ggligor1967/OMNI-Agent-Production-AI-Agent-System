"""
OMNI AGENT - Tool Suite
Web search, scraping, code execution, semantic analysis, security, documentation.
"""
import re
import ast
import sys
import time
import json
import logging
import hashlib
import asyncio
import shlex
import textwrap
import subprocess
import tempfile
import os
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import aiohttp
from bs4 import BeautifulSoup
from config import CONFIG

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# WEB SCRAPER & SEARCH
# ══════════════════════════════════════════════════════════════════════════════

class WebScraper:
    """Async web scraper with retry logic and content extraction."""

    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(total=CONFIG.SCRAPER_TIMEOUT)
        self.headers = {
            "User-Agent": "OmniAgent/1.0 (+https://github.com/your-org/omni-agent)"
        }
        self.last_search_backend = "uninitialized"
        self.last_search_error = ""
        self._searxng_available = bool(CONFIG.SEARXNG_URL)

    async def fetch(self, url: str) -> Dict[str, Any]:
        """Fetch a URL and return structured content."""
        for attempt in range(CONFIG.SCRAPER_MAX_RETRIES):
            try:
                async with aiohttp.ClientSession(headers=self.headers) as session:
                    async with session.get(url, timeout=self.timeout) as resp:
                        html = await resp.text(errors="replace")
                        return self._parse(html, url, resp.status)
            except Exception as e:
                if attempt == CONFIG.SCRAPER_MAX_RETRIES - 1:
                    return {"error": str(e), "url": url}
                await asyncio.sleep(2 ** attempt)

    def _parse(self, html: str, url: str, status: int) -> Dict:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        title = soup.title.string.strip() if soup.title else ""
        meta_desc = ""
        meta = soup.find("meta", attrs={"name": "description"})
        if meta:
            meta_desc = meta.get("content", "")

        paragraphs = [p.get_text(separator=" ", strip=True)
                     for p in soup.find_all("p") if len(p.get_text(strip=True)) > 40]
        body_text = "\n".join(paragraphs[:30])  # first 30 substantial paragraphs

        links = [
            {"href": a.get("href", ""), "text": a.get_text(strip=True)}
            for a in soup.find_all("a", href=True)
            if a.get_text(strip=True)
        ][:20]

        return {
            "url": url, "status": status, "title": title,
            "description": meta_desc, "body": body_text,
            "links": links, "word_count": len(body_text.split())
        }

    async def search(self, query: str, num_results: int = 5) -> List[Dict]:
        """Search via SearXNG (self-hostable) or DuckDuckGo HTML fallback."""
        if self._searxng_available and CONFIG.SEARXNG_URL:
            try:
                results = await self._search_searxng(query, num_results)
                self.last_search_backend = "searxng"
                self.last_search_error = ""
                return results
            except Exception as exc:
                self._searxng_available = False
                self.last_search_error = str(exc)
                logger.warning(
                    "SearXNG unavailable, falling back to DuckDuckGo HTML: %s",
                    exc,
                )
        return await self._search_ddg(query, num_results)

    async def _search_searxng(self, query: str, n: int) -> List[Dict]:
        params = {"q": query, "format": "json", "engines": "google,bing,duckduckgo"}
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{CONFIG.SEARXNG_URL}/search",
                                   params=params,
                                   timeout=self.timeout) as resp:
                data = await resp.json()
        return [
            {"title": r.get("title",""), "url": r.get("url",""),
             "snippet": r.get("content","")}
            for r in data.get("results", [])[:n]
        ]

    async def _search_ddg(self, query: str, n: int) -> List[Dict]:
        self.last_search_backend = "duckduckgo_html"
        url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
        async with aiohttp.ClientSession(headers=self.headers) as session:
            async with session.get(url, timeout=self.timeout) as resp:
                html = await resp.text(errors="replace")

        soup = BeautifulSoup(html, "html.parser")
        results: List[Dict[str, str]] = []
        for node in soup.select(".result"):
            link = node.select_one("a.result__a, .result__title a")
            if not link:
                continue

            title = link.get_text(" ", strip=True)
            href = link.get("href", "")
            snippet_node = node.select_one(".result__snippet")
            snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""

            if title and href:
                results.append({"title": title, "url": href, "snippet": snippet})
            if len(results) >= n:
                break

        if results:
            return results

        result = self._parse(html, url, 200)
        links = result.get("links", [])[:n]
        return [{"title": l["text"], "url": l["href"], "snippet": ""} for l in links]


# ══════════════════════════════════════════════════════════════════════════════
# CODE EXECUTOR
# ══════════════════════════════════════════════════════════════════════════════

class CodeExecutor:
    """Sandboxed Python code execution with resource limits."""

    BLOCKED_IMPORTS = {"os", "subprocess", "sys", "shutil", "socket",
                       "ctypes", "importlib", "__builtins__"}
    SHELL_CONTROL_TOKENS = {"&&", "||", "|", ";", "&", ">", ">>", "<"}

    def execute_python(self, code: str, timeout: int = 10,
                       safe_mode: bool = True) -> Dict[str, Any]:
        """Execute Python in an isolated subprocess (safe_mode blocks dangerous imports)."""
        if safe_mode:
            violation = self._check_safety(code)
            if violation:
                return {"error": f"Security violation: {violation}", "output": "", "success": False}
        result = {"output": "", "error": "", "return_value": None, "success": False}
        wrapper = textwrap.dedent("""
            import json
            import sys
            from pathlib import Path

            RESULT_MARKER = "__OMNI_EXEC_RESULT__"

            source_path = Path(sys.argv[1])
            code = source_path.read_text(encoding="utf-8")
            local_ns = {}
            payload = {"return_value": None, "error": "", "success": False}

            try:
                exec(compile(code, str(source_path), "exec"), {"__builtins__": __builtins__}, local_ns)
                payload["return_value"] = local_ns.get("result", local_ns.get("output"))
                payload["success"] = True
            except Exception as exc:
                payload["error"] = f"{type(exc).__name__}: {exc}"

            print(RESULT_MARKER + json.dumps(payload, default=str))
        """)
        source_path = None

        try:
            with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as temp_source:
                temp_source.write(code)
                source_path = temp_source.name

            proc = subprocess.run(
                [sys.executable, "-I", "-c", wrapper, source_path],
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            marker = "__OMNI_EXEC_RESULT__"
            stdout = proc.stdout or ""
            if marker in stdout:
                output, _, payload_json = stdout.partition(marker)
                payload = json.loads(payload_json.strip() or "{}")
                result["output"] = output
                result["error"] = payload.get("error", "") or (proc.stderr or "")
                result["return_value"] = payload.get("return_value")
                result["success"] = bool(payload.get("success"))
            else:
                result["output"] = stdout
                result["error"] = proc.stderr or "Execution result marker missing"
        except subprocess.TimeoutExpired:
            result["error"] = "Timeout"
        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"
        finally:
            if source_path and os.path.exists(source_path):
                try:
                    os.unlink(source_path)
                except OSError:
                    pass

        return result

    def execute_shell(self, command: str, timeout: int = 15) -> Dict[str, Any]:
        """Execute a single process without invoking a system shell."""
        if not isinstance(command, str) or not command.strip():
            return {"error": "Command must be a non-empty string", "success": False}
        if "\n" in command or "\r" in command:
            return {"error": "Multiline commands are not allowed", "success": False}

        try:
            argv = shlex.split(command.strip())
            if not argv:
                return {"error": "Command must include an executable", "success": False}
            if any(token in self.SHELL_CONTROL_TOKENS for token in argv):
                return {
                    "error": "Shell control operators are not allowed",
                    "success": False,
                }

            proc = subprocess.run(
                argv, shell=False, capture_output=True,
                text=True, timeout=timeout
            )
            return {
                "stdout": proc.stdout, "stderr": proc.stderr,
                "returncode": proc.returncode, "success": proc.returncode == 0
            }
        except ValueError as e:
            return {"error": f"Invalid command: {e}", "success": False}
        except subprocess.TimeoutExpired:
            return {"error": "Timeout", "success": False}
        except Exception as e:
            return {"error": str(e), "success": False}

    def _check_safety(self, code: str) -> Optional[str]:
        try:
            tree = ast.parse(code)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [alias.name.split(".")[0] for alias in node.names]
                    for name in names:
                        if name in self.BLOCKED_IMPORTS:
                            return f"Blocked import: {name}"
        except SyntaxError as e:
            return f"SyntaxError: {e}"
        return None


# ══════════════════════════════════════════════════════════════════════════════
# SEMANTIC ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

class SemanticAnalyzer:
    """Text analysis: intent, entities, sentiment, keywords, summarization."""

    # Simple keyword-based sentiment (upgrade with transformers/Ollama)
    POSITIVE_WORDS = {"good","great","excellent","happy","love","amazing","wonderful","best"}
    NEGATIVE_WORDS = {"bad","terrible","awful","hate","worst","horrible","poor","fail"}

    def analyze(self, text: str) -> Dict[str, Any]:
        return {
            "word_count": len(text.split()),
            "char_count": len(text),
            "sentences": self._count_sentences(text),
            "keywords": self._extract_keywords(text),
            "sentiment": self._simple_sentiment(text),
            "language": self._detect_language(text),
            "entities": self._extract_entities(text),
            "reading_time_seconds": len(text.split()) // 3,  # ~180 wpm
        }

    def _count_sentences(self, text: str) -> int:
        return len(re.findall(r'[.!?]+', text))

    def _extract_keywords(self, text: str, top_n: int = 10) -> List[str]:
        STOPWORDS = {"the","a","an","is","it","in","on","at","to","for","of",
                     "and","or","but","i","we","you","he","she","they","this","that"}
        words = re.findall(r'\b[a-zA-Z]{3,}\b', text.lower())
        freq: Dict[str, int] = {}
        for w in words:
            if w not in STOPWORDS:
                freq[w] = freq.get(w, 0) + 1
        return sorted(freq, key=freq.get, reverse=True)[:top_n]

    def _simple_sentiment(self, text: str) -> Dict[str, Any]:
        words = set(text.lower().split())
        pos = len(words & self.POSITIVE_WORDS)
        neg = len(words & self.NEGATIVE_WORDS)
        score = (pos - neg) / max(len(words), 1)
        label = "positive" if score > 0.01 else "negative" if score < -0.01 else "neutral"
        return {"label": label, "score": round(score, 4), "positive": pos, "negative": neg}

    def _detect_language(self, text: str) -> str:
        # Heuristic - replace with langdetect in production
        ascii_ratio = sum(1 for c in text if ord(c) < 128) / max(len(text), 1)
        return "en" if ascii_ratio > 0.85 else "unknown"

    def _extract_entities(self, text: str) -> Dict[str, List[str]]:
        urls = re.findall(r'https?://[^\s]+', text)
        emails = re.findall(r'\b[\w.+-]+@[\w-]+\.[a-z]{2,}\b', text)
        # Capitalized sequences heuristic for names/orgs
        proper = re.findall(r'\b[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*\b', text)
        proper = [p for p in proper if len(p) > 2]
        return {"urls": urls, "emails": emails, "proper_nouns": list(set(proper))[:15]}


# ══════════════════════════════════════════════════════════════════════════════
# CYBERSECURITY TOOLKIT
# ══════════════════════════════════════════════════════════════════════════════

class SecurityToolkit:
    """Input validation, rate limiting, prompt injection detection, hashing."""

    INJECTION_PATTERNS = [
        r"ignore\s+previous\s+instructions",
        r"forget\s+(your|all)\s+(instructions|rules)",
        r"you\s+are\s+now\s+(a|an|the)",
        r"jailbreak", r"dan\s+mode", r"developer\s+mode",
        r"pretend\s+you\s+(are|have\s+no)",
        r"act\s+as\s+if\s+you",
        r"sudo\s+", r"system:\s*override",
    ]

    RATE_LIMITS: Dict[str, List[float]] = {}  # user_id -> [timestamps]

    def check_prompt_injection(self, text: str) -> Dict[str, Any]:
        text_lower = text.lower()
        findings = []
        for pattern in self.INJECTION_PATTERNS:
            if re.search(pattern, text_lower, re.IGNORECASE):
                findings.append(pattern)
        return {
            "safe": len(findings) == 0,
            "threats": findings,
            "risk_level": "high" if len(findings) > 1 else "medium" if findings else "low"
        }

    def rate_check(self, user_id: str, limit: int = None,
                   window: int = 60) -> Dict[str, Any]:
        limit = limit or CONFIG.RATE_LIMIT_PER_MINUTE
        now = time.time()
        bucket = self.RATE_LIMITS.setdefault(user_id, [])
        self.RATE_LIMITS[user_id] = [t for t in bucket if now - t < window]
        if len(self.RATE_LIMITS[user_id]) >= limit:
            return {"allowed": False, "remaining": 0,
                    "retry_after": window - (now - self.RATE_LIMITS[user_id][0])}
        self.RATE_LIMITS[user_id].append(now)
        return {"allowed": True, "remaining": limit - len(self.RATE_LIMITS[user_id])}

    def sanitize_input(self, text: str) -> str:
        """Basic sanitization - strip null bytes and control chars."""
        text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
        return text[:4096]  # hard cap

    def hash_data(self, data: str, algo: str = "sha256") -> str:
        h = hashlib.new(algo)
        h.update(data.encode())
        return h.hexdigest()

    def validate_url(self, url: str) -> Dict[str, Any]:
        pattern = re.compile(
            r'^https?://'
            r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'
            r'localhost|\d{1,3}(?:\.\d{1,3}){3})'
            r'(?::\d+)?(?:/?|[/?]\S+)$', re.IGNORECASE
        )
        is_valid = bool(pattern.match(url))
        is_localhost = bool(re.search(r'localhost|127\.|0\.0\.0\.0|::1', url))
        return {"valid": is_valid, "is_localhost": is_localhost,
                "safe": is_valid and not is_localhost}
