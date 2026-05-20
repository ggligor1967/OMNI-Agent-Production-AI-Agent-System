import os
import sys


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def test_dashboard_html_formats_structured_status_payloads():
    from agent.dashboard import DASHBOARD_HTML

    assert "function redactSensitiveValue(value,key='')" in DASHBOARD_HTML
    assert "function formatDisplayValue(value)" in DASHBOARD_HTML
    assert "function formatArrayValue(values)" in DASHBOARD_HTML
    assert "setStatusIndicator(formatDisplayValue(d.status||'running')" in DASHBOARD_HTML
    assert "{l:'Skills',v:d.skills||[]}" in DASHBOARD_HTML
    assert "{l:'Jobs',v:Array.isArray(d.jobs)?d.jobs.length:0}" in DASHBOARD_HTML
    assert "{l:'Providers',v:router.providers||[]}" in DASHBOARD_HTML
    assert "renderStatCards(cards);" in DASHBOARD_HTML


def test_dashboard_html_redacts_sensitive_keys_in_structured_values():
    from agent.dashboard import DASHBOARD_HTML

    assert "SENSITIVE_KEY_PATTERN" in DASHBOARD_HTML
    assert "api[_-]?key" in DASHBOARD_HTML
    assert "return '[redacted]'" in DASHBOARD_HTML
    assert "JSON.stringify(redactSensitiveValue(value),null,2)" in DASHBOARD_HTML
