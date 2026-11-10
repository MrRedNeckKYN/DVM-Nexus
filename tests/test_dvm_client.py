"""
Unit tests for DVMHost REST API Client
"""

import pytest
from dvm_nexus.core.dvm_client import DVMHostClient


def test_dvm_client_offline_behavior():
    # Tests that when DVMHost is offline, client gracefully returns structured error without crashing
    client = DVMHostClient(host="127.0.0.1", port=9998, password="WRONG_PASSWORD")
    res = client.get_status()
    assert res["success"] is False
    assert "error" in res
    assert res["status"] in (401, 503)
