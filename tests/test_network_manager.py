"""
Unit & Integration Tests for DVM Nexus Network & Wi-Fi Management Module
Tests NetworkController, nmcli integration, failover logic, web provisioning, and REST/HTMX endpoints.
"""

import pytest
from starlette.testclient import TestClient
from unittest.mock import patch, MagicMock

from dvm_nexus.main import app
from dvm_nexus.core.network_manager import (
    network_controller,
    HOTSPOT_CON_NAME,
    HOTSPOT_SSID,
    HOTSPOT_IP,
)


@pytest.fixture
def client():
    return TestClient(app)


def test_network_controller_get_status_sim():
    """Verify simulated network status in development environment."""
    network_controller.clear_manual_override()
    status = network_controller.get_status()
    assert "mode" in status
    assert "active_ssid" in status
    assert "ip_address" in status
    assert "mac_address" in status
    assert "interface" in status
    assert status["interface"] == "wlan0"
    assert "is_connected" in status
    assert "hotspot_provisioned" in status
    assert "hotspot_config" in status


def test_network_controller_hotspot_toggle_sim():
    """Verify switching between client and hotspot mode."""
    # Toggle to Hotspot
    res = network_controller.toggle_hotspot(True)
    assert res["success"] is True
    assert res["mode"] == "hotspot"
    assert res["ip"] == HOTSPOT_IP

    status = network_controller.get_status()
    assert status["mode"] == "hotspot"

    # Toggle back to Client
    res_client = network_controller.toggle_hotspot(False)
    assert res_client["success"] is True
    assert res_client["mode"] == "client"

    status_client = network_controller.get_status()
    assert status_client["mode"] == "client"


def test_network_controller_scan_sim():
    """Verify Wi-Fi network scanning, deduplication, and signal ordering."""
    networks = network_controller.scan_networks()
    assert isinstance(networks, list)
    assert len(networks) > 0
    first = networks[0]
    assert "ssid" in first
    assert "signal" in first
    assert "security" in first
    # Verify sorted descending by signal
    signals = [n["signal"] for n in networks]
    assert signals == sorted(signals, reverse=True)


def test_network_controller_connect_sim():
    """Verify Wi-Fi client connection."""
    res = network_controller.connect_network("Station-Wi-Fi-5G", "SecretPassword")
    assert res["success"] is True
    assert res["ssid"] == "Station-Wi-Fi-5G"

    # Empty SSID error
    err_res = network_controller.connect_network("")
    assert err_res["success"] is False
    assert err_res["status_code"] == 400


def test_network_controller_connect_from_hotspot_sim():
    """Verify connecting back to client Wi-Fi while currently in Hotspot mode."""
    # Put controller in hotspot mode
    network_controller.toggle_hotspot(True)
    assert network_controller.get_status()["mode"] == "hotspot"

    # Connect back to home Wi-Fi
    res = network_controller.connect_network("Home-Network", "MyHomePass123")
    assert res["success"] is True
    assert res["ssid"] == "Home-Network"
    status = network_controller.get_status()
    assert status["mode"] == "client"
    assert status["active_ssid"] == "Home-Network"


def test_network_controller_connect_failure_rollback_sim():
    """Verify failed client connection rolls back safely to Field-Hotspot."""
    # Put controller in hotspot mode
    network_controller.toggle_hotspot(True)
    assert network_controller.get_status()["mode"] == "hotspot"

    # Attempt connection to invalid network
    res = network_controller.connect_network("FailNetwork", "WrongPassword")
    assert res["success"] is False
    assert res["status_code"] == 401
    assert "restored" in res["error"].lower()


def test_get_known_wifi_connections():
    """Verify querying saved client Wi-Fi connection profiles."""
    conns = network_controller.get_known_wifi_connections()
    assert isinstance(conns, list)
    assert len(conns) > 0
    for c in conns:
        assert "name" in c
        assert "ssid" in c
        assert "uuid" in c


def test_manual_override_flag():
    """Verify manual override duration and clearing."""
    network_controller.set_manual_override()
    assert network_controller.is_manual_override_active() is True
    network_controller.clear_manual_override()
    assert network_controller.is_manual_override_active() is False


