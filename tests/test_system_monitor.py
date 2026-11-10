"""
Unit tests for system_monitor hardware telemetry engine targeting Raspberry Pi 5.
"""

import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path
from dvm_nexus.core.system_monitor import (
    get_cpu_temperature,
    get_cpu_temperature_c,
    get_memory_info,
    get_system_metrics,
    is_raspberry_pi_5,
)


def test_get_memory_info():
    used_mb, total_mb, percent, used_str, total_str, display_str = get_memory_info()
    assert total_mb == 2048.0 or total_mb > 0
    assert used_mb >= 0
    assert 0.0 <= percent <= 100.0
    assert "/" in display_str
    assert used_str in display_str
    assert "2 GB" in total_str or total_str.endswith("GB")


def test_get_system_metrics():
    metrics = get_system_metrics()
    assert "cpu_percent" in metrics
    assert "cpu_temp_c" in metrics
    assert "cpu_temp_f" in metrics
    assert "cpu_temp_display" in metrics
    assert "°F" in metrics["cpu_temp_display"]
    assert "memory_used_mb" in metrics
    assert "memory_total_mb" in metrics
    assert "memory_percent" in metrics
    assert "memory_display" in metrics
    assert "disk_used_gb" in metrics
    assert "disk_total_gb" in metrics
    assert metrics["disk_total_gb"] == 64.0
    assert "BCM2712" in metrics["cpu_model"]
    assert "uptime" in metrics
    assert isinstance(metrics["memory_percent"], float)
    assert 0.0 <= metrics["memory_percent"] <= 100.0


def test_cpu_temp_vcgencmd_mock():
    with patch("shutil.which", return_value="/usr/bin/vcgencmd"):
        with patch("os.path.exists", return_value=True):
            with patch("subprocess.run") as mock_sub:
                mock_sub.return_value = MagicMock(returncode=0, stdout="temp=46.2'C\n")
                temp_c = get_cpu_temperature("C")
                assert temp_c == 46.2
                temp_f = get_cpu_temperature("F")
                # (46.2 * 9/5) + 32 = 115.16 -> 115.2 °F
                assert temp_f == 115.2


def test_pi5_thermal_zone_millidegrees():
    """Verify /sys/class/thermal/thermal_zone0/temp in millidegrees C converts correctly to °F."""
    with patch("pathlib.Path.exists", return_value=True):
        with patch("pathlib.Path.read_text", return_value="42500\n"):
            temp_c = get_cpu_temperature_c()
            assert temp_c == 42.5
            temp_f = get_cpu_temperature("F")
            # (42.5 * 9/5) + 32 = 76.5 + 32 = 108.5 °F
            assert temp_f == 108.5
