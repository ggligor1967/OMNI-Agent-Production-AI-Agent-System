#!/usr/bin/env python
"""
Improved ADR tanker / liquid bulk job search for Switzerland.

Key improvements over the original version:
- broader and cleaner query generation
- less destructive domain filtering
- URL canonicalization for better deduplication
- concurrent search execution
- context enrichment for more candidates
- stronger scoring using title + snippet + fetched page content
- adaptive retry/expansion when high-signal results are scarce
"""

import argparse
import asyncio
import csv
import json
import re
import sys
from collections import Counter
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

if sys.platform == "win32":
    import io

sys.path.insert(0, str(Path(__file__).parent))

from agent.tools import WebScraper
from job_search_config import (
    BLOCKED_RESULT_DOMAINS,
    BLOCKED_RESULT_TITLE_PATTERNS,
    CH_RESULT_DOMAIN_SUFFIXES,
    CH_RESULT_EXTRA_DOMAINS,
    JOB_RESULTS_DIR,
    SOCIAL_RESULT_DOMAINS,
    SOCIAL_RESULT_URL_PATTERNS,
)

CANDIDATE = {
    "role": "ADR Liquid / Tank Container Driver",
    "experience_years": 10,
    "license": "Category CE (truck + trailer)",
    "adr_certificates": [
        "ADR Class 2 – Gases",
        "ADR Class 3 – Flammable liquids",
        "ADR Class 6 – Toxic & infectious substances",
        "ADR Class 8 – Corrosives",
        "ADR Class 9 – Miscellaneous",
        "ADR Tanker Certificate (liquid bulk)",
    ],
    "documents_valid": [
        "CE Driving Licence",
        "ADR Certificate",
        "Digital Tachograph Card",
        "CPC / Driver Qualification Card",
        "Medical Certificate",
        "Tank Container Certificate",
    ],
    "languages": "English, Romanian",
    "availability": "immediate",
    "work_permit": "EU citizen — no sponsorship required",
}
OUTPUT_DIR = JOB_RESULTS_DIR
TARGET_LABEL = "Switzerland (CH) + Social Media"
ALLOWED_EXPORT_FORMATS = {"json", "csv", "html", "all"}

SEARCH_CONCURRENCY = 5
CONTEXT_CONCURRENCY = 8
SEARCH_RESULTS_PER_QUERY = 15
SEARCH_BATCH_SLEEP = 0.15
MIN_PRELIM_SCORE_FOR_FETCH = 10
CONTEXT_ENRICH_LIMIT = 45
HIGH_THRESHOLD_DEFAULT = 52
MEDIUM_THRESHOLD_DEFAULT = 28
NON_DETAIL_SCORE_CAP = 41

KNOWN_JOB_DOMAINS = {
    "jobs.ch",
    "jobup.ch",
    "stepstone.ch",
    "jobscout24.ch",
    "jobagent.ch",
    "monster.ch",
    "careerjet.ch",
    "jooble.org",
    "job-room.ch",
    "ostjob.ch",
    "regionaljob.ch",
    "swissjobs.ch",
    "indeed.ch",
    "optioncarriere.ch",
}

AGGREGATOR_RESULT_DOMAINS = {
    "jooble.org",
    "careerjet.ch",
    "optioncarriere.ch",
    "suissetalent.ch",
    "tutti.ch",
}

LOCAL_BLOCKED_RESULT_DOMAINS = {
    "whatjobs.com",
    "englishjobsearch.ch",
}

LOCATION_TERMS = [
    "Switzerland",
    "Schweiz",
    "Suisse",
    "Svizzera",
    "Zurich",
    "Basel",
    "Geneva",
    "Bern",
    "Lausanne",
    "Zug",
    "Aargau",
    "Luzern",
    "Ticino",
]

ROLE_TERMS = [
    "ADR Tankfahrer",
    "Tankwagenfahrer ADR",
    "Chauffeur citerne ADR",
    "autista cisterna ADR",
    "ADR tanker driver",
    "dangerous goods driver",
    "tank container driver",
    "liquid bulk driver",
    "chemical tanker driver",
    "fuel tanker driver",
]

BASE_QUERIES = [
    # High intent / role-first
    ("CH", "ADR tanker driver Switzerland"),
    ("CH", "ADR liquid bulk driver Switzerland"),
    ("CH", "tank container driver Switzerland ADR"),
    ("CH", "chemical tanker driver Switzerland"),
    ("CH", "fuel tanker driver Switzerland ADR"),
    ("CH", "Gefahrgut Tankwagenfahrer Schweiz"),
    ("CH", "ADR Tankfahrer Schweiz"),
    ("CH", "Chauffeur citerne ADR Suisse"),
    ("CH", "autista cisterna ADR Svizzera"),
    # Board constrained, but fewer and cleaner than before
    ("BOARD", "site:jobs.ch ADR Tankfahrer Schweiz"),
    ("BOARD", "site:jobup.ch chauffeur citerne ADR Suisse"),
    ("BOARD", "site:indeed.ch ADR tanker driver Switzerland"),
    ("BOARD", "site:jobscout24.ch Tankwagenfahrer ADR"),
    ("BOARD", "site:job-room.ch ADR Fahrer Tank"),
    # Social / network surfaces
    ("SOCIAL", "site:linkedin.com/jobs ADR tanker driver Switzerland"),
    ("SOCIAL", "site:linkedin.com/jobs tank container driver Switzerland"),
    ("SOCIAL", "site:facebook.com/groups ADR Fahrer Schweiz job"),
]

