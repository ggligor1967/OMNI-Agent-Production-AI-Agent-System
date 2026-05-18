import os
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def test_dashboard_html_contains_dedicated_job_search_button():
    from agent.dashboard import DASHBOARD_HTML

    assert "ADR Tanker Job Search — Quick Launch" in DASHBOARD_HTML
    assert "Quick Run ADR Job Search" in DASHBOARD_HTML
    assert "runAdrJobSearch('ov-job-search')" in DASHBOARD_HTML
    assert "ov-job-search-export" in DASHBOARD_HTML
    assert "ov-job-search-out" in DASHBOARD_HTML
    assert "ADR Tanker Job Search" in DASHBOARD_HTML
    assert "Run ADR Job Search" in DASHBOARD_HTML
    assert "runAdrJobSearch('job-search')" in DASHBOARD_HTML
    assert "run_job_search_tank_adr_improved" in DASHBOARD_HTML
    assert "job-search-export" in DASHBOARD_HTML
    assert "job-search-out" in DASHBOARD_HTML


@pytest.mark.asyncio
async def test_dashboard_route_serves_dedicated_job_search_ui():
    from agent.dashboard import register_dashboard

    class DummyMemory:
        def get_audit_log(self, limit=30):
            return []

    class DummyAgent:
        memory = DummyMemory()

    app = web.Application()
    register_dashboard(app, DummyAgent())

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.get("/dashboard")
        html = await response.text()
    finally:
        await client.close()

    assert response.status == 200
    assert "ADR Tanker Job Search — Quick Launch" in html
    assert "Quick Run ADR Job Search" in html
    assert "ADR Tanker Job Search" in html
    assert "Run ADR Job Search" in html
    assert "run_job_search_tank_adr_improved" in html