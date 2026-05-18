#!/usr/bin/env python
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         ADR LIQUID / TANK CONTAINER DRIVER — EXHAUSTIVE JOB SEARCH         ║
║         Switzerland (CH) + Social Media                                     ║
║         Candidate Profile: 10 yrs exp · ADR · CE · Tachograph Card          ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import argparse
import asyncio
import csv
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import unquote, urlparse

if sys.platform == "win32":
    import io

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))
os.environ["AUTH_ENFORCE"] = "false"

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
        "ADR Class 2  – Gases",
        "ADR Class 3  – Flammable liquids",
        "ADR Class 6  – Toxic & infectious substances",
        "ADR Class 8  – Corrosives",
        "ADR Class 9  – Miscellaneous",
        "ADR Tanker Certificate (liquid bulk)",
    ],
    "documents_valid": [
        "CE Driving Licence           — valid, recently renewed",
        "ADR Certificate              — valid, recently renewed",
        "Digital Tachograph Card      — valid, recently renewed",
        "CPC / Driver Qualification   — valid, recently renewed",
        "Medical Certificate          — valid",
        "Tank Container Certificate   — valid",
    ],
    "languages": "English (preferred), Romanian (native)",
    "availability": "immediate",
    "work_permit": "EU citizen — no sponsorship required",
    "target_locations": [
        "Switzerland",
        "Zurich",
        "Basel",
        "Geneva",
        "Bern",
        "Lausanne",
        "Zug",
        "Lucerne",
        "St. Gallen",
        "Aarau",
    ],
}

TODAY = datetime.now().strftime("%Y-%m-%d")
DATE_TAG = datetime.now().strftime("%Y%m%d_%H%M")
OUTPUT_DIR = JOB_RESULTS_DIR
TARGET_LABEL = "Switzerland (CH) + Social Media"
HIGH_THRESHOLD_DEFAULT = 50
MEDIUM_THRESHOLD_DEFAULT = 20
CONTEXT_ENRICH_LIMIT = 24
CONTEXT_ENRICH_CONCURRENCY = 6

