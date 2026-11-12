"""
Unit tests for DVMHost REST API Client
"""

import json
import socket
import threading
import pytest
from dvm_nexus.core.dvm_client import DVMHostClient


def test_dvm_client_offline_behavior():
    # Tests that when DVMHost is offline, client gracefully returns structured error without crashing
    client = DVMHostClient(host="127.0.0.1", port=9998, password="WRONG_PASSWORD")
    res = client.get_status()
    assert res["success"] is False
    assert "error" in res
    assert res["status"] in (401, 503)


def test_dvm_client_single_write_http10_and_token_invalidation():
    """
    Verify DVMHostClient sends PUT /auth and GET /status in a single TCP packet (HTTP/1.0 + Connection: close)
    so DVMHost's ServerConnection::read() never hits 'End of file, code = 2', and invalidates stale tokens.
    """
    received_packets = []

    def mock_dvmhost_server(srv_sock: socket.socket):
        for _ in range(2):
            conn, _ = srv_sock.accept()
            with conn:
                data = conn.recv(8192).decode("utf-8", errors="replace")
                received_packets.append(data)
                if data.startswith("PUT /auth HTTP/1.0"):
                    body = json.dumps({"status": 200, "token": "123456789"})
                    resp = (
                        "HTTP/1.0 200 OK\r\n"
                        "Content-Type: application/json\r\n"
                        f"Content-Length: {len(body)}\r\n\r\n"
                        f"{body}"
                    )
                    conn.sendall(resp.encode("utf-8"))
                elif data.startswith("GET /status HTTP/1.0"):
                    body = json.dumps({"status": 200, "state": 1, "tx": False, "dmrSpace1": 31})
                    resp = (
                        "HTTP/1.0 200 OK\r\n"
                        "Content-Type: application/json\r\n"
                        f"Content-Length: {len(body)}\r\n\r\n"
                        f"{body}"
                    )
                    conn.sendall(resp.encode("utf-8"))

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.listen(5)

        t = threading.Thread(target=mock_dvmhost_server, args=(srv,), daemon=True)
        t.start()

        client = DVMHostClient(host="127.0.0.1", port=port, password="PASSWORD")
        res = client.get_status()
        t.join(timeout=3.0)

    assert res["success"] is True
    assert res["data"]["dmrSpace1"] == 31
    assert len(received_packets) == 2
    # Crucial check: PUT /auth headers AND JSON body arrived in a single recv()
    auth_head, sep, auth_body = received_packets[0].partition("\r\n\r\n")
    assert sep == "\r\n\r\n"
    assert '"auth":' in auth_body
    assert client._token == "123456789"
    client.invalidate_token()
    assert client._token is None


def test_dvm_client_ota_rid_payload_and_error_handling():
    """
    Verify dmr_rid_action and p25_rid_action send DVMHost's exact JSON keys ('command', 'dstId', 'slot', 'tgId')
    and that DVMHost errorPayload responses (HTTP 200 OK with JSON {"status": 400, "message": ...}) are surfaced
    as errors instead of false-positive successes.
    """
    captured_requests = []

    def mock_ota_server(srv_sock: socket.socket):
        for _ in range(5):
            conn, _ = srv_sock.accept()
            with conn:
                data = conn.recv(8192).decode("utf-8", errors="replace")
                head, _, body_str = data.partition("\r\n\r\n")
                req_line = head.splitlines()[0] if head else ""
                parsed_body = json.loads(body_str) if body_str.strip() else {}
                captured_requests.append((req_line, parsed_body))

                if req_line.startswith("PUT /auth"):
                    reply = {"status": 200, "token": "999888777"}
                elif req_line.startswith("PUT /dmr/rid") and parsed_body.get("command") == "invalid-cmd":
                    # Simulate DVMHost RESTAPI::errorPayload (HTTP 200 OK header, status 400 in JSON)
                    reply = {"status": 400, "message": "command was not valid"}
                else:
                    reply = {"status": 200, "message": "OK"}

                raw_reply = json.dumps(reply)
                resp = (
                    "HTTP/1.0 200 OK\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(raw_reply)}\r\n\r\n"
                    f"{raw_reply}"
                )
                conn.sendall(resp.encode("utf-8"))

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.listen(5)

        t = threading.Thread(target=mock_ota_server, args=(srv,), daemon=True)
        t.start()

        client = DVMHostClient(host="127.0.0.1", port=port, password="PASSWORD")
        res_dmr = client.dmr_rid_action("check", rid=3112138, slot=2)
        res_p25 = client.p25_rid_action("dyn-regrp", rid=1234567, tg_id=10100)
        res_bcn = client.dmr_rid_action("beacon", rid=1, slot=1)
        res_err = client.dmr_rid_action("invalid-cmd", rid=3112138, slot=1)
        t.join(timeout=3.0)

    assert res_dmr["success"] is True
    assert captured_requests[1] == (
        "PUT /dmr/rid HTTP/1.0",
        {"command": "check", "dstId": 3112138, "slot": 2},
    )

    assert res_p25["success"] is True
    assert captured_requests[2] == (
        "PUT /p25/rid HTTP/1.0",
        {"command": "dyn-regrp", "dstId": 1234567, "tgId": 10100},
    )

    assert res_bcn["success"] is True
    assert captured_requests[3][0] == "GET /dmr/beacon HTTP/1.0"

    assert res_err["success"] is False
    assert res_err["status"] == 400
    assert res_err["error"] == "command was not valid"

