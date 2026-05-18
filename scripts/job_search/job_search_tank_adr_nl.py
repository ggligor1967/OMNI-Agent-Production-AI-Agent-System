#!/usr/bin/env python
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         ADR LIQUID / TANK CONTAINER DRIVER — EXHAUSTIVE JOB SEARCH         ║
║         Netherlands (NL) + Europe (EU) + Social Media                      ║
║         Candidate Profile: 10 yrs exp · ADR · CE · Tachograph Card          ║
╚══════════════════════════════════════════════════════════════════════════════╝

Usage:
    python job_search_tank_adr_nl.py
    python job_search_tank_adr_nl.py --lang en
    python job_search_tank_adr_nl.py --export html
    python job_search_tank_adr_nl.py --export all

Exports: JSON  (always)
         CSV   (--export csv  or --export all)
         HTML  (--export html or --export all)
"""
import asyncio
import json
import csv
import sys
import os
import re
import argparse
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

# Force UTF-8 output on Windows
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Add project root to path (2 levels up from scripts/job_search/)
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
os.environ["AUTH_ENFORCE"] = "false"

from config import CONFIG
from agent.tools import WebScraper
from job_search_config import JOB_RESULTS_DIR

# ─────────────────────────────────────────────────────────────────────────────
# CANDIDATE PROFILE  (used in application messages & cover-letter template)
# ─────────────────────────────────────────────────────────────────────────────
CANDIDATE = {
    "role"             : "ADR Liquid / Tank Container Driver",
    "experience_years" : 10,
    "license"          : "Category CE (truck + trailer)",
    "adr_certificates" : [
        "ADR Class 2  – Gases",
        "ADR Class 3  – Flammable liquids",
        "ADR Class 6  – Toxic & infectious substances",
        "ADR Class 8  – Corrosives",
        "ADR Class 9  – Miscellaneous",
        "ADR Tanker Certificate (liquid bulk)",
    ],
    "documents_valid"  : [
        "CE Driving Licence           — valid, recently renewed",
        "ADR Certificate              — valid, recently renewed",
        "Digital Tachograph Card      — valid, recently renewed",
        "CPC / Driver Qualification   — valid, recently renewed",
        "Medical Certificate          — valid",
        "Tank Container Certificate   — valid",
    ],
    "languages"        : "English (preferred), Romanian (native)",
    "availability"     : "immediate",
    "work_permit"      : "EU citizen — no sponsorship required",
    "target_locations" : ["Netherlands", "Rotterdam", "Amsterdam", "Utrecht",
                          "The Hague", "Eindhoven", "Groningen", "Breda",
                          "Tilburg", "Dordrecht", "Moerdijk", "Europoort",
                          "Pernis", "Botlek", "Vlaardingen"],
}

TODAY = datetime.now().strftime("%Y-%m-%d")
DATE_TAG = datetime.now().strftime("%Y%m%d_%H%M")

OUTPUT_DIR = JOB_RESULTS_DIR

# ─────────────────────────────────────────────────────────────────────────────
# SEARCH QUERY MATRIX
# All queries are purposely varied to avoid dedup at engine level.
# Groups: NL-specific · EU-general · Transport-boards · Social · Company direct
# ─────────────────────────────────────────────────────────────────────────────
SEARCH_QUERIES = [

    # ── DUTCH JOB BOARDS (Dutch) ───────────────────────────────────────────
    ("NL-NL", "site:indeed.nl tankwagen chauffeur ADR vacature"),
    ("NL-NL", "site:indeed.nl ADR tankauto chauffeur gevaarlijke stoffen"),
    ("NL-NL", "site:werkenbijrandstad.nl ADR tankwagen chauffeur"),
    ("NL-NL", "site:nationalevacaturebank.nl ADR tankwagen chauffeur"),
    ("NL-NL", "site:nationalevacaturebank.nl chauffeur gevaarlijke stoffen tank"),
    ("NL-NL", "site:monsterboard.nl ADR tankwagen chauffeur vacature"),
    ("NL-NL", "site:jobbird.com ADR tankwagen chauffeur Nederland"),
    ("NL-NL", "site:werk.nl ADR tankwagen chauffeur vacature"),
    ("NL-NL", "site:tempo-team.nl ADR chauffeur tankwagen"),
    ("NL-NL", "site:unique.nl ADR tankwagen chauffeur"),
    ("NL-NL", "site:manpower.nl ADR chauffeur tanktransport"),
    ("NL-NL", "site:hays.nl ADR tankwagen chauffeur logistiek"),
    ("NL-NL", "site:driessen.nl ADR chauffeur tankwagen"),
    ("NL-NL", "site:youngcapital.nl ADR tankwagen chauffeur"),
    ("NL-NL", "site:intermediair.nl ADR chauffeur tankwagen"),
    ("NL-NL", "ADR tankwagen chauffeur vacature Nederland 2026"),
    ("NL-NL", "chauffeur gevaarlijke stoffen tankwagen Rotterdam Amsterdam 2026"),
    ("NL-NL", "ADR tankauto chauffeur vloeibare stoffen vacature Nederland"),
    ("NL-NL", "CE rijbewijs ADR tankwagen chauffeur baan Nederland"),
    ("NL-NL", "tankcontainer chauffeur ADR Rotterdam Europoort Botlek"),
    ("NL-NL", "ADR chauffeur klasse 3 brandbare vloeistoffen Nederland"),
    ("NL-NL", "bulktransport chauffeur ADR vloeistoffen vacature"),
    ("NL-NL", "chauffeur chemisch transport ADR tankwagen Nederland"),
    ("NL-NL", "vacature ADR chauffeur tankoplegger Nederland"),

    # ── DUTCH JOB BOARDS (English) ────────────────────────────────────────
    ("NL-EN", "site:indeed.nl ADR tanker driver Netherlands English"),
    ("NL-EN", "site:indeed.nl ADR liquid tanker driver Netherlands 2026"),
    ("NL-EN", "site:linkedin.com ADR tanker driver Netherlands CE licence"),
    ("NL-EN", "tank container driver Netherlands 10 years experience ADR"),
    ("NL-EN", "ADR liquid bulk driver job Rotterdam Amsterdam Netherlands 2026"),
    ("NL-EN", "tanker truck driver ADR chemicals Netherlands hiring now"),
    ("NL-EN", "dangerous goods driver ADR tank Netherlands vacancy 2026"),
    ("NL-EN", "HazMat tanker driver Netherlands CE license job offer"),
    ("NL-EN", "ADR class 3 flammable liquids driver Netherlands employment"),
    ("NL-EN", "ADR liquid tanker driver Netherlands 10 years full documents"),
    ("NL-EN", "ADR tanker driver Rotterdam port Europoort vacancy 2026"),
    ("NL-EN", "chemical tanker driver Netherlands English speaking vacancy"),
    ("NL-EN", "petroleum tanker driver Netherlands ADR CE immediate start"),
    ("NL-EN", "bulk liquid transport driver Netherlands ADR certified"),
    ("NL-EN", "tank container driver Moerdijk Botlek Pernis Netherlands ADR"),

    # ── EUROPEAN JOB BOARDS (EU-WIDE) ────────────────────────────────────
    ("EU-EN", "site:eures.europa.eu ADR tanker driver Netherlands"),
    ("EU-EN", "site:eures.europa.eu tankwagen chauffeur ADR Nederland"),
    ("EU-EN", "site:eurojobs.com ADR tanker driver Netherlands CE"),
    ("EU-EN", "site:jobs.eu ADR tanker driver Netherlands"),
    ("EU-EN", "site:eu.indeed.com ADR liquid tanker driver Netherlands"),
    ("EU-EN", "site:monster.de ADR Tankfahrer Niederlande 2026"),
    ("EU-EN", "site:stepstone.de ADR Tankfahrer Niederlande"),
    ("EU-EN", "site:totaljobs.com ADR tanker driver Netherlands"),
    ("EU-EN", "site:reed.co.uk ADR tanker driver Netherlands"),
    ("EU-EN", "site:cv-library.co.uk ADR tanker driver Netherlands Holland"),
    ("EU-EN", "site:glassdoor.com ADR tanker driver Netherlands jobs"),
    ("EU-EN", "site:glassdoor.nl ADR tanker driver vacature"),
    ("EU-EN", "site:ziprecruiter.com ADR tanker driver Netherlands"),
    ("EU-EN", "European ADR tank container driver vacancy Netherlands 2026"),
    ("EU-EN", "ADR liquid tanker driver job offer Netherlands EU citizen"),

    # ── TRANSPORT & LOGISTICS SPECIFIC BOARDS ────────────────────────────
    ("TRANS", "site:truckjobs.eu ADR tank driver Netherlands"),
    ("TRANS", "site:jobs-in-logistics.com ADR tanker driver Netherlands"),
    ("TRANS", "site:transport.jobs ADR tanker driver Netherlands CE"),
    ("TRANS", "site:logistiekbanen.nl ADR chauffeur tankwagen"),
    ("TRANS", "site:transportbanen.nl ADR tankwagen chauffeur vacature"),
    ("TRANS", "site:logistiekjobs.nl ADR chauffeur tank vacature"),
    ("TRANS", "site:jobsintransport.com ADR tanker driver Netherlands 2026"),
    ("TRANS", "site:freight-jobs.com ADR liquid tanker driver Netherlands"),
    ("TRANS", "site:driversjobs.eu ADR tank Netherlands hiring"),
    ("TRANS", "site:vrachtwagenchauffeur.nl ADR tankwagen vacature"),
    ("TRANS", "tank container driver Netherlands logistics job board 2026"),
    ("TRANS", "bulk liquid tanker driver Netherlands hazmat logistics"),
    ("TRANS", "chemical tanker driver Nederland vacature ADR CE 2026"),
    ("TRANS", "petrol tanker driver Netherlands ADR 10 years experience"),
    ("TRANS", "road tanker driver ADR Netherlands immediate vacancy"),
    ("TRANS", "cistern truck driver Netherlands ADR CE full package"),
    ("TRANS", "ADR chauffeur tankwagen Randstad regio vacature"),
    ("TRANS", "intermodal tank container driver Netherlands Rotterdam"),

    # ── SOCIAL MEDIA / PROFESSIONAL NETWORKS ─────────────────────────────
    ("SOCIAL", "site:linkedin.com/jobs ADR tanker driver Netherlands"),
    ("SOCIAL", "site:linkedin.com ADR liquid tanker driver Netherlands 2026 hiring"),
    ("SOCIAL", "site:linkedin.com ADR tankwagen chauffeur Nederland vacature 2026"),
    ("SOCIAL", "site:linkedin.com tank container driver Netherlands vacancy"),
    ("SOCIAL", "site:linkedin.com ADR chauffeur Rotterdam tankwagen"),
    ("SOCIAL", "site:facebook.com ADR tanker driver Netherlands job"),
    ("SOCIAL", "site:facebook.com groups ADR chauffeur Nederland vacature 2026"),
    ("SOCIAL", "site:twitter.com ADR tanker driver Netherlands job hiring"),
    ("SOCIAL", "site:reddit.com r/Netherlands ADR driver job"),
    ("SOCIAL", "LinkedIn ADR tanker driver Netherlands 10 years experience English"),

    # ── DIRECT COMPANY SEARCHES (major NL/EU logistics & chemical transport) ──
    ("COMPANY", "site:kuehne-nagel.com driver jobs Netherlands ADR"),
    ("COMPANY", "site:dhl.com ADR tanker driver Netherlands careers"),
    ("COMPANY", "Bertschi logistics ADR tanker driver Netherlands job"),
    ("COMPANY", "Stolt-Nielsen tank container driver Netherlands vacancy"),
    ("COMPANY", "HOYER Group Netherlands ADR tanker driver job"),
    ("COMPANY", "Den Hartogh Netherlands ADR liquid driver vacancy"),
    ("COMPANY", "Den Hartogh Logistics ADR tankwagen chauffeur vacature"),
    ("COMPANY", "Suttons International ADR tanker driver Netherlands hiring"),
    ("COMPANY", "Trifleet Netherlands tank container driver ADR"),
    ("COMPANY", "Van den Bosch Transporten ADR tankwagen chauffeur vacature"),
    ("COMPANY", "Nijhof-Wassink ADR chauffeur tankwagen Nederland"),
    ("COMPANY", "Vos Logistics ADR tankwagen chauffeur vacature"),
    ("COMPANY", "Simon Loos ADR chauffeur tankwagen"),
    ("COMPANY", "Broekman Logistics ADR tank driver Rotterdam"),
    ("COMPANY", "Vopak Netherlands ADR tanker driver terminal operator"),
    ("COMPANY", "Koole Tanktransport ADR chauffeur vacature"),
    ("COMPANY", "De Rijke Group ADR tankwagen chauffeur Rotterdam"),
    ("COMPANY", "Interbulk Netherlands ADR tank container driver"),
    ("COMPANY", "Vervaeke ADR tankwagen chauffeur Nederland vacature"),
    ("COMPANY", "Mammoet transport Netherlands ADR driver"),
    ("COMPANY", "hazardous goods transport company Netherlands ADR driver hiring 2026"),
    ("COMPANY", "chemical logistics company Netherlands ADR tanker driver vacancy"),
    ("COMPANY", "petrochemical transport Rotterdam ADR tank driver hiring"),

    # ── AGGREGATORS & META-SEARCH ─────────────────────────────────────────
    ("META", "ADR tanker driver Netherlands jobs 2026 -courses -training"),
    ("META", "tankwagen chauffeur ADR Nederland vacature maart 2026"),
    ("META", "ADR tankwagen chauffeur CE rijbewijs Nederland direct beschikbaar"),
    ("META", "ADR liquid tanker driver Netherlands immediate start English"),
    ("META", "dangerous goods tanker driver Netherlands CE card ADR 2026"),
    ("META", "tank truck driver Netherlands chemical transport ADR experience"),
    ("META", "ADR tanker driver Holland Rotterdam port area vacancy 2026"),
    ("META", "ADR gevaarlijke stoffen chauffeur tankwagen Nederland direct"),
]

# ─────────────────────────────────────────────────────────────────────────────
# RELEVANCE SCORING  (keyword match in title + snippet)
# ─────────────────────────────────────────────────────────────────────────────
HIGH_RELEVANCE = [
    r"\badr\b", r"tanker", r"tank\s*container", r"liquid", r"vloeistof",
    r"tankwagen", r"tankauto", r"gevaarlijke\s*stoffen", r"hazmat", r"hazardous",
    r"\bce\b", r"klasse\s*3", r"class\s*3", r"bulk", r"chemical",
    r"chauffeur", r"driver", r"bestuurder", r"rijbewijs",
    r"rotterdam", r"europoort", r"botlek", r"moerdijk", r"pernis",
]
LOW_RELEVANCE = [
    r"course", r"training", r"opleiding", r"cursus",
    r"hotel", r"tourism", r"touris", r"viator",
]

def relevance_score(title: str, snippet: str) -> int:
    """Return 0-100 relevance score."""
    text = (title + " " + snippet).lower()
    score = 0
    for pat in HIGH_RELEVANCE:
        if re.search(pat, text, re.I):
            score += 10
    for pat in LOW_RELEVANCE:
        if re.search(pat, text, re.I):
            score -= 20
    return max(0, min(score, 100))

def clean_url(url: str) -> str:
    """Strip DuckDuckGo redirect wrappers to get the real URL."""
    if "duckduckgo.com/l/?uddg=" in url:
        from urllib.parse import unquote
        m = re.search(r"uddg=([^&]+)", url)
        if m:
            return unquote(m.group(1))
    return url

# ─────────────────────────────────────────────────────────────────────────────
# COVER LETTER TEMPLATE (EN) — printed at the end
# ─────────────────────────────────────────────────────────────────────────────
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
  • Familiar with Dutch traffic regulations and cross-border EU transport
  • Experienced with Rotterdam port area (Europoort, Botlek, Pernis, Moerdijk)
  • Fluent English (professional); available for immediate start

I am an EU citizen and require no visa sponsorship.

I would welcome the opportunity to discuss how my experience and qualifications
can contribute to your operations in the Netherlands.

Kind regards,
[YOUR NAME]
[PHONE] | [EMAIL] | [LOCATION]

══════════════════════════════════════════════════════════════════════════════
"""

