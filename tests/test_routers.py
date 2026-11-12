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

    # Verify active config has updated NAC, P25 enabled, and BEE00 auto-sanitized to BB800
    cfg = dvm_config_manager.load_config_dict()
    assert cfg["system"]["config"]["nac"] == "456"
    assert cfg["system"]["config"]["netId"] == "BB800"
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


def test_rf_hud_component(client):
    """Test the real-time RF Transceiver State HUD component endpoint."""
    resp = client.get("/components/rf-hud")
    assert resp.status_code == 200
    assert "Live RF Transceiver Telemetry" in resp.text
    assert "CURRENT TALKER" in resp.text
    assert "TARGET TALKGROUP" in resp.text


def test_last_heard_component(client):
    """Test the real-time Last Heard activity feed component endpoint."""
    resp = client.get("/components/last-heard")
    assert resp.status_code == 200
    assert "Real-Time Last Heard Radio Activity" in resp.text
    assert "Caller (Alias / RID)" in resp.text


def test_simulate_call_endpoint(client):
    """Test injecting a simulated RF call and verifying it appears in the Last Heard feed."""
    resp = client.post("/api/telemetry/simulate-call")
    assert resp.status_code == 200
    assert "Real-Time Last Heard Radio Activity" in resp.text
    assert "Unit " in resp.text


def test_telemetry_service_log_parsing():
    """Unit test for TelemetryService log line regex decoding."""
    from dvm_nexus.core.telemetry_service import TelemetryService
    svc = TelemetryService()

    # 1. DMR RF Call Start
    svc.process_log_line("Nov 11 11:00:00 pi dvmhost[123]: DMR, received RF voice header from 3120101 to 3100, slot = 1")
    hud = svc.get_hud_status()
    assert hud["state"] == "RX"
    assert hud["protocol"] == "DMR"
    assert hud["active_slot"] == 1
    assert hud["source_id"] == 3120101
    assert hud["dest_id"] == 3100
    assert hud["slot1_active"] is True

    # 2. DMR Call End
    svc.process_log_line("Nov 11 11:00:04 pi dvmhost[123]: DMR, call end, duration = 4.2s, BER = 0.5%, loss = 0%")
    calls = svc.get_last_heard(limit=1)
    assert len(calls) >= 1
    recent = calls[0]
    assert recent["protocol"] == "dmr"
    assert recent["duration_sec"] == 4.2
    assert recent["ber_percent"] == 0.5

    # 3. P25 HDU & Call End
    svc.process_log_line("Nov 11 11:00:10 pi dvmhost[123]: P25, received RF voice HDU from 1001 to 101, NAC = 293")
    hud_p25 = svc.get_hud_status()
    assert hud_p25["state"] == "RX"
    assert hud_p25["protocol"] == "P25"
    assert hud_p25["source_id"] == 1001
    assert hud_p25["dest_id"] == 101

    svc.process_log_line("Nov 11 11:00:15 pi dvmhost[123]: P25, call end, duration = 5.0s, errs = 10/1233")
    p25_calls = svc.get_last_heard(limit=1)
    assert len(p25_calls) >= 1
    assert p25_calls[0]["protocol"] == "p25"
    assert p25_calls[0]["duration_sec"] == 5.0

    # 4. Upstream DVMHost / MMDVMHost log format (Slot X, received RF voice header ... to TG Y)
    svc.process_log_line("M: 2026-11-11 12:00:00.000 DMR Slot 2, received RF voice header from N0CALL to TG 3100")
    hud_up = svc.get_hud_status()
    assert hud_up["state"] == "RX"
    assert hud_up["active_slot"] == 2
    assert hud_up["source_alias"] == "N0CALL"
    assert hud_up["dest_id"] == 3100

    svc.process_log_line("M: 2026-11-11 12:00:06.100 DMR Slot 2, received RF end of voice transmission from N0CALL to TG 3100, 6.1 seconds, BER: 0.8%, RSSI: -71/-71/-71 dBm")
    hud_after = svc.get_hud_status()
    assert hud_after["state"] == "IDLE"


