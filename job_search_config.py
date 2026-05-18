"""
Shared configuration for all job-search scripts.

Usage in any script (from root or from scripts/job_search/):
    from job_search_config import JOB_RESULTS_DIR

The folder is created automatically on first import.
"""
from pathlib import Path

# Project root = directory containing this file
_PROJECT_ROOT = Path(__file__).parent

# All job search output files (JSON / CSV / HTML) go here
JOB_RESULTS_DIR: Path = _PROJECT_ROOT / "data" / "job_results"
JOB_RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# Active source groups for the ADR tanker search target.
TARGET_JOB_GROUPS: tuple[str, ...] = ("CH-DE", "CH-FR", "CH-IT", "CH-EN", "SOCIAL")

# Swiss job search results should stay on Swiss domains or explicitly allowed boards.
CH_RESULT_DOMAIN_SUFFIXES: tuple[str, ...] = (".ch",)
CH_RESULT_EXTRA_DOMAINS: frozenset[str] = frozenset({
    "jooble.org",
})

# Social-media search results are limited to job-like pages on the core platforms.
SOCIAL_RESULT_DOMAINS: frozenset[str] = frozenset({
    "linkedin.com",
    "facebook.com",
    "xing.com",
    "twitter.com",
    "x.com",
    "reddit.com",
})
SOCIAL_RESULT_URL_PATTERNS: tuple[str, ...] = (
    r"linkedin\.com/jobs/",
    r"xing\.com/jobs/",
    r"facebook\.com/(groups/|[^/]+/posts/)",
    r"(?:twitter|x)\.com/.+/status/",
    r"reddit\.com/r/",
)

# Noisy result sources/pages that routinely rank high but are not actual target jobs.
BLOCKED_RESULT_DOMAINS: frozenset[str] = frozenset({
    "cv-library.co.uk",
    "expertini.com",
    "gastrobaiter.com",
    "glassdoor.com",
    "glassdoor.ie",
    "hegelmann.com",
    "indeed.com",
    "jobkeep.eu",
    "jobtransport.com",
    "reed.co.uk",
    "totaljobs.com",
    "ziprecruiter.com",
    "247drive.com",
})
BLOCKED_RESULT_TITLE_PATTERNS: tuple[str, ...] = (
    r"\bsalar(?:y|ies)\b",
    r"\breviews?\b",
    r"\bprofile\b",
    r"\bhiring guide\b",
    r"\bhow to hire\b",
    r"\bnear me\b",
    r"\bhighest paying\b",
    r"\bmost popular types\b",
)