SEARCH_QUERIES = [
    ("CH-DE", "site:jobs.ch Tankfahrer ADR Flüssigkeiten Schweiz"),
    ("CH-DE", "site:jobs.ch ADR Gefahrgut Tankzug Fahrer 2026"),
    ("CH-DE", "site:jobup.ch Chauffeur citerne ADR Suisse"),
    ("CH-DE", "site:stepstone.ch Tankfahrer ADR Gefahrguttransport"),
    ("CH-DE", "site:jobscout24.ch LKW Fahrer ADR Tank"),
    ("CH-DE", "site:jobagent.ch ADR Tankfahrer"),
    ("CH-DE", "site:monster.ch Tankfahrer ADR Flüssigkeiten"),
    ("CH-DE", "site:careerjet.ch ADR Tanker driver Switzerland"),
    ("CH-DE", "site:jooble.org ADR Tankfahrer Schweiz"),
    ("CH-DE", "site:jobs.ch Chauffeur Citerne ADR CE Suisse"),
    ("CH-DE", "site:ostjob.ch Fahrer ADR Gefahrgut"),
    ("CH-DE", "site:job-room.ch Tankfahrer ADR"),
    ("CH-DE", "site:regionaljob.ch ADR Fahrer Tank"),
    ("CH-DE", "site:swissjobs.ch ADR Tankfahrer CE"),
    ("CH-DE", "ADR Tankfahrer Flüssigkeiten Stelle Schweiz 2026"),
    ("CH-DE", "Gefahrguttransport Tankzug Fahrer Stelle Zürich Basel Bern 2026"),
    ("CH-DE", "ADR Tankzug Fahrer Arbeit Aargau Zug Luzern"),
    ("CH-FR", "site:jobup.ch chauffeur citerne ADR liquide Suisse"),
    ("CH-FR", "site:indeed.ch chauffeur ADR citerne liquide Suisse"),
    ("CH-FR", "site:optioncarriere.ch chauffeur ADR citerne"),
    ("CH-FR", "chauffeur ADR citerne liquides emploi Suisse Romande 2026"),
    ("CH-FR", "chauffeur poids lourd ADR matières dangereuses Genève Lausanne"),
    ("CH-FR", "chauffeur citerne ADR classe 3 emploi Suisse"),
    ("CH-IT", "autista cisterna ADR Svizzera Ticino lavoro 2026"),
    ("CH-IT", "camionista ADR liquidi pericolosi Canton Ticino"),
    ("CH-EN", "site:jobs.ch ADR tanker driver Switzerland English"),
    ("CH-EN", "site:indeed.ch ADR liquid tanker driver Switzerland 2026"),
    ("CH-EN", "tank container driver Switzerland 10 years experience ADR"),
    ("CH-EN", "ADR liquid bulk driver job Zurich Basel Geneva Switzerland 2026"),
    ("CH-EN", "tanker truck driver ADR chemicals Switzerland hiring now"),
    ("CH-EN", "dangerous goods driver ADR tank Switzerland vacancy 2026"),
    ("CH-EN", "HazMat tanker driver Switzerland CE license job offer"),
    ("CH-EN", "ADR class 3 flammable liquids driver Switzerland employment"),
    ("CH-EN", "ADR liquid tanker driver Switzerland 10 years full documents"),
    ("SOCIAL", "site:linkedin.com/jobs ADR tanker driver Switzerland"),
    ("SOCIAL", "site:linkedin.com ADR liquid tanker driver Switzerland 2026 hiring"),
    ("SOCIAL", "site:linkedin.com ADR Tankfahrer Schweiz Stelle 2026"),
    ("SOCIAL", "site:linkedin.com tank container driver Switzerland vacancy"),
    ("SOCIAL", "site:xing.com ADR Tankzug Fahrer Schweiz"),
    ("SOCIAL", "site:facebook.com ADR tanker driver Switzerland job"),
    ("SOCIAL", "site:facebook.com groups ADR Fahrer Schweiz Stelle 2026"),
    ("SOCIAL", "site:twitter.com ADR tanker driver Switzerland job hiring"),
    ("SOCIAL", "site:reddit.com r/Switzerland ADR driver job"),
    ("SOCIAL", "LinkedIn ADR tanker driver Switzerland 10 years experience English"),
]

HIGH_RELEVANCE = [
    r"\badr\b",
    r"tanker",
    r"tank\s*container",
    r"liquid",
    r"flüssig",
    r"citerne",
    r"cistern",
    r"gefahrgut",
    r"hazmat",
    r"hazardous",
    r"\bce\b",
    r"klasse\s*3",
    r"class\s*3",
    r"bulk",
    r"chemical",
    r"fahrer",
    r"driver",
    r"chauffeur",
    r"autista",
]
LOW_RELEVANCE = [
    r"course",
    r"training",
    r"formation",
    r"ausbildung",
    r"hotel",
    r"tourism",
    r"touris",
    r"viator",
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
]
LOCATION_PATTERNS = [
    r"switzerland",
    r"schweiz",
    r"suisse",
    r"svizzera",
    r"\bch\b",
    r"zurich",
    r"basel",
    r"geneva",
    r"bern",
    r"lausanne",
    r"zug",
    r"lucerne",
    r"st\. gallen",
    r"aargau",
    r"ticino",
    r"vaud",
    r"fribourg",
    r"romont",
    r"yverdon",
    r"lugano",
    r"luganese",
]