def test_origin_check_middleware_and_stale_config_rev(client):
    """Cross-origin POSTs must be blocked (403) while same-origin POSTs succeed, and stale config_rev is caught."""
    # Cross-origin POST blocked
    bad_resp = client.post(
        "/api/telemetry/simulate-call",
        headers={"Origin": "https://evil.example.com", "Host": "localhost:8080"},
    )
    assert bad_resp.status_code == 403

    # Same-origin POST allowed
    ok_resp = client.post(
        "/api/telemetry/simulate-call",
        headers={"Origin": "http://localhost:8080", "Host": "localhost:8080"},
    )
    assert ok_resp.status_code == 200

    # Stale config_rev rejected on /dmr/save
    stale_resp = client.post(
        "/dmr/save",
        data={
            "config_rev": "0000000000000000",
            "rx_freq": 434.0,
            "tx_freq": 439.0,
            "color_code": 1,
        },
    )
    assert stale_resp.status_code == 200
    assert "config.yml changed since this page was loaded" in stale_resp.text


def test_voice_gateway_resampler():
    """Verify 22050 Hz -> 8000 Hz 16-bit PCM downsampler preserves duration."""
    from dvm_nexus.meshtastic.voice_gateway import _resample_pcm16_mono
    # 1 second of 22050 Hz 16-bit mono = 44100 bytes
    raw_22k = b"\x00\x10" * 22050
    out_8k = _resample_pcm16_mono(raw_22k, src_rate=22050, dst_rate=8000)
    # 1 second of 8000 Hz 16-bit mono = 16000 bytes
    assert len(out_8k) == 16000


def test_duplex_simplex_and_control_channel_rules(client):
    """Verify DVMHost Full Duplex vs Simplex and Control Channel safety rules on /dmr, /p25, and /modem."""
    from dvm_nexus.config_engine.dvm_yaml import dvm_config_manager

    # 1. DMR Full Duplex + Dedicated TSCC + equal TX/RX auto-splits UHF (+5.0 MHz)
    resp_dmr_dup = client.post("/dmr/save", data={
        "enable": "true",
        "duplex": "true",
        "tx_freq": 439.0,
        "rx_freq": 439.0,
        "color_code": 1,
        "tscc_mode": "dedicated",
        "tscc_slot": 1,
        "disable_grant_src_check": "true",
        "beacon_enable": "true",
        "beacon_interval": 60,
        "beacon_duration": 3,
    })
    assert resp_dmr_dup.status_code == 200
    cfg = dvm_config_manager.load_config_dict()
    assert cfg["system"]["duplex"] is True
    assert cfg["system"]["simplexSameFrequency"] is False
    assert cfg["protocols"]["dmr"]["control"]["enable"] is True
    assert cfg["protocols"]["dmr"]["control"]["dedicated"] is True
    assert cfg["protocols"]["dmr"]["control"]["slot"] == 1
    assert cfg["protocols"]["dmr"]["control"]["disableGrantSourceIdCheck"] is True
    assert cfg["protocols"]["dmr"]["beacons"]["enable"] is True

    # 2. DMR Simplex forces TSCC and Beacons off and sets simplexSameFrequency=True
    resp_dmr_sim = client.post("/dmr/save", data={
        "enable": "true",
        "duplex": "false",
        "tx_freq": 439.0,
        "rx_freq": 444.0,
        "color_code": 1,
        "tscc_mode": "dedicated",
        "beacon_enable": "true",
    })
    assert resp_dmr_sim.status_code == 200
    cfg = dvm_config_manager.load_config_dict()
    assert cfg["system"]["duplex"] is False
    assert cfg["system"]["simplexSameFrequency"] is True
    assert cfg["protocols"]["dmr"]["control"]["enable"] is False
    assert cfg["protocols"]["dmr"]["control"]["dedicated"] is False
    assert cfg["protocols"]["dmr"]["beacons"]["enable"] is False

    # 3. P25 Full Duplex + Non-Dedicated Control Channel
    resp_p25_dup = client.post("/p25/save", data={
        "enable": "true",
        "duplex": "true",
        "tx_freq": 439.0,
        "rx_freq": 444.0,
        "nac": "293",
        "wacn": "BEE00",
        "sys_id": "001",
        "cc_mode": "non_dedicated",
        "cc_broadcast": "true",
        "cc_interval": 300,
        "cc_duration": 1,
        "control_only": "true",
        "disable_grant_src_check": "true",
    })
    assert resp_p25_dup.status_code == 200
    cfg = dvm_config_manager.load_config_dict()
    assert cfg["system"]["duplex"] is True
    assert cfg["system"]["simplexSameFrequency"] is False
    assert cfg["protocols"]["p25"]["control"]["enable"] is True
    assert cfg["protocols"]["p25"]["control"]["dedicated"] is False
    assert cfg["protocols"]["p25"]["control"]["interval"] == 300
    assert cfg["protocols"]["p25"]["controlOnly"] is True
    assert cfg["protocols"]["p25"]["control"]["disableGrantSourceIdCheck"] is True

    # 4. P25 Simplex forces Control Channel & controlOnly off
    resp_p25_sim = client.post("/p25/save", data={
        "enable": "true",
        "duplex": "false",
        "tx_freq": 439.0,
        "rx_freq": 444.0,
        "nac": "293",
        "wacn": "BEE00",
        "sys_id": "001",
        "cc_mode": "dedicated",
        "control_only": "true",
    })
    assert resp_p25_sim.status_code == 200
    cfg = dvm_config_manager.load_config_dict()
    assert cfg["system"]["duplex"] is False
    assert cfg["system"]["simplexSameFrequency"] is True
    assert cfg["protocols"]["p25"]["control"]["enable"] is False
    assert cfg["protocols"]["p25"]["control"]["dedicated"] is False
    assert cfg["protocols"]["p25"]["controlOnly"] is False


