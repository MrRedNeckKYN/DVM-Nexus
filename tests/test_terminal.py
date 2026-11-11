"""
Tests for DVM Nexus Web Terminal and Console.
"""

import pytest
from starlette.testclient import TestClient
from dvm_nexus.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_terminal_page_renders(client):
    """Verify /terminal returns 200 and loads the console interface."""
    response = client.get("/terminal")
    assert response.status_code == 200
    assert "System Web Terminal &amp; Console" in response.text or "Web Terminal" in response.text
    assert "terminal-screen" in response.text
    assert "pinctrl get 14-15" in response.text
    assert "/dev/tty*" in response.text


def test_terminal_exec_echo(client):
    """Verify executing a basic command returns output and exit code 0."""
    response = client.post(
        "/api/terminal/exec",
        json={"command": "echo 'Hello DVM Nexus'"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["exit_code"] == 0
    assert "Hello DVM Nexus" in data["stdout"]
    assert "duration_ms" in data
    assert "cwd" in data


def test_terminal_exec_empty_command(client):
    """Verify empty command returns clean response."""
    response = client.post(
        "/api/terminal/exec",
        json={"command": "   "}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["command"] == ""
    assert data["stdout"] == ""
    assert data["exit_code"] == 0


def test_terminal_exec_cd(client):
    """Verify directory navigation via cd command."""
    response = client.post(
        "/api/terminal/exec",
        json={"command": "cd .."}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["exit_code"] == 0
    assert "Directory changed to:" in data["stdout"]
