#!/usr/bin/env python
"""
ADR Driver Job Search Script
Comprehensive search across Swiss job boards for ADR driver positions
Date: 13.03.2026
"""
import asyncio
import json
import sys
import os
from pathlib import Path

# Disable auth
os.environ['AUTH_ENFORCE'] = 'false'

# Add project root to path (2 levels up from scripts/job_search/)
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config import CONFIG
from agent.tools import WebScraper

async def search_adr_jobs():
    """Execute exhaustive ADR driver job search across Swiss job boards."""
    scraper = WebScraper()
    
    # Comprehensive search queries targeting Swiss job boards
    search_queries = [
        # Primary Swiss job boards
        "ADR driver jobs Switzerland 2026",
        "Fahrer ADR Schweiz 2026",
        "chauffeur ADR Suisse 2026",
        
        # Specific locations
        "ADR driver Zurich Switzerland",
        "ADR driver Geneva Switzerland",
        "ADR driver Bern Switzerland",
        "ADR driver Basel Switzerland",
        "ADR driver Lausanne Switzerland",
        
        # Job board specific searches
        "site:jobup.ch ADR driver",
        "site:indeed.ch ADR driver",
        "site:linkedin.com ADR driver Switzerland",
        "site:stepstone.ch ADR driver",
        "site:swisstalents.ch ADR",
        
        # Transport companies
        "transport Switzerland ADR driver hiring",
        "logistics Switzerland ADR certification",
        "trucking company Switzerland ADR",
    ]
    
    print("=" * 80)
    print("🚚 EXHAUSTIVE ADR DRIVER JOB SEARCH - SWITZERLAND")
    print("Date: 13.03.2026")
    print("=" * 80)
    print()
    
    all_jobs = []
    seen_urls = set()
    
    for query in search_queries:
        print(f"🔍 Searching: {query}")
        try:
            results = await scraper.search(query, num_results=8)
            
            if results:
                for job in results:
                    url = job.get("url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        all_jobs.append(job)
                        title = job.get("title", "No title")
                        snippet = job.get("snippet", "")[:100]
                        print(f"  ✓ {title}")
                        if snippet:
                            print(f"    → {snippet}...")
        except Exception as e:
            print(f"  ✗ Error: {e}")
        
        print()
    
    # Report
    print("=" * 80)
    print(f"📊 SEARCH RESULTS SUMMARY")
    print("=" * 80)
    print(f"Total unique job listings found: {len(all_jobs)}")
    print()
    
    if all_jobs:
        print("📋 DETAILED LISTING:")
        print()
        for idx, job in enumerate(all_jobs, 1):
            print(f"{idx}. {job.get('title', 'No title')}")
            print(f"   URL: {job.get('url', 'N/A')}")
            if job.get('snippet'):
                print(f"   Summary: {job.get('snippet')[:200]}")
            print()
    
    # Save to file
    output_file = "adr_jobs_search_results_20260313.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump({
            "search_date": "2026-03-13",
            "total_found": len(all_jobs),
            "results": all_jobs
        }, f, indent=2, ensure_ascii=False)
    
    print(f"✅ Results saved to: {output_file}")
    
    return all_jobs


if __name__ == "__main__":
    results = asyncio.run(search_adr_jobs())
    sys.exit(0 if results else 1)
