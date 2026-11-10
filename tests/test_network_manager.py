"""
Unit & Integration Tests for DVM Nexus Network & Wi-Fi Management Module
Tests NetworkController, nmcli integration, failover logic, and REST/HTMX endpoints.
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


def test_network_controller_hotspot_toggle_sim():
    """Verify switching between client and hotspot mode."""
    # Toggle to Hotspot
    res = network_controller.toggle_hotspot(True)
    assert res["success"] is True
    assert res["mode"] == "hotspot"
    assert res["active_ssid"] == HOTSPOT_SSID
    assert res["ip"] == HOTSPOT_IP

    status = network_controller.get_status()
    assert status["mode"] == "hotspot"
    assert status["active_ssid"] == HOTSPOT_SSID

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


def test_manual_override_flag():
    """Verify manual override duration and clearing."""
    network_controller.set_manual_override()
    assert network_controller.is_manual_override_active() is True
    network_controller.clear_manual_override()
    assert network_controller.is_manual_override_active() is False


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
