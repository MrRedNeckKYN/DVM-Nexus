"""
End-to-End Route Integration Tests
Verifies that all views and components render without errors.
"""

import pytest
from starlette.testclient import TestClient
from dvm_nexus.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_dashboard_route(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "DVM Nexus" in resp.text
    assert "Active Protocol" in resp.text


def test_wizard_route(client):
    resp = client.get("/wizard")
    assert resp.status_code == 200
    assert "Network Builder Wizard" in resp.text
    assert "Band Plan Preset" in resp.text


def test_dmr_route(client):
    resp = client.get("/dmr")
    assert resp.status_code == 200
    assert "DMR Protocol Parameters" in resp.text
    assert "Talkgroup Management" in resp.text


def test_p25_route(client):
    resp = client.get("/p25")
    assert resp.status_code == 200
    assert "P25 Phase 1 Parameters" in resp.text
    assert "IDEN Frequency Bandplan Table" in resp.text


def test_profiles_route(client):
    resp = client.get("/profiles")
    assert resp.status_code == 200
    assert "Radio Profile Manager" in resp.text
    assert "Default DMR Duplex" in resp.text or "Default P25 Phase 1" in resp.text


def test_modem_route(client):
    resp = client.get("/modem")
    assert resp.status_code == 200
    assert "ADF7021 Transceiver Tuning" in resp.text
    assert "Serial Device Port" in resp.text


def test_config_route(client):
    resp = client.get("/config")
    assert resp.status_code == 200
    assert "Configuration Snapshots" in resp.text
    assert "Raw YAML Editor" in resp.text


def test_api_status_route(client):
    resp = client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "system" in data
    assert "service" in data
    assert "dvm" in data


def test_components_routes(client):
    resp_stats = client.get("/components/system-stats")
    assert resp_stats.status_code == 200
    assert "CPU Utilization" in resp_stats.text

    resp_badge = client.get("/components/service-badge")
    assert resp_badge.status_code == 200
    assert "DVMHost" in resp_badge.text
