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
    assert "DMR Identity" in resp.text or "DMR Protocol Parameters" in resp.text
    assert "Talkgroup Management" in resp.text


def test_p25_route(client):
    resp = client.get("/p25")
    assert resp.status_code == 200
    assert "P25 Network Identity" in resp.text or "P25 Phase 1 Parameters" in resp.text
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


def test_protocol_switch_api(client):
    """Test switching between DMR and P25 protocols."""
    resp = client.post("/api/protocol/switch", data={"target": "p25"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["target"] == "p25"

    # Verify P25 page now shows active, DMR page shows standby banner
    dmr_page = client.get("/dmr")
    assert "DMR Duplex is in Standby Mode" in dmr_page.text

    # Switch back to DMR
    resp_dmr = client.post("/api/protocol/switch", data={"target": "dmr"})
    assert resp_dmr.status_code == 200
    data_dmr = resp_dmr.json()
    assert data_dmr["success"] is True
    assert data_dmr["target"] == "dmr"

    # Verify P25 page now shows standby banner
    p25_page = client.get("/p25")
    assert "P25 Phase 1 is in Standby Mode" in p25_page.text


def test_save_updates_active_config(client):
    """Test saving P25 saves directly to active config."""
    from dvm_nexus.config_engine.dvm_yaml import dvm_config_manager

    # Post P25 save
    save_resp = client.post("/p25/save", data={
        "enable": "true",
        "nac": "456",
        "wacn": "BEE00",
        "sys_id": "002",
        "rfss_id": 1,
        "site_id": 1,
        "channel_id": 0,
        "channel_no": 1,
        "call_hang": 5,
        "tdu_preambles": 6,
        "p25_corr_count": 2,
        "p25_disc_bw": 0,
        "p25_post_bw": 0,
    })
    assert save_resp.status_code == 200
    assert "saved to active config" in save_resp.text.lower()

    # Verify active config has updated NAC and P25 enabled
    cfg = dvm_config_manager.load_config_dict()
    assert cfg["system"]["config"]["nac"] == "456"
    assert cfg["protocols"]["p25"]["enable"] is True


def test_p25_talkgroups_add_and_delete(client):
    """Test adding and deleting P25 talkgroups."""
    add_resp = client.post("/p25/talkgroups/add", data={
        "tgid": 9991,
        "name": "Test P25 TG",
        "parrot": "false",
    })
    assert add_resp.status_code == 200
    assert "9991" in add_resp.text

    del_resp = client.post("/p25/talkgroups/delete/9991")
    assert del_resp.status_code == 200
    assert "Deleted Talkgroup 9991" in del_resp.text


def test_api_settings_save(client):
    """Test saving DVMHost REST and RPC API connection settings."""
    from dvm_nexus.config_engine.dvm_yaml import dvm_config_manager
    resp = client.post("/config/api-settings/save", data={
        "rest_enable": "true",
        "rest_port": 9990,
        "rest_address": "127.0.0.1",
        "rest_password": "TEST_PASSWORD",
        "rpc_port": 9890,
        "rpc_password": "TEST_RPC_PASSWORD",
    })
    assert resp.status_code == 200
    assert "dvmhost rest api updated" in resp.text.lower()

    cfg = dvm_config_manager.load_config_dict()
    assert cfg["network"]["restEnable"] is True
    assert cfg["network"]["restPort"] == 9990
    assert cfg["network"]["restPassword"] == "TEST_PASSWORD"
    assert cfg["network"]["rpcPassword"] == "TEST_RPC_PASSWORD"