def test_hotspot_config_persistence_and_provision():
    """Verify Web UI hotspot settings storage and auto-provisioning."""
    cfg = network_controller.get_hotspot_config()
    assert "ssid" in cfg
    assert "password" in cfg

    updated = network_controller.save_hotspot_config({
        "ssid": "MMDVM-Field-AP",
        "password": "NewSecretPassword123",
        "channel": 11,
        "auto_failover_enabled": True
    })
    assert updated["channel"] == 11
    assert updated["password"] == "NewSecretPassword123"

    # Provision
    prov_res = network_controller.provision_hotspot()
    assert prov_res["success"] is True
    assert prov_res["provisioned"] is True
    assert network_controller.is_hotspot_provisioned() is True


def test_sync_client_priorities():
    """Verify 1-click client connection priority synchronization."""
    res = network_controller.sync_client_priorities()
    assert res["success"] is True


def test_watchdog_step_execution():
    """Verify watchdog failover state machine evaluation."""
    network_controller.clear_manual_override()
    step_res = network_controller.watchdog_step()
    assert isinstance(step_res, dict)
    assert "action" in step_res


# ==============================================================================
# REST API Endpoint Tests
# ==============================================================================

def test_api_network_status(client):
    """GET /api/network/status"""
    resp = client.get("/api/network/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "mode" in data
    assert "active_ssid" in data
    assert "ip_address" in data
    assert "gateway" in data
    assert "signal_quality" in data
    assert "mac_address" in data


def test_api_network_hotspot_toggle(client):
    """POST /api/network/hotspot/toggle"""
    resp = client.post("/api/network/hotspot/toggle", json={"enable": True})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["mode"] == "hotspot"

    # Toggle back to client
    resp2 = client.post("/api/network/hotspot/toggle", json={"enable": False})
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["success"] is True
    assert data2["mode"] == "client"


def test_api_network_scan(client):
    """GET /api/network/scan"""
    resp = client.get("/api/network/scan")
    assert resp.status_code == 200
    data = resp.json()
    assert "networks" in data
    assert "count" in data
    assert data["count"] > 0


def test_api_network_connect(client):
    """POST /api/network/connect"""
    resp = client.post("/api/network/connect", json={"ssid": "MeshNet-Repeater", "password": "pass"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["ssid"] == "MeshNet-Repeater"

    # Test missing SSID validation
    resp_bad = client.post("/api/network/connect", json={"ssid": "", "password": ""})
    assert resp_bad.status_code == 400


def test_api_hotspot_config_and_provision(client):
    """Test /api/network/hotspot/config and /provision."""
    resp = client.get("/api/network/hotspot/config")
    assert resp.status_code == 200
    data = resp.json()
    assert "config" in data
    assert "provisioned" in data

    post_resp = client.post("/api/network/hotspot/config", json={"ssid": "MMDVM-Field-AP", "channel": 6})
    assert post_resp.status_code == 200
    assert post_resp.json()["success"] is True

    prov_resp = client.post("/api/network/hotspot/provision")
    assert prov_resp.status_code == 200
    assert prov_resp.json()["success"] is True

    sync_resp = client.post("/api/network/hotspot/sync-priorities")
    assert sync_resp.status_code == 200
    assert sync_resp.json()["success"] is True


# ==============================================================================
# Web UI HTML Endpoint Tests
# ==============================================================================

def test_network_html_page(client):
    """GET /network renders full Wi-Fi Management view."""
    resp = client.get("/network")
    assert resp.status_code == 200
    assert "Wi-Fi &amp; Field Hotspot" in resp.text or "Wi-Fi & Field Hotspot" in resp.text
    assert "network-hero-card" in resp.text
    assert "MMDVM-Field-AP" in resp.text
    assert "Quick Connect" in resp.text
    assert "hotspot-card-container" in resp.text


def test_network_components_htmx(client):
    """Test HTMX polling fragments."""
    # Status component
    resp_stat = client.get("/components/network-status")
    assert resp_stat.status_code == 200
    assert "network-hero-card" in resp_stat.text

    # Scan component
    resp_scan = client.get("/components/network-scan")
    assert resp_scan.status_code == 200
    assert "scanned-networks-list" in resp_scan.text

    # Mode toggle component
    resp_tog = client.post("/components/network-toggle", data={"enable": "true"})
    assert resp_tog.status_code == 200
    assert "FIELD AP ACTIVE" in resp_tog.text

    # Connect component
    resp_conn = client.post("/components/network-connect", data={"ssid": "Field-Station-Net", "password": "123"})
    assert resp_conn.status_code == 200
    assert "CLIENT MODE" in resp_conn.text

    # Hotspot config component
    resp_hcfg = client.post("/components/network-hotspot-config", data={
        "ssid": "MMDVM-Field-AP",
        "password": "Password123",
        "band": "bg",
        "channel": "6",
        "failover_threshold_sec": "45",
        "auto_failover_enabled": "true"
    })
    assert resp_hcfg.status_code == 200
    assert "Failover Hotspot Settings" in resp_hcfg.text or "PROVISIONED" in resp_hcfg.text


def test_connect_network_linux_flow_with_hotspot_teardown():
    """Verify Linux execution order: profile setup -> hotspot down -> client up."""
    executed_cmds = []

    def fake_run(cmd, timeout=None):
        executed_cmds.append(cmd)
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "Connection successfully activated"
        mock_res.stderr = ""
        return mock_res

    with patch.object(network_controller, "_has_nmcli", True), \
         patch("os.name", "posix"), \
         patch.object(network_controller, "_run_cmd", side_effect=fake_run), \
         patch.object(network_controller, "get_status", return_value={"mode": "hotspot", "ip_address": "192.168.1.185"}), \
         patch.object(network_controller, "get_known_wifi_connections", return_value=[{"name": "HomeWiFi", "ssid": "HomeWiFi", "uuid": "u-1"}]):

        res = network_controller.connect_network("HomeWiFi", "SecretPass")
        assert res["success"] is True

        # Check command sequence
        cmd_strs = [" ".join(c) for c in executed_cmds]
        # Should modify existing profile
        assert any("connection modify HomeWiFi" in s for s in cmd_strs)
        # Should tear down hotspot
        assert any("connection down Field-Hotspot" in s for s in cmd_strs)
        # Should bring up HomeWiFi
        assert any("connection up HomeWiFi" in s for s in cmd_strs)


def test_connect_network_linux_failure_triggers_hotspot_rollback():
    """Verify that when client connection fails on Linux, Field-Hotspot is restored."""
    executed_cmds = []

    def fake_run(cmd, timeout=None):
        executed_cmds.append(cmd)
        mock_res = MagicMock()
        if "connection up HomeWiFi" in " ".join(cmd) or "dev wifi connect" in " ".join(cmd):
            mock_res.returncode = 1
            mock_res.stdout = ""
            mock_res.stderr = "Error: Secrets were required, but not provided"
        else:
            mock_res.returncode = 0
            mock_res.stdout = ""
            mock_res.stderr = ""
        return mock_res

    with patch.object(network_controller, "_has_nmcli", True), \
         patch("os.name", "posix"), \
         patch.object(network_controller, "_run_cmd", side_effect=fake_run), \
         patch.object(network_controller, "get_status", return_value={"mode": "hotspot", "ip_address": "10.42.0.1"}), \
         patch.object(network_controller, "get_known_wifi_connections", return_value=[]):

        res = network_controller.connect_network("HomeWiFi", "BadPassword")
        assert res["success"] is False
        assert res["status_code"] == 401
        assert "restored" in res["error"].lower()

        cmd_strs = [" ".join(c) for c in executed_cmds]
        # Should tear down hotspot
        assert any("connection down Field-Hotspot" in s for s in cmd_strs)
        # Should attempt client connect
        assert any("connection up HomeWiFi" in s for s in cmd_strs)
        # CRITICAL ROLLBACK: Should restore Field-Hotspot!
        assert any("connection up Field-Hotspot" in s for s in cmd_strs)