HIGH_RELEVANCE = [
    r"\badr\b",
    r"tanker",
    r"tank\s*(container|wagen|zug|truck|trailer)?",
    r"liquid",
    r"bulk",
    r"fl[üu]ssig",
    r"citerne",
    r"cistern",
    r"gefahrgut",
    r"hazmat",
    r"hazardous",
    r"dangerous goods",
    r"chemical",
    r"petrol",
    r"fuel",
    r"diesel",
    r"chauffeur",
    r"fahrer",
    r"driver",
    r"autista",
    r"\bce\b",
    r"klasse\s*3",
    r"class\s*3",
]

JOB_SIGNAL_PATTERNS = [
    r"\bjob(s)?\b",
    r"stelle",
    r"stellenangebot",
    r"emploi",
    r"offre d'emploi",
    r"vacanc(y|ies)",
    r"hiring",
    r"careers?",
    r"recruit",
    r"bewerben",
    r"apply",
]

LOCATION_PATTERNS = [
    r"\bswitzerland\b",
    r"\bschweiz\b",
    r"\bsuisse\b",
    r"\bsvizzera\b",
    r"\bzurich\b",
    r"\bzürich\b",
    r"\bbasel\b",
    r"\bgeneva\b",
    r"\bgenève\b",
    r"\bbern\b",
    r"\blausanne\b",
    r"\bzug\b",
    r"\blucerne\b",
    r"\bluzern\b",
    r"\bst\.? gallen\b",
    r"\baargau\b",
    r"\bticino\b",
    r"\blugano\b",
]

NEGATIVE_PATTERNS = [
    r"course",
    r"training",
    r"\bcorso\b",
    r"formation",
    r"ausbildung",
    r"school",
    r"\bscuola\b",
    r"salary guide",
    r"tourism",
    r"hotel",
    r"bus driver",
    r"taxi",
    r"\bmaritime\b",
    r"\bship\b",
    r"\bvessel\b",
    r"\bseafarer\b",
    r"\b2nd engineer\b",
]

AGGREGATOR_PATTERNS = [
    r"\b\d+[.,']?\d*\+?\s+(current\s+)?jobs?\b",
    r"\bextensive selection\b",
    r"\bjob-?mail service\b",
    r"\bjobsuche\b",
    r"\bführende arbeitgeber\b",
    r"\bfind the .* job\b",
    r"\bapply now for .* jobs\b",
    r"\btrouvez votre emploi en suisse parmi\b",
    r"\bstellenangebote auf\b",
    r"\bjobs in [a-zà-ÿ\- ]+\b",
]

INTERSTITIAL_PATTERNS = [
    r"access (?:has )?been denied",
    r"access denied",
    r"acc[eè]s (?:a )?été refusé",
    r"accesso .* negato",
    r"security service",
    r"online-?angriffen",
    r"protect our (?:site|website)",
    r"parameters\s*:\s*\{",
    r"access-control-allow-methods",
    r"page not found",
    r"seite wurde nicht gefunden",
    r"page demandée n[’']a pas pu être trouvée",
]

NON_JOB_URL_PATTERNS = [
    r"/(?:aus-weiterbildung|weiterbildung|formation|training|academy|school)(?:/|$)",
    r"/(?:berufswelt|career-guide|guide)(?:/|$)",
]

