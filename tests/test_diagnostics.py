"""
Unit and Integration Tests for DVM Nexus System Diagnostics & Log Viewer
Tests DiagnosticsEngine, health doctor, journalctl log parsing, and web UI routes.
"""

import pytest
from starlette.testclient import TestClient
from unittest.mock import patch, MagicMock

from dvm_nexus.main import app
from dvm_nexus.core.diagnostics import diagnostics_engine
from dvm_nexus.core.service_controller import service_controller
from dvm_nexus.config_engine.dvm_yaml import dvm_config_manager


@pytest.fixture
def client():
    return TestClient(app)


def test_diagnostics_health_checks():
    """Verify health doctor runs all checks and returns structured report."""
    report = diagnostics_engine.run_health_checks()
    assert "overall_status" in report
    assert "checks" in report
    assert isinstance(report["checks"], list)
    assert len(report["checks"]) >= 4

    check_ids = [c["id"] for c in report["checks"]]
    assert "service_state" in check_ids
    assert "config_file" in check_ids
    assert "yaml_indentation" in check_ids
    assert "stupid_check" in check_ids
    assert "daemon_mode" in check_ids
    assert "logging_config" in check_ids


def test_diagnostics_detects_bad_config_and_autofixes():
    """Verify doctor catches iAgreeNotToBeStupid=False and autofixes it."""
    cfg = dvm_config_manager.load_config_dict()
    # Temporarily set to false
    cfg["iAgreeNotToBeStupid"] = False
    cfg["daemon"] = True
    # Bypass auto-enforcement in save_config_dict for this test
    content = dvm_config_manager.config_path.read_text(encoding="utf-8")
    content = content.replace("iAgreeNotToBeStupid: true", "iAgreeNotToBeStupid: false")
    content = content.replace("daemon: false", "daemon: true")
    dvm_config_manager.config_path.write_text(content, encoding="utf-8")

    # Run check
    report = diagnostics_engine.run_health_checks()
    stupid_c = next((c for c in report["checks"] if c["id"] == "stupid_check"), None)
    assert stupid_c is not None
    assert stupid_c["status"] == "fail"

    # Run auto-fix
    fix_res = diagnostics_engine.auto_fix_all()
    assert len(fix_res["fixes_applied"]) >= 1

    # Verify fixed
    recheck = diagnostics_engine.run_health_checks()
    stupid_fixed = next((c for c in recheck["checks"] if c["id"] == "stupid_check"), None)
    assert stupid_fixed is not None
    assert stupid_fixed["status"] == "pass"


def test_diagnostics_detects_bad_yaml_indent_and_autofixes():
    """Verify doctor catches unindented sequence items (yaml-cpp incompatibility) and autofixes it."""
    orig = dvm_config_manager.config_path.read_text(encoding="utf-8")
    import re
    # Unindent list item under voiceChNo to match mapping key indentation
    bad_content = re.sub(r"(\n\s+voiceChNo:\n)\s+(- )", r"\1    - ", orig)
    if bad_content == orig:
        bad_content = orig + "\nfake_map:\n- bad_item: 1\n"
    try:
        dvm_config_manager.config_path.write_text(bad_content, encoding="utf-8")

        # Run doctor check
        report = diagnostics_engine.run_health_checks()
        yaml_check = next((c for c in report["checks"] if c["id"] == "yaml_indentation"), None)
        assert yaml_check is not None
        assert yaml_check["status"] == "fail"

        # Run auto-fix
        fix_res = diagnostics_engine.auto_fix_all()
        assert any("YAML" in f for f in fix_res["fixes_applied"])

        # Verify fixed
        recheck = diagnostics_engine.run_health_checks()
        yaml_fixed = next((c for c in recheck["checks"] if c["id"] == "yaml_indentation"), None)
        assert yaml_fixed is not None
        assert yaml_fixed["status"] == "pass"
    finally:
        dvm_config_manager.config_path.write_text(orig, encoding="utf-8")


def test_diagnostics_logs_parsing_and_filtering():
    """Verify journalctl log parsing and severity filtering."""
    # Test all logs
    logs_all = diagnostics_engine.get_service_logs(service_name="dvmhost", lines=50, filter_level="all")
    assert "entries" in logs_all
    assert len(logs_all["entries"]) > 0

    # Test error filter
    logs_err = diagnostics_engine.get_service_logs(service_name="dvmhost", lines=50, filter_level="errors")
    for entry in logs_err["entries"]:
        assert entry["level"] in ("error", "warn")


def test_diagnostics_html_page(client):
    """GET /diagnostics renders the main diagnostics view."""
    resp = client.get("/diagnostics")
    assert resp.status_code == 200
    assert "System Diagnostics &amp; Live Logs" in resp.text or "System Diagnostics & Live Logs" in resp.text
    assert "doctor-card" in resp.text
    assert "terminal-card" in resp.text


def test_diagnostics_components_htmx(client):
    """Test HTMX fragments for logs, checks, and autofix."""
    # Logs fragment
    resp_logs = client.get("/components/diagnostics-logs?service=dvmhost&filter=all&lines=30")
    assert resp_logs.status_code == 200
    assert "log-terminal-header" in resp_logs.text

    # Checks fragment
    resp_checks = client.get("/components/diagnostics-checks")
    assert resp_checks.status_code == 200
    assert "doctor-checks-list" in resp_checks.text

    # Autofix fragment
    resp_fix = client.post("/components/diagnostics-autofix")
    assert resp_fix.status_code == 200
    assert "Auto-Repair" in resp_fix.text or "Doctor" in resp_fix.text


def test_diagnostics_api_endpoints(client):
    """Test JSON REST endpoints for status, logs, and plain-text export."""
    # Status JSON
    resp_stat = client.get("/api/diagnostics/status")
    assert resp_stat.status_code == 200
    data = resp_stat.json()
    assert "overall_status" in data
    assert "checks" in data

    # Logs JSON
    resp_logs = client.get("/api/diagnostics/logs?service=dvmhost&lines=20")
    assert resp_logs.status_code == 200
    log_data = resp_logs.json()
    assert "entries" in log_data

    # Export plain text
    resp_exp = client.get("/api/diagnostics/export?service=dvmhost")
    assert resp_exp.status_code == 200
    assert "text/plain" in resp_exp.headers["content-type"]


def test_diagnostics_clear_logs(client):
    """Test clearing/rotating logs via engine and HTMX route."""
    res = diagnostics_engine.clear_logs(service_name="dvmhost")
    assert res["success"] is True

    resp = client.post("/components/diagnostics-clear-logs", data={"service": "dvmhost"})
    assert resp.status_code == 200
    assert "log-terminal-header" in resp.text


def test_diagnostics_system_reboot(client):
    """Test simulated system reboot endpoint."""
    resp = client.post("/api/system/reboot")
    assert resp.status_code == 200
    assert "reboot" in resp.text.lower()
