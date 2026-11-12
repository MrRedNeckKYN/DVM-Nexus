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