def test_dvmhost_error_4_wacn_and_hex_channel_no_guard(client):
    """
    Verify DVMHost C++ compatibility guards:
    1. Banned P25 WACN (0xBEE00..0xBEEFF) is never written to config.yml (prevents 'FATAL PANIC; error 4' / 'error 38258').
    2. channelNo and voiceChNo[0].channelNo are written as uppercase hex strings (prevents $0FFF clamping in strtoul base-16).
    3. Legacy decimal channelNo: 2019 is migrated to '7E3' while preserving 462.61875 MHz.
    """
    from dvm_nexus.config_engine.dvm_yaml import (
        dvm_config_manager, is_banned_p25_wacn, sanitize_p25_wacn, parse_channel_no, format_channel_no
    )

    assert is_banned_p25_wacn("BEE00") is True
    assert is_banned_p25_wacn("BEE09") is True
    assert is_banned_p25_wacn("BEEFF") is True
    assert is_banned_p25_wacn("BB800") is False
    assert sanitize_p25_wacn("BEE00") == "BB800"
    assert sanitize_p25_wacn("BEE09") == "BB800"
    assert sanitize_p25_wacn("AB123") == "AB123"

    # Legacy int 2019 -> 2019 -> '7E3' -> 2019
    assert parse_channel_no(2019) == 2019
    assert format_channel_no(2019) == "7E3"
    assert parse_channel_no("7E3") == 2019

    # Save DMR at 462.61875 MHz TX / 467.61875 MHz RX (Channel ID 2, Channel No 2019 = 0x7E3)
    resp = client.post("/dmr/save", data={
        "enable": "true",
        "duplex": "true",
        "tx_freq": 462.61875,
        "rx_freq": 467.61875,
        "color_code": 1,
    })
    assert resp.status_code == 200
    cfg = dvm_config_manager.load_config_dict()
    assert cfg["system"]["config"]["netId"] == "BB800"
    assert cfg["system"]["config"]["channelNo"] == "7E3"
    assert cfg["system"]["config"]["voiceChNo"][0]["channelNo"] == "7E3"

    tx_mhz, rx_mhz = dvm_config_manager.get_tuned_frequencies()
    assert abs(tx_mhz - 462.61875) < 0.0001
    assert abs(rx_mhz - 467.61875) < 0.0001