COVER_LETTER_EN = """
══════════════════════════════════════════════════════════════════════════════
                      COVER LETTER / APPLICATION TEMPLATE
══════════════════════════════════════════════════════════════════════════════

Subject: Application — ADR Liquid / Tank Container Driver

Dear Hiring Manager,

I am writing to express my strong interest in the ADR Liquid / Tank Container
Driver position at your company. With 10 years of professional experience in
hazardous goods transport, I bring a proven track record in liquid bulk and
chemical/petroleum tanker operations across European routes.

QUALIFICATIONS & DOCUMENTS (all valid, recently renewed):
  ✔  Category CE Driving Licence
  ✔  ADR Certificate — Classes 2, 3, 6, 8, 9 (including Tanker endorsement)
  ✔  Digital Tachograph / Driver Card — valid
  ✔  CPC / Driver Qualification Card — valid
  ✔  Medical Certificate — valid
  ✔  Tank Container Certificate — valid

PROFESSIONAL SUMMARY:
  • 10 years of tank / cistern truck driving experience
  • Extensive ADR compliance knowledge — pre/post-trip checks, loading/unloading
    procedures, emergency protocols, documentation
  • Clean driving record — no accidents, no infringements
  • Experienced with both rigid tankers and articulated tank semi-trailers
  • Familiar with Swiss traffic regulations and cross-border EU transport
  • Fluent English (professional); available for immediate start

I am an EU citizen and require no visa sponsorship.

I would welcome the opportunity to discuss how my experience and qualifications
can contribute to your operations in Switzerland.

Kind regards,
[YOUR NAME]
[PHONE] | [EMAIL] | [LOCATION]

══════════════════════════════════════════════════════════════════════════════
"""

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang=\"en\">
<head>
<meta charset=\"UTF-8\">
<title>ADR Tank Driver Jobs — {date}</title>
<style>
  body{{font-family:Arial,sans-serif;max-width:1200px;margin:0 auto;padding:20px;background:#f5f5f5}}
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
  .g-CH-DE{{background:#1565c0}} .g-CH-FR{{background:#2e7d32}} .g-CH-IT{{background:#6a1b9a}}
  .g-CH-EN{{background:#00838f}} .g-SOCIAL{{background:#ad1457}}
  .note{{background:#fff8e1;border-left:4px solid #fb8c00;padding:14px 16px;border-radius:8px;margin:18px 0;color:#4e342e}}
  .cover{{white-space:pre-wrap;font-family:monospace;background:#fff;padding:20px;border-radius:8px;border:1px solid #ccc;font-size:.85em;margin-top:30px}}
  @media(max-width:600px){{th:nth-child(3),th:nth-child(4),td:nth-child(3),td:nth-child(4){{display:none}}}}
</style>
</head>
<body>
<h1>🚚 ADR Liquid / Tank Container Driver — Job Search Report</h1>
<p><strong>Date:</strong> {date} &nbsp;|&nbsp;
   <strong>Target:</strong> Switzerland (CH) + Social Media &nbsp;|&nbsp;
   <strong>Language:</strong> English preferred</p>
<div class=\"stats\">
  <div class=\"stat\"><div class=\"num\">{total}</div><div class=\"lbl\">Total results</div></div>
  <div class=\"stat\"><div class=\"num\">{high}</div><div class=\"lbl\">High relevance ({high_label})</div></div>
  <div class=\"stat\"><div class=\"num\">{queries}</div><div class=\"lbl\">Queries executed</div></div>
  <div class=\"stat\"><div class=\"num\">{boards}</div><div class=\"lbl\">Boards / sources</div></div>
</div>
<div class=\"note\"><strong>Search diagnostics:</strong> {diagnostics_note}</div>
<h2>Results</h2>
<table>
<thead><tr><th>#</th><th>Title</th><th>URL</th><th>Source Group</th><th>Score</th><th>Snippet</th></tr></thead>
<tbody>
{rows}
</tbody>
</table>
<h2>Application Cover Letter (English)</h2>
<div class=\"cover\">{cover}</div>
</body></html>
"""


def _pattern_hits(patterns, text: str) -> int:
    if not text:
        return 0
    return sum(1 for pattern in patterns if re.search(pattern, text, re.I))


def _compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _extract_domain(url: str) -> str:
    domain = urlparse(url).netloc.lower().strip()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def _domain_matches(domain: str, candidates) -> bool:
    return any(domain == candidate or domain.endswith(f".{candidate}") for candidate in candidates)


def _is_social_result_url_allowed(url: str) -> bool:
    return any(re.search(pattern, url, re.I) for pattern in SOCIAL_RESULT_URL_PATTERNS)


def clean_url(url: str) -> str:
    if "duckduckgo.com/l/?uddg=" in url:
        match = re.search(r"uddg=([^&]+)", url)
        if match:
            return unquote(match.group(1))
    return url


def is_result_allowed(group: str, url: str, title: str, snippet: str) -> tuple[bool, str]:
    domain = _extract_domain(url)
    combined = _compact_text(f"{title} {snippet}")
    combined_with_url = _compact_text(f"{title} {snippet} {url}")

    if not domain:
        return False, "invalid-domain"
    if _domain_matches(domain, BLOCKED_RESULT_DOMAINS):
        return False, "blocked-domain"
    if any(re.search(pattern, combined, re.I) for pattern in BLOCKED_RESULT_TITLE_PATTERNS):
        return False, "blocked-title"

    if group == "SOCIAL":
        if not _domain_matches(domain, SOCIAL_RESULT_DOMAINS):
            return False, "non-social-domain"
        if not _is_social_result_url_allowed(url):
            return False, "social-path"
        if _pattern_hits(LOCATION_PATTERNS, combined_with_url) == 0:
            return False, "non-target-location"
        return True, ""

    if domain.endswith(CH_RESULT_DOMAIN_SUFFIXES) or _domain_matches(domain, CH_RESULT_EXTRA_DOMAINS):
        return True, ""

    return False, "outside-target-domain"


def relevance_score(title: str, snippet: str) -> int:
    title_text = _compact_text(title)
    snippet_text = _compact_text(snippet)
    combined = f"{title_text} {snippet_text}".strip()

    title_hits = _pattern_hits(HIGH_RELEVANCE, title_text)
    snippet_hits = _pattern_hits(HIGH_RELEVANCE, snippet_text)
    job_hits = _pattern_hits(JOB_SIGNAL_PATTERNS, combined)
    location_hits = _pattern_hits(LOCATION_PATTERNS, combined)

    score = (title_hits * 12) + (snippet_hits * 8)
    score += min(job_hits * 4, 8)
    score += min(location_hits * 4, 8)

    if title_hits >= 3 and job_hits:
        score += 6
    if title_hits >= 2 and location_hits:
        score += 4

    for pattern in LOW_RELEVANCE:
        if re.search(pattern, combined, re.I):
            score -= 25

    if not snippet_text and title_hits == 0:
        score -= 10

    return max(0, min(score, 100))


def _build_context_snippet(page: dict) -> str:
    parts = []
    description = _compact_text(page.get("description", ""))
    body = _compact_text(page.get("body", ""))

    if description:
        parts.append(description)

    if body:
        for sentence in re.split(r"(?<=[.!?])\s+", body):
            sentence = sentence.strip()
            if len(sentence) < 40:
                continue
            parts.append(sentence)
            if len(" ".join(parts)) >= 280:
                break

    return " ".join(parts)[:280].strip()


def determine_relevance_thresholds(jobs: list) -> dict:
    if not jobs:
        return {"high": HIGH_THRESHOLD_DEFAULT, "medium": MEDIUM_THRESHOLD_DEFAULT}

    snippet_coverage = sum(1 for job in jobs if _compact_text(job.get("snippet", ""))) / len(jobs)
    high_threshold = HIGH_THRESHOLD_DEFAULT
    if snippet_coverage < 0.25:
        high_threshold = 40
    elif snippet_coverage < 0.50:
        high_threshold = 45

    return {"high": high_threshold, "medium": MEDIUM_THRESHOLD_DEFAULT}


def score_tag(score: int, thresholds: dict) -> str:
    if score >= thresholds["high"]:
        return "[H]"
    if score >= thresholds["medium"]:
        return "[M]"
    return "[L]"


def build_search_diagnostics(jobs: list, backend_counts: Counter, thresholds: dict, enriched_count: int, filtered_counts: Counter) -> dict:
    total = max(len(jobs), 1)
    snippet_coverage = sum(1 for job in jobs if _compact_text(job.get("snippet", ""))) / total
    filtered_total = sum(filtered_counts.values())

    note_parts = []
    if backend_counts.get("searxng", 0) == 0 and backend_counts.get("duckduckgo_html", 0) > 0:
        note_parts.append("SearXNG was unavailable, so the run used DuckDuckGo HTML fallback.")
    if filtered_total:
        note_parts.append(f"{filtered_total} results were removed by source filters before scoring.")
    if enriched_count:
        note_parts.append(f"{enriched_count} low-context results were enriched from primary pages.")
    if thresholds["high"] < HIGH_THRESHOLD_DEFAULT:
        note_parts.append(f"High relevance was adapted to >= {thresholds['high']} because snippet coverage was {snippet_coverage:.0%}.")
    else:
        note_parts.append(f"Snippet coverage was {snippet_coverage:.0%}; high relevance remained >= {thresholds['high']}.")

    return {
        "backend_counts": dict(backend_counts),
        "filtered_counts": dict(filtered_counts),
        "snippet_coverage": snippet_coverage,
        "context_enriched_results": enriched_count,
        "note": " ".join(note_parts),
    }


async def enrich_job_contexts(scraper: WebScraper, jobs: list, verbose: bool = True) -> int:
    candidates = sorted(
        [job for job in jobs if job.get("score", 0) >= 18 and len(_compact_text(job.get("snippet", ""))) < 80],
        key=lambda item: item["score"],
        reverse=True,
    )[:CONTEXT_ENRICH_LIMIT]

    if not candidates:
        return 0

    semaphore = asyncio.Semaphore(CONTEXT_ENRICH_CONCURRENCY)

    async def enrich(job: dict) -> bool:
        async with semaphore:
            page = await scraper.fetch(job["url"])

        if page.get("error"):
            return False

        enriched_snippet = _build_context_snippet(page)
        if len(enriched_snippet) <= len(_compact_text(job.get("snippet", ""))):
            return False

        page_title = _compact_text(page.get("title", ""))
        if page_title and len(_compact_text(job.get("title", ""))) < 12:
            job["title"] = page_title

        job["snippet"] = enriched_snippet
        job["context_source"] = "page_fetch"
        job["score"] = relevance_score(job["title"], job["snippet"])
        return True

    enriched_count = sum(1 for updated in await asyncio.gather(*(enrich(job) for job in candidates)) if updated)
    if verbose and enriched_count:
        print(f"\n  Context enrichment   : {enriched_count} results updated from primary pages")
    return enriched_count


async def run_search(export_format: str = "json", verbose: bool = True) -> list:
    scraper = WebScraper()
    all_jobs = []
    seen_urls = set()
    boards_hit = set()
    backend_counts = Counter()
    filtered_counts = Counter()
    group_counts = {}
    width = 80

    print("=" * width)
    print("  ADR LIQUID / TANK CONTAINER DRIVER - EXHAUSTIVE JOB SEARCH")
    print(f"  Date      : {TODAY}")
    print(f"  Target    : {TARGET_LABEL}")
    print(f"  Languages : English (primary) | DE | FR | IT")
    print(f"  Queries   : {len(SEARCH_QUERIES)}")
    print("=" * width)
    print()

    labels = {
        "CH-DE": "[CH]  Swiss Boards (German)",
        "CH-FR": "[CH]  Swiss Boards (French)",
        "CH-IT": "[CH]  Swiss Boards (Italian)",
        "CH-EN": "[CH]  Swiss Boards (English)",
        "SOCIAL": "[SM]  Social Media / Networks",
    }
    current_group = None

    for index, (group, query) in enumerate(SEARCH_QUERIES, 1):
        if group != current_group:
            current_group = group
            print(f"\n{'-' * width}")
            print(f"  {labels.get(group, group)}")
            print(f"{'-' * width}")

        if verbose:
            short_query = query[:70] + "…" if len(query) > 70 else query
            print(f"  [{index:03d}/{len(SEARCH_QUERIES)}] {short_query}")

        try:
            results = await scraper.search(query, num_results=10)
            search_backend = getattr(scraper, "last_search_backend", "unknown")
            backend_counts[search_backend] += 1
            added_in_query = 0

            for result in results:
                url = clean_url(result.get("url", ""))
                if not url:
                    continue

                title = result.get("title", "")
                snippet = _compact_text(result.get("snippet", ""))
                allowed, reason = is_result_allowed(group, url, title, snippet)
                if not allowed:
                    filtered_counts[reason] += 1
                    continue
                if url in seen_urls:
                    continue

                seen_urls.add(url)
                score = relevance_score(title, snippet)
                all_jobs.append(
                    {
                        "group": group,
                        "query": query,
                        "title": title,
                        "url": url,
                        "snippet": snippet,
                        "score": score,
                        "search_backend": search_backend,
                        "context_source": "search_snippet" if snippet else "title_only",
                    }
                )
                added_in_query += 1
                boards_hit.add(_extract_domain(url))
                group_counts[group] = group_counts.get(group, 0) + 1

            if verbose and added_in_query > 0:
                top = sorted(all_jobs[-added_in_query:], key=lambda item: item["score"], reverse=True)
                preview = {"high": HIGH_THRESHOLD_DEFAULT, "medium": MEDIUM_THRESHOLD_DEFAULT}
                for job in top[:3]:
                    print(f"       {score_tag(job['score'], preview)} [{job['score']:3d}] {job['title'][:65]}")
        except Exception as exc:
            if verbose:
                print(f"       ✗ Error: {exc}")

        await asyncio.sleep(0.4)

    enriched_count = await enrich_job_contexts(scraper, all_jobs, verbose=verbose)
    all_jobs.sort(key=lambda item: item["score"], reverse=True)
    thresholds = determine_relevance_thresholds(all_jobs)
    diagnostics = build_search_diagnostics(all_jobs, backend_counts, thresholds, enriched_count, filtered_counts)

    high = sum(1 for job in all_jobs if job["score"] >= thresholds["high"])
    med = sum(1 for job in all_jobs if thresholds["medium"] <= job["score"] < thresholds["high"])
    low = sum(1 for job in all_jobs if job["score"] < thresholds["medium"])
    backend_summary = ", ".join(f"{name}={count}" for name, count in sorted(backend_counts.items())) or "n/a"

    print(f"\n{'=' * width}")
    print("  SEARCH COMPLETE - RESULTS SUMMARY")
    print(f"{'=' * width}")
    print(f"  Total unique results : {len(all_jobs)}")
    print(f"  High relevance (>={thresholds['high']}): {high}  [H]")
    print(f"  Medium relevance     : {med}  [M]")
    print(f"  Low / noise          : {low}  [L]")
    print(f"  Boards / sources     : {len(boards_hit)}")
    print(f"  Queries executed     : {len(SEARCH_QUERIES)}")
    print(f"  Search backends      : {backend_summary}")
    print(f"  Filtered out         : {sum(filtered_counts.values())}")
    print(f"  Snippet coverage     : {diagnostics['snippet_coverage']:.0%}")
    print(f"  Context enriched     : {diagnostics['context_enriched_results']}")
    print()
    print("  Results by group:")
    for group, count in sorted(group_counts.items(), key=lambda item: -item[1]):
        print(f"    {group:<10} {count:>3} results")
    print()
    print(f"  Note: {diagnostics['note']}")

    print(f"\n{'-' * width}")
    print(f"  TOP RESULTS (score >= {thresholds['high']}):")
    print(f"{'-' * width}")
    top20 = [job for job in all_jobs if job["score"] >= thresholds["high"]][:20]
    if top20:
        for index, job in enumerate(top20, 1):
            print(f"  {index:2}. [{job['score']:3d}] {job['title']}")
            print(f"       {job['url'][:90]}")
            if job["snippet"]:
                print(f"       → {job['snippet'][:120]}")
            print()
    else:
        print("  (no high-relevance results after scoring + context enrichment)")

    print(COVER_LETTER_EN)

    base_name = str(OUTPUT_DIR / f"tank_adr_jobs_{DATE_TAG}")
    _export_json(all_jobs, base_name, len(SEARCH_QUERIES), len(boards_hit), thresholds, diagnostics)
    if export_format in ("csv", "all"):
        _export_csv(all_jobs, base_name)
    if export_format in ("html", "all"):
        _export_html(all_jobs, base_name, len(SEARCH_QUERIES), len(boards_hit), thresholds, diagnostics)

    return all_jobs


def _export_json(jobs: list, base: str, queries: int, boards: int, thresholds: dict, diagnostics: dict):
    payload = {
        "search_date": TODAY,
        "profile": CANDIDATE,
        "total_results": len(jobs),
        "high_relevance": sum(1 for job in jobs if job["score"] >= thresholds["high"]),
        "relevance_thresholds": thresholds,
        "queries_executed": queries,
        "boards_sources": boards,
        "diagnostics": diagnostics,
        "results": jobs,
    }
    path = f"{base}.json"
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    print(f"  [OK] JSON saved  -> {path}")


def _export_csv(jobs: list, base: str):
    path = f"{base}.csv"
    fields = ["score", "group", "title", "url", "snippet", "query", "search_backend", "context_source"]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(jobs)
    print(f"  [OK] CSV  saved  -> {path}")


def _export_html(jobs: list, base: str, queries: int, boards: int, thresholds: dict, diagnostics: dict):
    path = f"{base}.html"
    high = sum(1 for job in jobs if job["score"] >= thresholds["high"])
    rows = []

    for index, job in enumerate(jobs, 1):
        score = job["score"]
        score_class = "score-high" if score >= thresholds["high"] else ("score-med" if score >= thresholds["medium"] else "score-low")
        safe_url = escape(job["url"], quote=True)
        safe_title = escape(job["title"] or job["url"][:60])
        short_url = escape(re.sub(r"https?://(?:www\.)?", "", job["url"])[:55])
        rows.append(
            "<tr>"
            f"<td>{index}</td>"
            f"<td><a href=\"{safe_url}\" target=\"_blank\">{safe_title}</a></td>"
            f"<td><a href=\"{safe_url}\" target=\"_blank\">{short_url}</a></td>"
            f"<td><span class='group g-{job['group']}'>{job['group']}</span></td>"
            f"<td><span class='{score_class}'>{score}</span></td>"
            f"<td>{escape(job['snippet'][:150])}</td>"
            "</tr>"
        )

    backend_summary = ", ".join(f"{name}={count}" for name, count in sorted(diagnostics["backend_counts"].items())) or "n/a"
    diagnostics_note = escape(f"{diagnostics['note']} Backends: {backend_summary}.")

    html = HTML_TEMPLATE.format(
        date=TODAY,
        total=len(jobs),
        high=high,
        high_label=f">={thresholds['high']}",
        queries=queries,
        boards=boards,
        diagnostics_note=diagnostics_note,
        rows="\n".join(rows),
        cover=COVER_LETTER_EN.replace("<", "&lt;").replace(">", "&gt;"),
    )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(html)
    print(f"  [OK] HTML saved  -> {path}")
    print(f"     Open in browser: file:///{Path(path).resolve().as_posix()}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="ADR Liquid/Tank Container driver job search — Switzerland + Social Media"
    )
    parser.add_argument(
        "--export",
        choices=["json", "csv", "html", "all"],
        default="html",
        help="Export format (default: html — also always saves JSON)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-query output",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    results = asyncio.run(run_search(export_format=args.export, verbose=not args.quiet))
    sys.exit(0 if results else 1)
