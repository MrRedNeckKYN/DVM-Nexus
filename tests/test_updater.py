"""
Unit and Integration Tests for System Auto-Updater
"""

import os
import pytest
from starlette.testclient import TestClient
from dvm_nexus.main import app
from dvm_nexus.core.database import init_db
from dvm_nexus.core.config import settings
from dvm_nexus.core.updater import system_updater


@pytest.fixture
def client():
    init_db()
    with TestClient(app) as c:
        yield c


def test_updater_engine():
    os.environ["NEXUS_TEST_DRY_RUN"] = "1"
    init_db()
    info = system_updater.get_repo_info()
    assert "version" in info
    assert info["version"] == settings.app_version
    assert "github_repo" in info

    # Check for updates
    check_res = system_updater.check_for_updates()
    assert "last_checked" in check_res
    assert "channel" in check_res

    # Apply update dry-run / execution
    apply_res = system_updater.apply_update()
    assert "success" in apply_res
    assert "logs" in apply_res
    assert len(apply_res["logs"]) >= 3


def test_updater_web_routes(client):
    # Main updates page
    resp = client.get("/updates")
    assert resp.status_code == 200
    assert "System Auto-Updater" in resp.text
    assert "Installed: v" in resp.text
    assert "Safety Snapshot &amp; Rollback Guarantee" in resp.text or "Safety Snapshot" in resp.text

    # JSON API status
    api_resp = client.get("/updates/api/status")
    assert api_resp.status_code == 200
    data = api_resp.json()
    assert "nexus" in data
    assert "dvmhost" in data
    assert "repo_info" in data["nexus"]
    assert "check" in data["nexus"]

    # Check for updates endpoint
    check_post = client.post("/updates/check", data={"channel": "main"})
    assert check_post.status_code == 200
    assert "update-card-content" in check_post.text

    # Status partial
    comp_resp = client.get("/updates/components/status")
    assert comp_resp.status_code == 200

    # Apply update endpoint
    apply_post = client.post("/updates/apply", data={"channel": "main"})
    assert apply_post.status_code == 200
    assert "update-progress-view" in apply_post.text
    assert "Execution Console Output" in apply_post.text


def test_dvmhost_updater_engine():
    from dvm_nexus.core.dvmhost_updater import dvmhost_updater

    init_db()
    info = dvmhost_updater.get_installed_info()
    assert "target_asset_arch" in info
    assert "binary_path" in info

    # Check for DVMHost updates (queries GitHub or returns formatted check)
    check_res = dvmhost_updater.check_for_updates()
    assert "last_checked" in check_res
    assert "installed_version" in check_res

    # Apply update (simulated/offline or real asset)
    apply_res = dvmhost_updater.apply_update()
    assert "success" in apply_res
    assert "logs" in apply_res
    assert len(apply_res["logs"]) >= 2


def test_dvmhost_updater_web_routes(client):
    # Verify DVMHost section appears on /updates
    resp = client.get("/updates")
    assert resp.status_code == 200
    assert "DVMHost Radio Daemon Updater" in resp.text
    assert "Zero-Breakage Guarantee" in resp.text

    # DVMHost check endpoint
    check_resp = client.post("/updates/dvmhost/check")
    assert check_resp.status_code == 200
    assert "dvmhost-update-card-content" in check_resp.text

    # DVMHost apply endpoint
    apply_resp = client.post("/updates/dvmhost/apply")
    assert apply_resp.status_code == 200
    assert "dvmhost-update-progress-view" in apply_resp.text
    assert "DVMHost Upgrade Console Output" in apply_resp.text

    # DVMHost status partial
    status_resp = client.get("/updates/components/dvmhost-status")
    assert status_resp.status_code == 200
