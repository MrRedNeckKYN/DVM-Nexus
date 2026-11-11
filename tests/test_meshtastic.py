"""
Unit and Integration Tests for Meshtastic Integration
Tests transport abstraction, messaging, event bridge, and secure remote management.
"""

import pytest
import re
from starlette.testclient import TestClient
from dvm_nexus.main import app
from dvm_nexus.core.database import init_db, get_db_connection
from dvm_nexus.meshtastic.transport import MockTransport, SerialTransport, TCPTransport, BLETransport
from dvm_nexus.meshtastic.client import MeshtasticManager
from dvm_nexus.meshtastic.bridge import MeshtasticBridge
from dvm_nexus.meshtastic.remote_admin import RemoteAdminProcessor


@pytest.fixture
def client():
    init_db()
    with TestClient(app) as c:
        yield c


def test_transport_classes():
    # Verify transport classes can be instantiated
    st = SerialTransport(port="auto")
    assert st.port == "auto"
    assert st.is_connected() is False

    tt = TCPTransport(hostname="192.168.1.100", port=4403)
    assert tt.hostname == "192.168.1.100"
    assert tt.is_connected() is False

    bt = BLETransport(address="AA:BB:CC:DD:EE:FF")
    assert bt.address == "AA:BB:CC:DD:EE:FF"
    assert bt.is_connected() is False

    # Mock transport
    mt = MockTransport()
    assert mt.connect() is True
    assert mt.is_connected() is True
    nodes = mt.get_nodes()
    assert len(nodes) >= 3
    node_ids = [n["id"] for n in nodes]
    assert "!2f9901aa" in node_ids
    assert "!4a12bc88" in node_ids


def test_meshtastic_manager_messaging():
    init_db()
    mgr = MeshtasticManager()
    mgr.save_config("mock")
    mgr._init_transport()

    assert mgr.is_connected() is True
    status = mgr.get_status()
    assert status["transport_type"] == "mock"
    assert status["node_count"] >= 3

    # Send outbound message
    send_res = mgr.send_message("Test message over mesh", recipient="^all", channel=0)
    assert send_res["success"] is True

    # Retrieve messages
    messages = mgr.get_messages(limit=10)
    assert len(messages) >= 1
    recent_texts = [m["text"] for m in messages]
    assert "Test message over mesh" in recent_texts

    # Simulate inbound message
    mgr._handle_inbound_packet({
        "sender": "!4a12bc88",
        "recipient": "^all",
        "channel": 0,
        "text": "Field report from T-Beam",
        "snr": 8.2,
        "rssi": -70,
        "timestamp": None
    })

    messages2 = mgr.get_messages(limit=10)
    recent_texts2 = [m["text"] for m in messages2]
    assert "Field report from T-Beam" in recent_texts2


def test_meshtastic_event_bridge():
    init_db()
    mgr = MeshtasticManager()
    mgr.save_config("mock")
    mgr._init_transport()

    bridge = MeshtasticBridge(meshtastic_manager=mgr)
    initial_routes = bridge.get_routes()
    assert len(initial_routes) >= 1

    # Add custom route
    route_id = bridge.add_route(
        name="Test DMR Alert",
        source_type="dmr_talkgroup",
        source_id="3100",
        dest_channel=0,
        dest_node="^all",
        template="[ALERT] TG 3100: {event_text}"
    )
    assert route_id > 0

    # Trigger matching event
    bridge.handle_radio_event(
        source_type="dmr_talkgroup",
        source_id="3100",
        event_text="Radio ID 12345 transmitting on Slot 1"
    )

    # Check that message was sent
    msgs = mgr.get_messages(limit=5)
    matched = any("[ALERT] TG 3100: Radio ID 12345 transmitting" in m["text"] for m in msgs)
    assert matched is True

    # Clean up route
    del_res = bridge.delete_route(route_id)
    assert del_res is True


