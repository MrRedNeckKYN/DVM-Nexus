"""
Unit tests for Radio Profile Manager
"""

import pytest
from dvm_nexus.config_engine.profile_manager import ProfileManager
from dvm_nexus.core.config import settings
from dvm_nexus.core.database import init_db


def test_profile_lifecycle(tmp_path, monkeypatch):
    test_db = tmp_path / "test_nexus.db"
    monkeypatch.setattr(settings, "db_path", test_db)
    init_db()
    profiles_dir = tmp_path / "profiles"
    dvm_dir = tmp_path / "dvm"
    dvm_dir.mkdir(parents=True, exist_ok=True)
    (dvm_dir / "config.yml").write_text("protocols:\n  dmr:\n    enable: true\n", encoding="utf-8")

    mgr = ProfileManager(profiles_dir=profiles_dir, dvm_dir=dvm_dir)

    # Verify default profiles were seeded
    profiles = mgr.list_profiles()
    assert len(profiles) >= 2
    protocols = [p["protocol"] for p in profiles]
    assert "dmr" in protocols
    assert "p25" in protocols

    # Test creating new profile
    new_id = mgr.save_current_as_profile("Experimental UHF DMR", "dmr", "Test profile")
    prof = mgr.get_profile(new_id)
    assert prof["name"] == "Experimental UHF DMR"
    assert prof["protocol"] == "dmr"

    # Test activation
    act_res = mgr.activate_profile(new_id)
    assert act_res["success"] is True

    active = mgr.get_active_profile()
    assert active["id"] == new_id

    # Test deleting non-active custom profile
    to_delete_id = mgr.save_current_as_profile("To Delete", "dmr", "Disposable")
    deleted = mgr.delete_profile(to_delete_id)
    assert deleted is True
