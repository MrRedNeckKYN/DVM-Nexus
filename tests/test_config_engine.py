"""
Unit tests for Configuration Engine, Validation, Diffs, and Atomic Backups
"""

import pytest
from dvm_nexus.config_engine.network_validator import (
    validate_dmr_settings,
    validate_p25_settings,
    validate_radio_id,
    validate_talkgroup,
    validate_protocol_concurrency,
)
from dvm_nexus.config_engine.diff_engine import (
    generate_unified_diff,
    generate_structured_diff,
)
from dvm_nexus.config_engine.backup_manager import BackupManager
from dvm_nexus.config_engine.dvm_yaml import DVMConfigManager


def test_dmr_validation():
    # Valid DMR settings
    assert validate_dmr_settings({"colorCode": 1, "dmrNetId": 1, "siteId": 1}) == []
    # Invalid Color Code
    errs = validate_dmr_settings({"colorCode": 25})
    assert len(errs) == 1
    assert "between 0 and 15" in errs[0]


def test_p25_validation():
    # Valid P25 settings
    assert validate_p25_settings({
        "nac": "293",
        "netId": "BB800",
        "sysId": "001",
        "rfssId": 1,
        "siteId": 1,
        "channelId": 0,
        "channelNo": 1
    }) == []

    # Invalid NAC
    errs = validate_p25_settings({"nac": "INVALID_HEX"})
    assert len(errs) >= 1

    # Invalid Channel ID
    errs_ch = validate_p25_settings({"channelId": 20})
    assert any("Channel ID must be between 0 and 15" in e for e in errs_ch)


def test_radio_id_and_talkgroup_limits():
    assert validate_radio_id(1) is True
    assert validate_radio_id(16777215) is True
    assert validate_radio_id(0) is False
    assert validate_talkgroup(9) is True
    assert validate_talkgroup(0) is False


def test_diff_engine():
    old = "protocols:\n  dmr:\n    enable: true\n"
    new = "protocols:\n  dmr:\n    enable: false\n"
    diff_u = generate_unified_diff(old, new)
    assert "-    enable: true" in diff_u
    assert "+    enable: false" in diff_u

    diff_s = generate_structured_diff(old, new)
    types = [x["type"] for x in diff_s]
    assert "del" in types
    assert "add" in types


def test_atomic_backup_manager(tmp_path):
    dvm_dir = tmp_path / "dvm"
    backup_dir = tmp_path / "backups"
    mgr = BackupManager(backup_dir=backup_dir, dvm_dir=dvm_dir)

    target_file = dvm_dir / "test.yml"
    content = "test: 1234\n"
    mgr.atomic_write(target_file, content)

    assert target_file.exists()
    assert target_file.read_text() == content

    # Create backup snapshot
    snapshot = mgr.create_backup(summary="Test snapshot", changed_files=["test.yml"])
    assert snapshot.exists()
    assert (snapshot / "test.yml").exists()


def test_protocol_concurrency_safety_block():
    # Single protocol enabled is valid
    valid_cfg = {
        "protocols": {
            "dmr": {"enable": True},
            "p25": {"enable": False},
        }
    }
    assert validate_protocol_concurrency(valid_cfg) == []

    # Multi-protocol collision triggers safety error
    conflict_cfg = {
        "protocols": {
            "dmr": {"enable": True},
            "p25": {"enable": True},
        }
    }
    errs = validate_protocol_concurrency(conflict_cfg)
    assert len(errs) == 1
    assert "HS_NO_DUAL_MODE" in errs[0]
    assert "DMR, P25" in errs[0]


def test_dvm_config_manager_mutual_exclusion_enforcement(tmp_path):
    mgr = DVMConfigManager(dvm_dir=tmp_path)
    # Save a config attempting to enable both DMR and P25
    data = {
        "protocols": {
            "dmr": {"enable": True},
            "p25": {"enable": True},
        }
    }
    mgr.save_config_dict(data, summary="Test Save")
    loaded = mgr.load_config_dict()
    # DMR must remain True, P25 must be forced to False
    assert loaded["protocols"]["dmr"]["enable"] is True
    assert loaded["protocols"]["p25"]["enable"] is False