def test_secure_remote_admin():
    init_db()
    processor = RemoteAdminProcessor()

    # 1. Non-whitelisted node -> Rejected
    unauth_resp = processor.process_command("!badactor99", "!status")
    assert "Access Denied" in unauth_resp

    # 2. Whitelist admin node
    conn = get_db_connection()
    with conn:
        conn.execute("""
            INSERT OR REPLACE INTO meshtastic_admins (node_id, callsign_or_name, permission_level, can_restart_service, can_switch_profile, added_at)
            VALUES ('!testadmin1', 'W3TEST Admin', 'admin', 1, 1, '2026-11-09T00:00:00Z')
        """)
    conn.close()

    # 3. Read-only command !status and !ping
    ping_resp = processor.process_command("!testadmin1", "!ping")
    assert "Pong" in ping_resp

    status_resp = processor.process_command("!testadmin1", "!status")
    assert "[Nexus Status]" in status_resp

    # 4. Disruptive action: !restart requires 2-step confirmation
    restart_resp = processor.process_command("!testadmin1", "!restart")
    assert "!confirm" in restart_resp
    match = re.search(r"!confirm (\d{4})", restart_resp)
    assert match is not None
    token = match.group(1)

    # 5. Invalid token -> Rejected
    invalid_conf = processor.process_command("!testadmin1", "!confirm 9999")
    assert "Invalid or expired" in invalid_conf

    # 6. Valid token -> Confirmed and executed
    valid_conf = processor.process_command("!testadmin1", f"!confirm {token}")
    assert "restarted" in valid_conf or "SUCCESS" in valid_conf

    # 7. Re-use same token -> Replay protection prevents execution (reset rate limit first)
    processor._rate_limits.clear()
    reuse_conf = processor.process_command("!testadmin1", f"!confirm {token}")
    assert "Invalid or expired" in reuse_conf


def test_meshtastic_web_routes(client):
    # Main dashboard
    resp = client.get("/meshtastic")
    assert resp.status_code == 200
    assert "Meshtastic Device Connection" in resp.text
    assert "Live Mesh Messenger" in resp.text
    assert "Mesh Nodes" in resp.text
    assert "DMR / P25 to Meshtastic Event Bridge" in resp.text

    # JSON API status
    api_resp = client.get("/meshtastic/api/status")
    assert api_resp.status_code == 200
    data = api_resp.json()
    assert "connected" in data
    assert "transport_type" in data

    # Partial components
    comp_msgs = client.get("/meshtastic/components/messages")
    assert comp_msgs.status_code == 200

    comp_nodes = client.get("/meshtastic/components/nodes")
    assert comp_nodes.status_code == 200

    # Send message via form
    post_send = client.post("/meshtastic/send", data={
        "text": "Automated route test message",
        "recipient": "^all",
        "channel": 0
    })
    assert post_send.status_code == 200
    assert "Automated route test message" in post_send.text

    # Simulate inbound
    post_sim = client.post("/meshtastic/simulate-inbound", data={
        "text": "Simulated field report",
        "sender": "!4a12bc88",
        "channel": 0
    })
    assert post_sim.status_code == 200
    assert "Simulated field report" in post_sim.text


