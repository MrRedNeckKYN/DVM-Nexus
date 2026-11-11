"""
Unit tests for DVM Nexus Embedded Modem Firmware Flasher Engine & Router.
"""

import pytest
from starlette.testclient import TestClient
from dvm_nexus.main import app
from dvm_nexus.core.firmware_flasher import firmware_flasher, FIRMWARE_TARGETS


@pytest.fixture
def client():
    return TestClient(app)


def test_firmware_flasher_status():
    """Verify flasher status reports available targets and idle state."""
    status = firmware_flasher.get_status()
    assert status["state"] == "idle"
    assert "dvm_hs_dual_14mhz" in status["targets"]
    assert "dvm_hs_simplex_14mhz" in status["targets"]
    assert "mmdvm_hs_dual_wpsd" in status["targets"]


def test_firmware_flasher_simulation():
    """Verify flash simulation on non-Linux dev host."""
    import asyncio
    res = asyncio.run(firmware_flasher.flash(target_key="dvm_hs_dual_14mhz", port="/dev/ttyAMA0"))
    assert res["success"] is True
    assert "simulated" in res["message"].lower() or "completed" in res["message"].lower()
    status = firmware_flasher.get_status()
    assert status["state"] == "completed"
    assert len(status["logs"]) > 0


def test_modem_page_includes_flasher(client):
    """Verify /modem page renders the firmware flasher card."""
    response = client.get("/modem")
    assert response.status_code == 200
    assert "DVMProject Modem Firmware Installer" in response.text
    assert "dvm_hs_dual_14mhz" in response.text
    assert "Flash DVM Firmware Now" in response.text


def test_firmware_status_endpoint(client):
    """Verify /components/firmware-status returns the HTMX modal snippet."""
    response = client.get("/components/firmware-status")
    assert response.status_code == 200
    assert "firmware-flash-status" in response.text


def test_firmware_flash_endpoint(client):
    """Verify POST /api/modem/firmware/flash triggers flasher."""
    response = client.post(
        "/api/modem/firmware/flash",
        data={"target": "dvm_hs_dual_14mhz", "port": "/dev/ttyAMA0"}
    )
    assert response.status_code == 200
    assert "firmware-flash-status" in response.text