# ─────────────────────────────────────────────────────────────────────────────
# HTML REPORT TEMPLATE
# ─────────────────────────────────────────────────────────────────────────────
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>ADR Tank Driver Jobs Netherlands — {date}</title>
<style>
  body{{font-family:Arial,sans-serif;max-width:1200px;margin:0 auto;padding:20px;background:#f5f5f5}}
  h1{{color:#e65100;border-bottom:3px solid #e65100;padding-bottom:10px}}
  h2{{color:#bf360c}}
  .stats{{background:#fff3e0;padding:15px;border-radius:8px;margin:20px 0;display:flex;gap:30px;flex-wrap:wrap}}
  .stat{{text-align:center}} .stat .num{{font-size:2em;font-weight:bold;color:#e65100}}
  .stat .lbl{{font-size:.85em;color:#555}}
  .filter-bar{{margin:15px 0;display:flex;gap:10px;flex-wrap:wrap}}
  .badge{{padding:4px 10px;border-radius:12px;font-size:.8em;cursor:pointer;border:1px solid #999;background:#fff}}
  .badge:hover{{background:#e65100;color:#fff;border-color:#e65100}}
  table{{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 6px rgba(0,0,0,.1)}}
  th{{background:#e65100;color:#fff;padding:10px 12px;text-align:left;font-size:.9em}}
  td{{padding:9px 12px;font-size:.85em;vertical-align:top;border-bottom:1px solid #eee}}
  tr:hover{{background:#fff3e0}}
  a{{color:#e65100;text-decoration:none}} a:hover{{text-decoration:underline}}
  .score-high{{color:#2e7d32;font-weight:bold}}
  .score-med{{color:#f57c00;font-weight:bold}}
  .score-low{{color:#c62828}}
  .group{{display:inline-block;padding:2px 7px;border-radius:10px;font-size:.75em;font-weight:bold;color:#fff}}
  .g-NL-NL{{background:#e65100}} .g-NL-EN{{background:#00838f}}
  .g-EU-EN{{background:#37474f}} .g-EU-DE{{background:#4e342e}}
  .g-TRANS{{background:#1565c0}} .g-SOCIAL{{background:#ad1457}} .g-COMPANY{{background:#558b2f}}
  .g-META{{background:#5e35b1}}
  .cover{{white-space:pre-wrap;font-family:monospace;background:#fff;padding:20px;border-radius:8px;
           border:1px solid #ccc;font-size:.85em;margin-top:30px}}
  @media(max-width:600px){{th:nth-child(3),th:nth-child(4),td:nth-child(3),td:nth-child(4){{display:none}}}}
</style>
</head>
<body>
<h1>🚚 ADR Liquid / Tank Container Driver — Netherlands Job Search Report</h1>
<p><strong>Date:</strong> {date} &nbsp;|&nbsp;
   <strong>Target:</strong> Netherlands (NL) + EU &nbsp;|&nbsp;
   <strong>Language:</strong> English preferred</p>

<div class="stats">
  <div class="stat"><div class="num">{total}</div><div class="lbl">Total results</div></div>
  <div class="stat"><div class="num">{high}</div><div class="lbl">High relevance (≥50)</div></div>
  <div class="stat"><div class="num">{queries}</div><div class="lbl">Queries executed</div></div>
  <div class="stat"><div class="num">{boards}</div><div class="lbl">Boards / sources</div></div>
</div>

<h2>Results</h2>
<table>
<thead><tr>
  <th>#</th><th>Title</th><th>URL</th><th>Source Group</th><th>Score</th><th>Snippet</th>
</tr></thead>
<tbody>
{rows}
</tbody>
</table>

<h2>Application Cover Letter (English)</h2>
<div class="cover">{cover}</div>

</body></html>
"""

# ─────────────────────────────────────────────────────────────────────────────
# MAIN SEARCH ENGINE
# ─────────────────────────────────────────────────────────────────────────────

async def run_search(export_format: str = "json", verbose: bool = True) -> list:
    scraper = WebScraper()
    all_jobs: list = []
    seen_urls: set = set()
    queries_done = 0
    boards_hit: set = set()

    WIDTH = 80
    print("=" * WIDTH)
    print("  ADR LIQUID / TANK CONTAINER DRIVER - EXHAUSTIVE JOB SEARCH")
    print(f"  Date      : {TODAY}")
    print(f"  Target    : Netherlands (NL) + Europe (EU) + Social Media")
    print(f"  Languages : English (primary) | Dutch (NL)")
    print(f"  Queries   : {len(SEARCH_QUERIES)}")
    print("=" * WIDTH)
    print()

    # Group progress tracker
    groups_seen: dict = {}
    for group, query in SEARCH_QUERIES:
        groups_seen[group] = groups_seen.get(group, 0) + 1

    current_group = None
    group_counts: dict = {}

    for group, query in SEARCH_QUERIES:
        if group != current_group:
            current_group = group
            labels = {
                "NL-NL": "[NL]  Dutch Job Boards (Dutch)",
                "NL-EN": "[NL]  Dutch Job Boards (English)",
                "EU-EN": "[EU]  European Boards (English)",
                "EU-DE": "[EU]  European Boards (German)",
                "TRANS": "[TR]  Transport & Logistics Boards",
                "SOCIAL": "[SM]  Social Media / Networks",
                "COMPANY":"[CO]  Company Direct Searches",
                "META":   "[MT]  Aggregators / Meta-search",
            }
            print(f"\n{'-'*WIDTH}")
            print(f"  {labels.get(group, group)}")
            print(f"{'-'*WIDTH}")

        queries_done += 1
        if verbose:
            short_q = query[:70] + "…" if len(query) > 70 else query
            print(f"  [{queries_done:03d}/{len(SEARCH_QUERIES)}] {short_q}")

        try:
            results = await scraper.search(query, num_results=10)
            new_in_query = 0
            for r in results:
                raw_url = r.get("url", "")
                url = clean_url(raw_url)
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                title = r.get("title", "")
                snippet = r.get("snippet", "")
                score = relevance_score(title, snippet)
                job = {
                    "group"  : group,
                    "query"  : query,
                    "title"  : title,
                    "url"    : url,
                    "snippet": snippet,
                    "score"  : score,
                }
                all_jobs.append(job)
                new_in_query += 1
                # Track board source
                domain = re.sub(r"https?://(?:www\.)?", "", url).split("/")[0]
                boards_hit.add(domain)
                group_counts[group] = group_counts.get(group, 0) + 1

            if verbose and new_in_query > 0:
                top = sorted(
                    [j for j in all_jobs[-new_in_query:]],
                    key=lambda x: x["score"], reverse=True
                )
                for j in top[:3]:
                    score_tag = "[H]" if j["score"] >= 50 else ("[M]" if j["score"] >= 20 else "[L]")
                    print(f"       {score_tag} [{j['score']:3d}] {j['title'][:65]}")

        except Exception as exc:
            if verbose:
                print(f"       ✗ Error: {exc}")

        # Small polite delay to avoid rate limiting
        await asyncio.sleep(0.4)

    # ── Sort: high score first ────────────────────────────────────────────
    all_jobs.sort(key=lambda x: x["score"], reverse=True)

    # ── Summary ──────────────────────────────────────────────────────────
    high = sum(1 for j in all_jobs if j["score"] >= 50)
    med  = sum(1 for j in all_jobs if 20 <= j["score"] < 50)
    low  = sum(1 for j in all_jobs if j["score"] < 20)

    print(f"\n{'='*WIDTH}")
    print(f"  SEARCH COMPLETE - RESULTS SUMMARY")
    print(f"{'='*WIDTH}")
    print(f"  Total unique results : {len(all_jobs)}")
    print(f"  High relevance (>=50): {high}  [H]")
    print(f"  Medium relevance     : {med}  [M]")
    print(f"  Low / noise          : {low}  [L]")
    print(f"  Boards / sources     : {len(boards_hit)}")
    print(f"  Queries executed     : {queries_done}")
    print()
    print("  Results by group:")
    for g, cnt in sorted(group_counts.items(), key=lambda x: -x[1]):
        print(f"    {g:<10} {cnt:>3} results")

    # -- Print top 20 high-relevance
    print(f"\n{'-'*WIDTH}")
    print("  TOP RESULTS (score >= 50):")
    print(f"{'-'*WIDTH}")
    top20 = [j for j in all_jobs if j["score"] >= 50][:20]
    if top20:
        for i, j in enumerate(top20, 1):
            print(f"  {i:2}. [{j['score']:3d}] {j['title']}")
            print(f"       {j['url'][:90]}")
            if j["snippet"]:
                print(f"       → {j['snippet'][:120]}")
            print()
    else:
        print("  (no high-relevance results - try with SearXNG running)")

    # -- Cover letter
    print(COVER_LETTER_EN)

    # -- Export
    base_name = str(OUTPUT_DIR / f"tank_adr_jobs_nl_{DATE_TAG}")
    _export_json(all_jobs, base_name, queries_done, len(boards_hit))
    if export_format in ("csv", "all"):
        _export_csv(all_jobs, base_name)
    if export_format in ("html", "all"):
        _export_html(all_jobs, base_name, queries_done, len(boards_hit))

    return all_jobs


# ─────────────────────────────────────────────────────────────────────────────
# EXPORTERS
# ─────────────────────────────────────────────────────────────────────────────

def _export_json(jobs: list, base: str, queries: int, boards: int):
    out = {
        "search_date"       : TODAY,
        "profile"           : CANDIDATE,
        "total_results"     : len(jobs),
        "high_relevance"    : sum(1 for j in jobs if j["score"] >= 50),
        "queries_executed"  : queries,
        "boards_sources"    : boards,
        "results"           : jobs,
    }
    path = f"{base}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"  [OK] JSON saved  -> {path}")


def _export_csv(jobs: list, base: str):
    path = f"{base}.csv"
    fields = ["score", "group", "title", "url", "snippet", "query"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(jobs)
    print(f"  [OK] CSV  saved  -> {path}")


def _export_html(jobs: list, base: str, queries: int, boards: int):
    path = f"{base}.html"
    high = sum(1 for j in jobs if j["score"] >= 50)

    rows = []
    for i, j in enumerate(jobs, 1):
        sc = j["score"]
        sc_cls = "score-high" if sc >= 50 else ("score-med" if sc >= 20 else "score-low")
        title_link = f'<a href="{j["url"]}" target="_blank">{j["title"] or j["url"][:60]}</a>'
        short_url  = re.sub(r"https?://(?:www\.)?", "", j["url"])[:55]
        url_link   = f'<a href="{j["url"]}" target="_blank">{short_url}</a>'
        grp = j["group"]
        rows.append(
            f"<tr>"
            f"<td>{i}</td>"
            f"<td>{title_link}</td>"
            f"<td>{url_link}</td>"
            f"<td><span class='group g-{grp}'>{grp}</span></td>"
            f"<td><span class='{sc_cls}'>{sc}</span></td>"
            f"<td>{j['snippet'][:150]}</td>"
            f"</tr>"
        )

    html = HTML_TEMPLATE.format(
        date   = TODAY,
        total  = len(jobs),
        high   = high,
        queries= queries,
        boards = boards,
        rows   = "\n".join(rows),
        cover  = COVER_LETTER_EN.replace("<", "&lt;").replace(">", "&gt;"),
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  [OK] HTML saved  -> {path}")
    print(f"     Open in browser: file:///{Path(path).resolve().as_posix()}")


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Exhaustive ADR Liquid/Tank Container driver job search — Netherlands (NL) + EU"
    )
    parser.add_argument(
        "--export",
        choices=["json", "csv", "html", "all"],
        default="html",
        help="Export format (default: html — also always saves JSON)",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress per-query output",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    results = asyncio.run(run_search(
        export_format=args.export,
        verbose=not args.quiet,
    ))
    sys.exit(0 if results else 1)