def test_bidirectional_text_gateway(client):
    from dvm_nexus.meshtastic.text_gateway import text_gateway
    from dvm_nexus.meshtastic.client import meshtastic_manager

    init_db()
    meshtastic_manager.save_config("mock")
    meshtastic_manager._init_transport()
    text_gateway.set_manager(meshtastic_manager)

    # 1. Test Gateway Config
    cfg = text_gateway.get_config()
    assert cfg.get("enabled") == "1"

    # 2. Test Radio -> Meshtastic: Text sent from DMR radio over-the-air
    radio_rx = text_gateway.handle_radio_inbound_text(
        protocol="dmr",
        src_id=98765,
        dest_id=3100,
        is_group=True,
        text="Field Unit 1 calling via DMR"
    )
    assert radio_rx["success"] is True

    # Verify message appeared in Meshtastic messages
    mesh_msgs = meshtastic_manager.get_messages(limit=5)
    matched = any("[DMR TG 3100 from RID 98765]: Field Unit 1 calling via DMR" in m["text"] for m in mesh_msgs)
    assert matched is True

    # 3. Test Meshtastic -> Radio: Text sent from Meshtastic handset with #dmr prefix
    mesh_rx = text_gateway.handle_meshtastic_inbound_text(
        sender_id="!4a12bc88",
        sender_name="N3XYZ",
        text="#dmr 3100 Responding from Meshtastic handheld",
        channel=0
    )
    assert mesh_rx is not None
    assert mesh_rx["protocol"] == "dmr"
    assert mesh_rx["target_id"] == 3100
    assert "Responding from Meshtastic handheld" in mesh_rx["text"]

    # 4. Test Meshtastic -> P25: Text sent with #p25 prefix
    mesh_p25 = text_gateway.handle_meshtastic_inbound_text(
        sender_id="!4a12bc88",
        sender_name="N3XYZ",
        text="#p25 101 P25 mesh dispatch test",
        channel=0
    )
    assert mesh_p25 is not None
    assert mesh_p25["protocol"] == "p25"
    assert mesh_p25["target_id"] == 101

    # 5. Check Bridged Messages Table
    recent_bridged = text_gateway.get_recent_messages(limit=10)
    assert len(recent_bridged) >= 2

    # 6. Test Web Endpoints
    # DMR SMS endpoint
    dmr_sms_resp = client.post("/dmr/send-sms", data={
        "target_id": 3100,
        "target_type": "group",
        "text": "Broadcast from DMR Web Panel",
        "forward_to_mesh": "1"
    })
    assert dmr_sms_resp.status_code == 200

    # P25 SMS endpoint
    p25_sms_resp = client.post("/p25/send-sms", data={
        "target_id": 101,
        "target_type": "group",
        "text": "Broadcast from P25 Web Panel",
        "forward_to_mesh": "1"
    })
    assert p25_sms_resp.status_code == 200

    # Meshtastic simulate radio text endpoint
    sim_resp = client.post("/meshtastic/simulate-radio-text", data={
        "protocol": "dmr",
        "src_id": 54321,
        "dest_id": 3100,
        "is_group": "1",
        "text": "OTA radio test text message"
    })
    assert sim_resp.status_code == 200
    assert "OTA radio test text message" in sim_resp.text

    # Meshtastic bridged messages partial
    comp_resp = client.get("/meshtastic/components/bridged-messages")
    assert comp_resp.status_code == 200
    assert "Delivered" in comp_resp.text


def test_voice_bridge_gateway(client):
    """Test Voice Bridge (STT / TTS) configuration, endpoints, and simulation."""
    from dvm_nexus.meshtastic.voice_gateway import voice_gateway

    # 1. Save voice bridge settings via Web UI endpoint
    save_resp = client.post("/meshtastic/voice-bridge/save", data={
        "enabled": "1",
        "stt_enabled": "1",
        "tts_enabled": "1",
        "protocol": "dmr",
        "voice_talkgroup": 99,
        "mesh_channel": 0,
        "whisper_model": "tiny.en",
        "udp_rx_port": 34001,
        "udp_tx_port": 32001,
        "udp_host": "127.0.0.1",
        "echo_lockout_sec": 3.0
    })
    assert save_resp.status_code == 200
    assert "Voice Bridge (STT/TTS) settings saved" in save_resp.text

    cfg = voice_gateway.get_config()
    assert cfg["enabled"] == "1"
    assert cfg["voice_talkgroup"] == "99"
    assert cfg["whisper_model"] == "tiny.en"

    status = voice_gateway.get_status()
    assert status["enabled"] is True
    assert status["voice_talkgroup"] == 99

    # 2. Simulate radio voice call -> Whisper -> Meshtastic
    sim_resp = client.post("/meshtastic/voice-bridge/simulate-rx", data={
        "src_id": 1001,
        "talkgroup": 99,
        "phrase": "Unit 1001 en route to dispatch"
    })
    assert sim_resp.status_code == 200
    assert "Simulated voice transmission" in sim_resp.text

    # 3. Verify activity logs
    logs = voice_gateway.get_recent_logs(limit=5)
    assert len(logs) >= 1
    recent_texts = [l["text"] for l in logs]
    assert "Unit 1001 en route to dispatch" in recent_texts

    # 4. Test live log polling partial
    log_resp = client.get("/meshtastic/voice-bridge/logs")
    assert log_resp.status_code == 200
    assert "Unit 1001 en route to dispatch" in log_resp.text

