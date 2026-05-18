"""
OMNI AGENT - Configuration
Supports all 24 cloud models with per-task routing configuration.
"""
import os
from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class Config:
    # ═══════════════════════════════════════════════════════════════════════
    # OLLAMA / MODEL ENDPOINT
    # ═══════════════════════════════════════════════════════════════════════
    OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    OLLAMA_EMBEDDING_MODEL: str = os.getenv(
        "OLLAMA_EMBEDDING_MODEL", "nomic-embed-text"
    )

    # ── Default model (used when auto-routing is disabled) ─────────────────
    OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen3-next:80b-cloud")

    # ── Task-specific model overrides (leave empty to use auto-routing) ────
    MODEL_CODE: str = os.getenv("MODEL_CODE",      "qwen3-coder-next:cloud")
    MODEL_MATH: str = os.getenv("MODEL_MATH",      "deepseek-v3.1:671b-cloud")
    MODEL_VISION: str = os.getenv("MODEL_VISION",  "qwen3-vl:235b-instruct-cloud")
    MODEL_REASON: str = os.getenv("MODEL_REASON",  "cogito-2.1:671b-cloud")
    MODEL_FAST: str = os.getenv("MODEL_FAST",      "gemma3:4b-cloud")
    MODEL_CREATIVE: str = os.getenv("MODEL_CREATIVE","gpt-oss:120b-cloud")
    MODEL_AGENT: str = os.getenv("MODEL_AGENT",    "devstral-2:123b-cloud")
    MODEL_MULTILANG: str = os.getenv("MODEL_MULTILANG","mistral-large-3:675b-cloud")
    MODEL_LONGCTX: str = os.getenv("MODEL_LONGCTX","minimax-m2.5:cloud")

    # ── Auto-routing toggle ────────────────────────────────────────────────
    MODEL_AUTO_ROUTE: bool = os.getenv("MODEL_AUTO_ROUTE", "true").lower() == "true"

    # ── Routing preferences ────────────────────────────────────────────────
    MODEL_EXCLUDE: List[str] = field(default_factory=lambda: [
        x.strip() for x in os.getenv("MODEL_EXCLUDE", "").split(",") if x.strip()
    ])
    MODEL_PREFER_PROVIDER: str = os.getenv("MODEL_PREFER_PROVIDER", "")
    MODEL_COMPARE_IDS: List[str] = field(default_factory=lambda: [
        x.strip() for x in os.getenv(
            "MODEL_COMPARE_IDS",
            "qwen3-next:80b-cloud,gpt-oss:120b-cloud,deepseek-v3.1:671b-cloud"
        ).split(",") if x.strip()
    ])

    # ═══════════════════════════════════════════════════════════════════════
    # TELEGRAM
    # ═══════════════════════════════════════════════════════════════════════
    TELEGRAM_TOKEN: str = os.getenv("TELEGRAM_TOKEN", "")
    TELEGRAM_ALLOWED_USERS: list = field(default_factory=lambda: [
        int(x) for x in os.getenv("TELEGRAM_ALLOWED_USERS", "").split(",") if x
    ])

    # ═══════════════════════════════════════════════════════════════════════
    # DATABASE
    # ═══════════════════════════════════════════════════════════════════════
    DB_PATH: str = os.getenv("DB_PATH", "data/omni_agent.db")
    DB_BACKEND: str = os.getenv("DB_BACKEND", "sqlite")
    POSTGRES_DSN: str = os.getenv(
        "POSTGRES_DSN", "postgresql://omni:omni@localhost:5432/omni_agent"
    )
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # ═══════════════════════════════════════════════════════════════════════
    # WEB SCRAPING
    # ═══════════════════════════════════════════════════════════════════════
    SCRAPER_TIMEOUT: int = int(os.getenv("SCRAPER_TIMEOUT", "15"))
    SCRAPER_MAX_RETRIES: int = int(os.getenv("SCRAPER_MAX_RETRIES", "3"))
    SEARXNG_URL: str = os.getenv("SEARXNG_URL", "http://localhost:8080")

    # ═══════════════════════════════════════════════════════════════════════
    # SECURITY
    # ═══════════════════════════════════════════════════════════════════════
    SECRET_KEY: str = os.getenv("SECRET_KEY", "CHANGE_ME_IN_PRODUCTION")
    RATE_LIMIT_PER_MINUTE: int = int(os.getenv("RATE_LIMIT_PER_MINUTE", "30"))
    ENABLE_SANDBOX: bool = os.getenv("ENABLE_SANDBOX", "true").lower() == "true"
    AUTH_ENFORCE: bool = os.getenv("AUTH_ENFORCE", "true").lower() == "true"
    AUTH_BOOTSTRAP_TOKEN: str = os.getenv("AUTH_BOOTSTRAP_TOKEN", "")

    # ═══════════════════════════════════════════════════════════════════════
    # HEARTBEAT
    # ═══════════════════════════════════════════════════════════════════════
    HEARTBEAT_INTERVAL: int = int(os.getenv("HEARTBEAT_INTERVAL", "60"))
    HEARTBEAT_WEBHOOK: str = os.getenv("HEARTBEAT_WEBHOOK", "")

    # ═══════════════════════════════════════════════════════════════════════
    # MEMORY
    # ═══════════════════════════════════════════════════════════════════════
    MEMORY_MAX_TOKENS: int = int(os.getenv("MEMORY_MAX_TOKENS", "4096"))
    MEMORY_SUMMARY_THRESHOLD: int = int(os.getenv("MEMORY_SUMMARY_THRESHOLD", "20"))

    # ═══════════════════════════════════════════════════════════════════════
    # LOGGING
    # ═══════════════════════════════════════════════════════════════════════
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_FILE: str = os.getenv("LOG_FILE", "logs/omni_agent.log")

    # ═══════════════════════════════════════════════════════════════════════
    # API SERVER
    # ═══════════════════════════════════════════════════════════════════════
    API_HOST: str = os.getenv("API_HOST", "0.0.0.0")
    API_PORT: int = int(os.getenv("API_PORT", "8000"))
    API_FALLBACK_PORTS: List[int] = field(default_factory=lambda: [
        int(x.strip()) for x in os.getenv("API_FALLBACK_PORTS", "8010").split(",")
        if x.strip()
    ])

    # ═══════════════════════════════════════════════════════════════════════
    # ANTHROPIC (optional fallback)
    # ═══════════════════════════════════════════════════════════════════════
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")


CONFIG = Config()
