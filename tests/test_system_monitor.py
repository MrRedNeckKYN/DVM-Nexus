"""
Unit tests for system_monitor hardware telemetry engine.
"""

import pytest
from unittest.mock import patch, MagicMock
from dvm_nexus.core.system_monitor import get_cpu_temperature, get_memory_info, get_system_metrics


def test_get_memory_info():
    used_mb, total_mb, percent, used_str, total_str, display_str = get_memory_info()
    assert total_mb > 0
    assert used_mb >= 0
    assert 0.0 <= percent <= 100.0
    assert "/" in display_str
    assert used_str in display_str
    assert total_str in display_str


def test_get_system_metrics():
    metrics = get_system_metrics()
    assert "cpu_percent" in metrics
    assert "cpu_temp_c" in metrics
    assert "cpu_temp_display" in metrics
    assert "memory_used_mb" in metrics
    assert "memory_total_mb" in metrics
    assert "memory_percent" in metrics
    assert "memory_display" in metrics
    assert "disk_used_gb" in metrics
    assert "uptime" in metrics
    assert isinstance(metrics["memory_percent"], float)
    assert 0.0 <= metrics["memory_percent"] <= 100.0


def test_cpu_temp_vcgencmd_mock():
    with patch("shutil.which", return_value="/usr/bin/vcgencmd"):
        with patch("os.path.exists", return_value=True):
            with patch("subprocess.run") as mock_sub:
                mock_sub.return_value = MagicMock(returncode=0, stdout="temp=46.2'C\n")
                temp = get_cpu_temperature()
                assert temp == 46.2
