import os
import sys


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def test_dashboard_html_uses_delegated_event_wiring_and_no_inline_handlers():
    from agent.dashboard import DASHBOARD_HTML

    assert "document.addEventListener('click'" in DASHBOARD_HTML
    assert "document.addEventListener('keydown'" in DASHBOARD_HTML
    assert 'data-action="save-key"' in DASHBOARD_HTML
    assert 'data-action="send-chat"' in DASHBOARD_HTML
    assert 'data-submit-on-enter="send-chat"' in DASHBOARD_HTML
    assert 'data-tab="chat"' in DASHBOARD_HTML
    assert "onclick=" not in DASHBOARD_HTML
    assert "onkeydown=" not in DASHBOARD_HTML
    assert "style=" not in DASHBOARD_HTML


def test_dashboard_html_bootstraps_event_binding_before_refresh_loop():
    from agent.dashboard import DASHBOARD_HTML

    bind_index = DASHBOARD_HTML.index("bindDashboardEvents();")
    hydrate_index = DASHBOARD_HTML.index("hydrateSavedKey();")
    overview_index = DASHBOARD_HTML.index("initOverview();")

    assert bind_index < hydrate_index < overview_index