DIRECT_JOB_URL_PATTERNS = [
    r"/(?:jobs/detail|job|emploi|offre-emploi|vacancy|vacancies/detail|stellenangebote/detail)(?:/|$)",
    r"/(?:careers?|karriere)/(?:[^/?]+/)?(?:job|jobs|vacancy|emploi|offre)(?:/|$)",
]

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang=\"en\">
<head>
<meta charset=\"UTF-8\">
<title>ADR Tank Driver Jobs — {date}</title>
<style>
body{{font-family:Arial,sans-serif;max-width:1280px;margin:0 auto;padding:20px;background:#f5f5f5}}
h1{{color:#1a237e;border-bottom:3px solid #1a237e;padding-bottom:10px}}
h2{{color:#283593}}
.stats{{background:#e8eaf6;padding:15px;border-radius:8px;margin:20px 0;display:flex;gap:30px;flex-wrap:wrap}}
.stat{{text-align:center}} .stat .num{{font-size:2em;font-weight:bold;color:#1a237e}}
.stat .lbl{{font-size:.85em;color:#555}}
table{{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 6px rgba(0,0,0,.1)}}
th{{background:#1a237e;color:#fff;padding:10px 12px;text-align:left;font-size:.9em}}
td{{padding:9px 12px;font-size:.85em;vertical-align:top;border-bottom:1px solid #eee}}
tr:hover{{background:#e8eaf6}}
a{{color:#1a237e;text-decoration:none}} a:hover{{text-decoration:underline}}
.score-high{{color:#2e7d32;font-weight:bold}}
.score-med{{color:#f57c00;font-weight:bold}}
.score-low{{color:#c62828}}
.group{{display:inline-block;padding:2px 7px;border-radius:10px;font-size:.75em;font-weight:bold;color:#fff}}
.g-CH{{background:#1565c0}} .g-BOARD{{background:#2e7d32}} .g-SOCIAL{{background:#ad1457}} .g-EXPAND{{background:#6a1b9a}}
.note{{background:#fff8e1;border-left:4px solid #fb8c00;padding:14px 16px;border-radius:8px;margin:18px 0;color:#4e342e}}
@media(max-width:600px){{th:nth-child(3),th:nth-child(6),td:nth-child(3),td:nth-child(6){{display:none}}}}
</style>
</head>
<body>
<h1>ADR Liquid / Tank Container Driver — Improved Search Report</h1>
<p><strong>Date:</strong> {date} &nbsp;|&nbsp;
   <strong>Target:</strong> {target} &nbsp;|&nbsp;
   <strong>Queries:</strong> {queries}</p>
<div class=\"stats\">
  <div class=\"stat\"><div class=\"num\">{total}</div><div class=\"lbl\">Total results</div></div>
  <div class=\"stat\"><div class=\"num\">{high}</div><div class=\"lbl\">High relevance</div></div>
  <div class=\"stat\"><div class=\"num\">{boards}</div><div class=\"lbl\">Unique sources</div></div>
  <div class=\"stat\"><div class=\"num\">{enriched}</div><div class=\"lbl\">Page-enriched</div></div>
</div>
<div class=\"note\"><strong>Diagnostics:</strong> {diagnostics_note}</div>
<h2>Results</h2>
<table>
<thead><tr><th>#</th><th>Title</th><th>Company</th><th>URL</th><th>Group</th><th>Score</th><th>Signals</th><th>Snippet</th></tr></thead>
<tbody>
{rows}
</tbody>
</table>
</body></html>
"""


def configure_stdio() -> None:
    if sys.platform != "win32":
        return
    if not hasattr(sys.stdout, "buffer") or not hasattr(sys.stderr, "buffer"):
        return
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


def format_run_date(run_started: datetime | None = None) -> str:
    return (run_started or datetime.now()).strftime("%Y-%m-%d")


def format_run_tag(run_started: datetime | None = None) -> str:
    return (run_started or datetime.now()).strftime("%Y%m%d_%H%M%S")


def validate_export_format(export_format: str) -> str:
    normalized = (export_format or "html").strip().lower()
    if normalized not in ALLOWED_EXPORT_FORMATS:
        raise ValueError(
            f"Invalid export format '{export_format}'. Expected one of: {sorted(ALLOWED_EXPORT_FORMATS)}"
        )
    return normalized


def resolve_output_dir(output_dir: str | Path | None = None) -> Path:
    resolved = Path(output_dir) if output_dir else OUTPUT_DIR
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def build_run_summary(
    jobs: list[dict],
    diagnostics: dict,
    thresholds: dict,
    queries: int,
    boards: int,
    output_files: dict[str, str],
    search_date: str,
    export_format: str,
) -> dict:
    resolved_files = {
        name: str(Path(path).resolve())
        for name, path in output_files.items()
        if path
    }
    high = sum(1 for job in jobs if job["score"] >= thresholds["high"])
    med = sum(1 for job in jobs if thresholds["medium"] <= job["score"] < thresholds["high"])
    low = sum(1 for job in jobs if job["score"] < thresholds["medium"])
    html_report = resolved_files.get("html")

    return {
        "search_date": search_date,
        "target": TARGET_LABEL,
        "export_format": export_format,
        "queries_executed": queries,
        "unique_sources": boards,
        "total_results": len(jobs),
        "high_relevance": high,
        "medium_relevance": med,
        "low_relevance": low,
        "filtered_results": sum(diagnostics.get("filtered_counts", {}).values()),
        "relevance_thresholds": thresholds,
        "diagnostics_note": diagnostics.get("note", ""),
        "report_files": resolved_files,
        "html_report_uri": Path(html_report).resolve().as_uri() if html_report else "",
    }


def compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def pattern_hits(patterns, text: str) -> int:
    if not text:
        return 0
    return sum(1 for pattern in patterns if re.search(pattern, text, re.I))


def extract_domain(url: str) -> str:
    domain = urlparse(url).netloc.lower().strip()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def domain_matches(domain: str, candidates) -> bool:
    return any(domain == candidate or domain.endswith(f".{candidate}") for candidate in candidates)


def clean_redirect(url: str) -> str:
    if "duckduckgo.com/l/?uddg=" in url:
        match = re.search(r"uddg=([^&]+)", url)
        if match:
            return unquote(match.group(1))
    return url


def canonicalize_url(url: str) -> str:
    url = clean_redirect(url)
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]

    filtered_qs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=False):
        if key.lower().startswith("utm_"):
            continue
        if key.lower() in {"trk", "trkinfo", "ref", "refid", "fbclid", "gclid", "mc_cid", "mc_eid"}:
            continue
        filtered_qs.append((key, value))

    path = parsed.path.rstrip("/") or "/"

    detail_match = re.search(
        r"^/(?:de|fr|it|en)/(?:jobs|emplois|offres-emplois|stellenangebote)/detail/([a-z0-9-]+)$",
        path,
        re.I,
    )
    if detail_match and domain in {"jobs.ch", "jobup.ch"}:
        path = f"/jobs/detail/{detail_match.group(1).lower()}"

    scout_match = re.search(r"^/(?:de|fr|it|en)/job/([a-z0-9-]+)$", path, re.I)
    if scout_match and domain == "jobscout24.ch":
        path = f"/job/{scout_match.group(1).lower()}"

    normalized = parsed._replace(netloc=domain, query=urlencode(filtered_qs), fragment="", path=path)
    return urlunparse(normalized)


def social_url_allowed(url: str) -> bool:
    return any(re.search(pattern, url, re.I) for pattern in SOCIAL_RESULT_URL_PATTERNS)


def extract_company_name(title: str, snippet: str, url: str) -> str:
    title = compact_text(title)
    for sep in (" - ", " | ", " — ", " · "):
        if sep in title:
            parts = [p.strip() for p in title.split(sep) if p.strip()]
            if len(parts) >= 2:
                last = parts[-1]
                if 2 <= len(last) <= 50 and not re.search(r"job|stelle|emploi|career|vacancy", last, re.I):
                    return last
    domain = extract_domain(url)
    if domain:
        stem = domain.split(".")[0]
        if stem not in {"jobs", "jobup", "linkedin", "facebook", "indeed", "monster", "careerjet"}:
            return stem.replace("-", " ").title()
    return ""


def classify_source_kind(group: str, url: str) -> str:
    domain = extract_domain(url)
    if group == "SOCIAL" or domain_matches(domain, SOCIAL_RESULT_DOMAINS):
        return "social"
    if domain_matches(domain, AGGREGATOR_RESULT_DOMAINS):
        return "aggregator"
    if domain_matches(domain, KNOWN_JOB_DOMAINS):
        return "job-board"
    return "company-page"


def has_direct_job_url(url: str) -> bool:
    return any(re.search(pattern, url, re.I) for pattern in DIRECT_JOB_URL_PATTERNS)


def is_result_allowed(group: str, url: str, title: str, snippet: str) -> tuple[bool, str]:
    domain = extract_domain(url)
    combined = compact_text(f"{title} {snippet}")
    job_hits = pattern_hits(JOB_SIGNAL_PATTERNS, combined)
    negative_hits = pattern_hits(NEGATIVE_PATTERNS, combined)

    if not domain:
        return False, "invalid-domain"
    if domain_matches(domain, BLOCKED_RESULT_DOMAINS):
        return False, "blocked-domain"
    if domain_matches(domain, LOCAL_BLOCKED_RESULT_DOMAINS):
        return False, "blocked-local-domain"
    if any(re.search(pattern, combined, re.I) for pattern in BLOCKED_RESULT_TITLE_PATTERNS):
        return False, "blocked-title"
    if any(re.search(pattern, url, re.I) for pattern in NON_JOB_URL_PATTERNS):
        return False, "non-job-path"
    if negative_hits and job_hits == 0:
        return False, "negative-content"

    if group == "SOCIAL":
        if not domain_matches(domain, SOCIAL_RESULT_DOMAINS):
            return False, "non-social-domain"
        if not social_url_allowed(url):
            return False, "social-path"
        if pattern_hits(LOCATION_PATTERNS, combined) == 0:
            return False, "non-target-location"
        return True, ""

    if domain.endswith(CH_RESULT_DOMAIN_SUFFIXES) or domain_matches(domain, CH_RESULT_EXTRA_DOMAINS):
        return True, ""

    if domain_matches(domain, KNOWN_JOB_DOMAINS):
        return True, ""

    # Important relaxation: allow company career pages if the text itself looks like a real target job.
    strong_role_hits = pattern_hits(HIGH_RELEVANCE, combined)
    location_hits = pattern_hits(LOCATION_PATTERNS, combined)
    driver_hits = re.search(r"\b(chauffeur|fahrer|driver|autista)\b", combined, re.I)
    if strong_role_hits >= 3 and job_hits >= 1 and location_hits >= 1 and driver_hits and has_direct_job_url(url):
        return True, "company-career-page"

    return False, "outside-target-domain"


def compute_score(title: str, snippet: str, page_text: str = "", url: str = "", source_kind: str = "company-page") -> tuple[int, list[str]]:
    title_text = compact_text(title)
    snippet_text = compact_text(snippet)
    page_text = compact_text(page_text)[:2500]
    combined = compact_text(f"{title_text} {snippet_text} {page_text}")

    title_role_hits = pattern_hits(HIGH_RELEVANCE, title_text)
    snippet_role_hits = pattern_hits(HIGH_RELEVANCE, snippet_text)
    page_role_hits = pattern_hits(HIGH_RELEVANCE, page_text)
    job_hits = pattern_hits(JOB_SIGNAL_PATTERNS, combined)
    location_hits = pattern_hits(LOCATION_PATTERNS, combined)
    negative_hits = pattern_hits(NEGATIVE_PATTERNS, combined)

    score = 0
    score += title_role_hits * 13
    score += min(snippet_role_hits * 7, 21)
    score += min(page_role_hits * 4, 20)
    score += min(job_hits * 5, 15)
    score += min(location_hits * 4, 12)

    if re.search(r"\badr\b", combined, re.I) and re.search(r"tank|citerne|cistern|bulk|liquid", combined, re.I):
        score += 10
    if re.search(r"\bce\b", combined, re.I):
        score += 5
    if re.search(r"class\s*3|klasse\s*3|flammable liquids|chemical", combined, re.I):
        score += 4
    if re.search(r"apply|bewerben|postuler|job details|stellenbeschreibung", combined, re.I):
        score += 4
    if re.search(r"linkedin\.com/jobs|indeed\.|jobs\.|jobup\.|jobscout24\.", url, re.I):
        score += 4

    if source_kind == "job-board":
        score += 6
    elif source_kind == "aggregator":
        score -= 20
    elif source_kind == "social":
        score -= 26

    if snippet_text:
        score += min(len(snippet_text) // 80, 4)
    if page_text:
        score += 5

    is_listing_page = (
        re.search(r"[?&](term|q|location)=", url, re.I)
        or (
            re.search(r"/(vacancies|jobs|offres-emplois)(?:$|[/?])", url, re.I)
            and not re.search(r"/(detail|job|offre-emploi|offres-emplois/detail|vacancies/detail)(?:$|[/?])", url, re.I)
        )
        or re.search(r"/jobs-in-[^/?]+", url, re.I)
        or re.search(r"^\d+\s+.*jobs?\b", title_text, re.I)
        or re.search(r"job ads|apply now for .* jobs|find the .* job", combined, re.I)
    )
    is_detail_page = re.search(
        r"/(detail|job|offre-emploi|offres-emplois/detail|vacancies/detail)(?:$|[/?])",
        url,
        re.I,
    )
    has_application_signal = re.search(
        r"apply|bewerben|postuler|job details|stellenbeschreibung|offre d'emploi|emploi|stelle|vacanc",
        combined,
        re.I,
    )
    is_direct_opening = bool(
        is_detail_page
        or has_direct_job_url(url)
        or (has_application_signal and job_hits >= 1 and (title_role_hits + snippet_role_hits + page_role_hits) >= 2)
    )
    has_broken_page = bool(page_text and pattern_hits(INTERSTITIAL_PATTERNS, page_text))

    if is_listing_page:
        score -= 18
    if is_detail_page:
        score += 8
    if re.search(r"\b(i am|i'm|looking for a job)\b", combined, re.I):
        score -= 20
    if pattern_hits(AGGREGATOR_PATTERNS, combined):
        score -= 18
    if pattern_hits(INTERSTITIAL_PATTERNS, combined):
        score -= 32
    if source_kind == "social":
        score = min(score, NON_DETAIL_SCORE_CAP - 3)
    elif source_kind == "aggregator":
        score = min(score, NON_DETAIL_SCORE_CAP)
    elif is_listing_page:
        score = min(score, NON_DETAIL_SCORE_CAP)
    if job_hits == 0 and not is_detail_page and not has_application_signal:
        score = min(score, NON_DETAIL_SCORE_CAP)
    if source_kind == "company-page" and not is_direct_opening:
        score = min(score, NON_DETAIL_SCORE_CAP)
    if has_broken_page:
        score = min(score, NON_DETAIL_SCORE_CAP)

    score -= negative_hits * 22
    if not snippet_text and not page_text:
        score -= 8

    signals = []
    if re.search(r"\badr\b", combined, re.I):
        signals.append("ADR")
    if re.search(r"tank|citerne|cistern|bulk|liquid", combined, re.I):
        signals.append("tank/liquid")
    if re.search(r"chemical|fuel|hazmat|dangerous goods|gefahrgut", combined, re.I):
        signals.append("hazmat")
    if job_hits:
        signals.append("job")
    if location_hits:
        signals.append("CH")
    if re.search(r"\bce\b", combined, re.I):
        signals.append("CE")

    return max(0, min(score, 100)), signals


def determine_thresholds(jobs: list[dict]) -> dict:
    if not jobs:
        return {"high": HIGH_THRESHOLD_DEFAULT, "medium": MEDIUM_THRESHOLD_DEFAULT}

    snippet_cov = sum(1 for job in jobs if compact_text(job.get("snippet", ""))) / len(jobs)
    page_cov = sum(1 for job in jobs if compact_text(job.get("page_excerpt", ""))) / len(jobs)

    high = HIGH_THRESHOLD_DEFAULT
    if snippet_cov < 0.35:
        high -= 5
    if page_cov > 0.35:
        high += 2

    return {"high": max(42, high), "medium": MEDIUM_THRESHOLD_DEFAULT}


def score_tag(score: int, thresholds: dict) -> str:
    if score >= thresholds["high"]:
        return "[H]"
    if score >= thresholds["medium"]:
        return "[M]"
    return "[L]"


def build_context_snippet(page: dict) -> tuple[str, str]:
    title = compact_text(page.get("title", ""))
    description = compact_text(page.get("description", ""))
    body = compact_text(page.get("body", ""))

    parts = []
    if description:
        parts.append(description)
    if body:
        for sentence in re.split(r"(?<=[.!?])\s+", body):
            sentence = sentence.strip()
            if len(sentence) < 40:
                continue
            parts.append(sentence)
            if len(" ".join(parts)) >= 550:
                break

    return title, " ".join(parts)[:550].strip()


def generate_queries() -> list[tuple[str, str]]:
    queries = list(BASE_QUERIES)

    # Controlled expansion: a few role/location combinations with high semantic value.
    for role in ["ADR tanker driver", "ADR Tankfahrer", "Chauffeur citerne ADR", "tank container driver"]:
        for location in ["Switzerland", "Zurich", "Basel", "Ticino"]:
            queries.append(("EXPAND", f"{role} {location}"))

    # Deduplicate while preserving order.
    seen = set()
    deduped = []
    for group, query in queries:
        key = query.lower().strip()
        if key in seen:
            continue
        seen.add(key)
        deduped.append((group, query))
    return deduped


async def probe_searxng_available() -> bool:
    probe_scraper = WebScraper()
    if not getattr(probe_scraper, "_searxng_available", False):
        return False

    try:
        await probe_scraper._search_searxng("site:jobs.ch adr tanker driver switzerland", 1)
        return True
    except Exception:
        return False


async def search_one(semaphore: asyncio.Semaphore, group: str, query: str, searxng_available: bool) -> dict:
    async with semaphore:
        scraper = WebScraper()
        scraper._searxng_available = searxng_available
        results = await scraper.search(query, num_results=SEARCH_RESULTS_PER_QUERY)
        backend = getattr(scraper, "last_search_backend", "unknown")
        await asyncio.sleep(SEARCH_BATCH_SLEEP)
        return {"group": group, "query": query, "results": results, "backend": backend}


async def enrich_contexts(scraper: WebScraper, jobs: list[dict], verbose: bool) -> int:
    candidates = []
    for job in jobs:
        snippet_len = len(compact_text(job.get("snippet", "")))
        prelim_score = job.get("score", 0)
        if prelim_score >= MIN_PRELIM_SCORE_FOR_FETCH and (snippet_len < 140 or job.get("source_kind") == "company-page"):
            candidates.append(job)

    candidates = sorted(candidates, key=lambda item: (item["score"], item.get("source_kind") == "company-page"), reverse=True)
    candidates = candidates[:CONTEXT_ENRICH_LIMIT]

    if not candidates:
        return 0

    semaphore = asyncio.Semaphore(CONTEXT_CONCURRENCY)

    async def enrich(job: dict) -> bool:
        async with semaphore:
            page = await scraper.fetch(job["url"])

        if page.get("error"):
            return False

        page_title, excerpt = build_context_snippet(page)
        combined_page = compact_text(f"{page_title} {excerpt}")
        if not combined_page:
            return False

        if page_title and len(compact_text(job.get("title", ""))) < 18:
            job["title"] = page_title

        if len(excerpt) > len(compact_text(job.get("snippet", ""))):
            job["snippet"] = excerpt[:300]

        job["page_excerpt"] = excerpt
        job["context_source"] = "page_fetch"
        job["company"] = job.get("company") or extract_company_name(job["title"], excerpt, job["url"])
        job["score"], job["signals"] = compute_score(
            job["title"],
            job.get("snippet", ""),
            excerpt,
            job["url"],
            job.get("source_kind", "company-page"),
        )
        return True

    results = await asyncio.gather(*(enrich(job) for job in candidates), return_exceptions=True)
    enriched = sum(1 for item in results if item is True)
    if verbose and enriched:
        print(f"  Context enrichment: {enriched} results updated from fetched pages")
    return enriched


def build_diagnostics(jobs: list[dict], backend_counts: Counter, filtered_counts: Counter, enriched_count: int, thresholds: dict) -> dict:
    total = max(len(jobs), 1)
    snippet_coverage = sum(1 for job in jobs if compact_text(job.get("snippet", ""))) / total
    page_coverage = sum(1 for job in jobs if compact_text(job.get("page_excerpt", ""))) / total
    source_kind_counts = Counter(job.get("source_kind", "unknown") for job in jobs)

    notes = []
    if backend_counts.get("searxng", 0) == 0 and backend_counts.get("duckduckgo_html", 0) > 0:
        notes.append("SearXNG was unavailable; fallback search backend dominated this run.")
    if sum(filtered_counts.values()):
        notes.append(f"{sum(filtered_counts.values())} results were filtered before scoring.")
    notes.append(f"Snippet coverage: {snippet_coverage:.0%}. Page enrichment coverage: {page_coverage:.0%}.")
    notes.append(f"High threshold: >= {thresholds['high']}; medium threshold: >= {thresholds['medium']}.")
    if enriched_count:
        notes.append(f"{enriched_count} candidates were re-scored using fetched page content.")

    return {
        "backend_counts": dict(backend_counts),
        "filtered_counts": dict(filtered_counts),
        "source_kind_counts": dict(source_kind_counts),
        "context_enriched_results": enriched_count,
        "snippet_coverage": round(snippet_coverage, 4),
        "page_coverage": round(page_coverage, 4),
        "note": " ".join(notes),
    }


def export_json(
    jobs: list[dict],
    base: str,
    diagnostics: dict,
    thresholds: dict,
    queries: int,
    boards: int,
    search_date: str,
) -> str:
    payload = {
        "search_date": search_date,
        "target": TARGET_LABEL,
        "candidate": CANDIDATE,
        "queries_executed": queries,
        "unique_sources": boards,
        "relevance_thresholds": thresholds,
        "diagnostics": diagnostics,
        "total_results": len(jobs),
        "results": jobs,
    }
    path = f"{base}.json"
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    print(f"  [OK] JSON saved -> {path}")
    return path


def export_csv(jobs: list[dict], base: str) -> str:
    path = f"{base}.csv"
    fields = [
        "score",
        "group",
        "company",
        "title",
        "url",
        "snippet",
        "signals",
        "source_kind",
        "query",
        "search_backend",
        "context_source",
    ]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        rows = []
        for job in jobs:
            row = dict(job)
            row["signals"] = ", ".join(job.get("signals", []))
            rows.append(row)
        writer.writerows(rows)
    print(f"  [OK] CSV saved  -> {path}")
    return path


def export_html(
    jobs: list[dict],
    base: str,
    diagnostics: dict,
    thresholds: dict,
    queries: int,
    boards: int,
    search_date: str,
) -> str:
    path = f"{base}.html"
    high = sum(1 for job in jobs if job["score"] >= thresholds["high"])

    rows = []
    for index, job in enumerate(jobs, 1):
        score = job["score"]
        score_class = "score-high" if score >= thresholds["high"] else ("score-med" if score >= thresholds["medium"] else "score-low")
        safe_url = escape(job["url"], quote=True)
        safe_title = escape(job.get("title") or job["url"])
        short_url = escape(re.sub(r"https?://(?:www\.)?", "", job["url"])[:55])
        rows.append(
            "<tr>"
            f"<td>{index}</td>"
            f"<td><a href=\"{safe_url}\" target=\"_blank\">{safe_title}</a></td>"
            f"<td>{escape(job.get('company', ''))}</td>"
            f"<td><a href=\"{safe_url}\" target=\"_blank\">{short_url}</a></td>"
            f"<td><span class='group g-{job['group']}'>{job['group']}</span></td>"
            f"<td><span class='{score_class}'>{score}</span></td>"
            f"<td>{escape(', '.join(job.get('signals', [])))}</td>"
            f"<td>{escape(job.get('snippet', '')[:180])}</td>"
            "</tr>"
        )

    html = HTML_TEMPLATE.format(
        date=search_date,
        target=TARGET_LABEL,
        queries=queries,
        total=len(jobs),
        high=high,
        boards=boards,
        enriched=diagnostics["context_enriched_results"],
        diagnostics_note=escape(diagnostics["note"]),
        rows="\n".join(rows),
    )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(html)
    print(f"  [OK] HTML saved -> {path}")
    return path


async def execute_search(
    export_format: str = "html",
    verbose: bool = True,
    output_dir: str | Path | None = None,
) -> dict:
    export_format = validate_export_format(export_format)
    run_started = datetime.now()
    search_date = format_run_date(run_started)
    date_tag = format_run_tag(run_started)
    resolved_output_dir = resolve_output_dir(output_dir)

    scraper = WebScraper()
    queries = generate_queries()
    query_semaphore = asyncio.Semaphore(SEARCH_CONCURRENCY)
    searxng_available = await probe_searxng_available()

    print("=" * 88)
    print("ADR LIQUID / TANK CONTAINER DRIVER — IMPROVED SEARCH")
    print(f"Date      : {search_date}")
    print(f"Target    : {TARGET_LABEL}")
    print(f"Queries   : {len(queries)}")
    print(f"Concurrency: {SEARCH_CONCURRENCY}")
    print("=" * 88)

    search_tasks = [
        search_one(query_semaphore, group, query, searxng_available)
        for group, query in queries
    ]
    search_outputs = await asyncio.gather(*search_tasks, return_exceptions=True)

    jobs = []
    seen_urls = set()
    boards_hit = set()
    backend_counts = Counter()
    filtered_counts = Counter()

    for item in search_outputs:
        if isinstance(item, Exception):
            if verbose:
                print(f"  Search error: {item}")
            continue
        if not isinstance(item, dict):
            continue

        group = item["group"]
        query = item["query"]
        backend = item["backend"]
        backend_counts[backend] += 1

        if verbose:
            print(f"  [{group:<6}] {query[:72]}{'…' if len(query) > 72 else ''} -> {len(item['results'])} raw")

        for result in item["results"]:
            raw_url = result.get("url", "")
            url = canonicalize_url(raw_url)
            if not url or url in seen_urls:
                continue

            title = compact_text(result.get("title", ""))
            snippet = compact_text(result.get("snippet", ""))

            allowed, reason = is_result_allowed(group, url, title, snippet)
            if not allowed:
                filtered_counts[reason] += 1
                continue

            source_kind = classify_source_kind(group, url)
            score, signals = compute_score(title, snippet, "", url, source_kind)
            company = extract_company_name(title, snippet, url)

            jobs.append(
                {
                    "group": group,
                    "query": query,
                    "title": title,
                    "company": company,
                    "url": url,
                    "snippet": snippet,
                    "page_excerpt": "",
                    "score": score,
                    "signals": signals,
                    "source_kind": source_kind,
                    "search_backend": backend,
                    "context_source": "search_snippet" if snippet else "title_only",
                }
            )
            seen_urls.add(url)
            boards_hit.add(extract_domain(url))

    enriched_count = await enrich_contexts(scraper, jobs, verbose=verbose)
    jobs.sort(key=lambda item: item["score"], reverse=True)
    thresholds = determine_thresholds(jobs)
    diagnostics = build_diagnostics(jobs, backend_counts, filtered_counts, enriched_count, thresholds)

    high = sum(1 for job in jobs if job["score"] >= thresholds["high"])
    med = sum(1 for job in jobs if thresholds["medium"] <= job["score"] < thresholds["high"])
    low = sum(1 for job in jobs if job["score"] < thresholds["medium"])

    print("\n" + "=" * 88)
    print("SUMMARY")
    print("=" * 88)
    print(f"Total unique results : {len(jobs)}")
    print(f"High relevance       : {high}")
    print(f"Medium relevance     : {med}")
    print(f"Low relevance        : {low}")
    print(f"Unique sources       : {len(boards_hit)}")
    print(f"Filtered             : {sum(filtered_counts.values())}")
    print(f"Diagnostics          : {diagnostics['note']}")

    top_results = [job for job in jobs if job["score"] >= thresholds["high"]][:20]
    print("\nTop results:")
    for idx, job in enumerate(top_results, 1):
        print(f"  {idx:2}. {score_tag(job['score'], thresholds)} [{job['score']:3d}] {job['title']}")
        print(f"      {job['url']}")
        if job.get("company"):
            print(f"      Company: {job['company']}")
        if job.get("signals"):
            print(f"      Signals: {', '.join(job['signals'])}")
        if job.get("snippet"):
            print(f"      {job['snippet'][:180]}")
        print()

    base_name = str(resolved_output_dir / f"tank_adr_jobs_improved_{date_tag}")
    output_files = {
        "json": export_json(jobs, base_name, diagnostics, thresholds, len(queries), len(boards_hit), search_date)
    }
    if export_format in ("csv", "all"):
        output_files["csv"] = export_csv(jobs, base_name)
    if export_format in ("html", "all"):
        output_files["html"] = export_html(
            jobs,
            base_name,
            diagnostics,
            thresholds,
            len(queries),
            len(boards_hit),
            search_date,
        )

    summary = build_run_summary(
        jobs,
        diagnostics,
        thresholds,
        len(queries),
        len(boards_hit),
        output_files,
        search_date,
        export_format,
    )

    return {
        "jobs": jobs,
        "summary": summary,
        "diagnostics": diagnostics,
        "thresholds": thresholds,
    }


async def run_search(
    export_format: str = "html",
    verbose: bool = True,
    output_dir: str | Path | None = None,
) -> list[dict]:
    result = await execute_search(export_format=export_format, verbose=verbose, output_dir=output_dir)
    return result["jobs"]


async def run_search_with_summary(
    export_format: str = "html",
    verbose: bool = False,
    output_dir: str | Path | None = None,
) -> dict:
    result = await execute_search(export_format=export_format, verbose=verbose, output_dir=output_dir)
    return result["summary"]


def parse_args():
    parser = argparse.ArgumentParser(description="Improved ADR tanker driver search for Switzerland")
    parser.add_argument(
        "--export",
        choices=["json", "csv", "html", "all"],
        default="html",
        help="Export format (JSON is always written)",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress detailed per-query logging")
    return parser.parse_args()


if __name__ == "__main__":
    configure_stdio()
    args = parse_args()
    results = asyncio.run(run_search(export_format=args.export, verbose=not args.quiet))
    sys.exit(0 if results else 1)
