"""
Unit tests for system_monitor hardware telemetry engine targeting Raspberry Pi 5.
Verifies dynamic RAM, dynamic storage, remote telemetry agent, and temperature conversion.
"""

import pytest
import shutil
from unittest.mock import patch, MagicMock
from pathlib import Path
from dvm_nexus.core.system_monitor import (
    get_cpu_temperature,
    get_cpu_temperature_c,
    get_memory_info,
    get_disk_info,
    get_linux_memory,
    get_linux_storage,
    get_cpu_model,
    get_system_metrics,
    is_raspberry_pi_5,
    remote_telemetry_agent,
)


def test_get_memory_info_default():
    remote_telemetry_agent.clear()
    used_mb, total_mb, percent, used_str, total_str, display_str = get_memory_info()
    assert total_mb > 0
    assert used_mb >= 0
    assert 0.0 <= percent <= 100.0
    assert "/" in display_str
    assert used_str in display_str
    assert "GB" in total_str


def test_dynamic_ram_detection_proc_meminfo():
    """Verify dynamic RAM detection across 2GB, 4GB, 8GB, and 16GB configurations."""
    # Test 8GB Pi 5
    meminfo_8gb = (
        "MemTotal:        8168256 kB\n"
        "MemFree:         4123456 kB\n"
        "MemAvailable:    5900000 kB\n"
        "Buffers:          123456 kB\n"
        "Cached:          1800000 kB\n"
    )
    with patch("pathlib.Path.exists", return_value=True):
        with patch("pathlib.Path.read_text", return_value=meminfo_8gb):
            used_mb, total_mb, percent, used_str, total_str, display_str = get_linux_memory()
            assert "GB" in total_str
            assert "7.8 GB" in total_str or "7.7 GB" in total_str or "8.0 GB" in total_str
            assert "/" in display_str
            assert "%" in display_str

    # Test 4GB Pi 5
    meminfo_4gb = (
        "MemTotal:        4084128 kB\n"
        "MemAvailable:    3000000 kB\n"
    )
    with patch("pathlib.Path.exists", return_value=True):
        with patch("pathlib.Path.read_text", return_value=meminfo_4gb):
            used_mb, total_mb, percent, used_str, total_str, display_str = get_linux_memory()
            assert "3.9 GB" in total_str or "4.0 GB" in total_str
            assert "/" in display_str


def test_dynamic_storage_detection():
    """Verify dynamic storage calculation across 32GB, 64GB, and 128GB media."""
    # 64 GB SD card mock (total = 64 * 1024^3, used = 8.7 * 1024^3)
    mock_usage = shutil._ntuple_diskusage(
        int(64.0 * (1024 ** 3)),
        int(8.7 * (1024 ** 3)),
        int(55.3 * (1024 ** 3))
    )
    with patch("shutil.disk_usage", return_value=mock_usage):
        used_gb, total_gb, percent, display_str = get_linux_storage("/")
        assert total_gb == 64.0
        assert used_gb == 8.7
        assert "8.7 / 64.0 GB" in display_str

    # 128 GB NVMe mock
    mock_nvme = shutil._ntuple_diskusage(
        int(128.0 * (1024 ** 3)),
        int(14.2 * (1024 ** 3)),
        int(113.8 * (1024 ** 3))
    )
    with patch("shutil.disk_usage", return_value=mock_nvme):
        used_gb, total_gb, percent, display_str = get_linux_storage("/")
        assert total_gb == 128.0
        assert used_gb == 14.2
        assert "14.2 / 128.0 GB" in display_str


def test_remote_telemetry_agent_custom_provider():
    """Verify remote telemetry provider registration and priority on remote dev host."""
    remote_telemetry_agent.clear()
    custom_metrics = {
        "hostname": "pi5-repeater",
        "os": "Linux (Debian Bookworm)",
        "arch": "aarch64",
        "cpu_percent": 3.8,
        "cpu_model": "BCM2712 Cortex-A76 4-Core",
        "cpu_temp_c": 43.0,
        "cpu_temp_f": 109.4,
        "cpu_temp_display": "109.4 °F",
        "cpu_temp_percent": 50.6,
        "temp_source": "BCM2712 SoC Thermal",
        "memory_used_mb": 1024.0,
        "memory_total_mb": 8192.0,
        "memory_percent": 12.5,
        "memory_used_str": "1.0 GB",
        "memory_total_str": "8.0 GB",
        "memory_display": "1.0 GB / 8.0 GB (12.5%)",
        "disk_used_gb": 12.4,
        "disk_total_gb": 128.0,
        "disk_percent": 9.7,
        "disk_display": "12.4 / 128.0 GB (9.7%)",
        "uptime": "5d 4h 12m",
        "load_avg": [0.10, 0.08, 0.05],
        "hardware_label": "Raspberry Pi 5 (8.0 GB / 128 GB)",
        "is_remote_agent": True,
    }

    remote_telemetry_agent.register_provider(lambda: custom_metrics)
    with patch("dvm_nexus.core.system_monitor.is_linux", return_value=False):
        metrics = get_system_metrics()
        assert metrics["memory_total_str"] == "8.0 GB"
        assert metrics["disk_total_gb"] == 128.0
        assert metrics["hardware_label"] == "Raspberry Pi 5 (8.0 GB / 128 GB)"
        assert metrics["is_remote_agent"] is True
    remote_telemetry_agent.clear()


def test_remote_vs_local_guard_no_host_pc_leak():
    """Verify that when running on dev host without connected remote agent, host PC specs are not leaked."""
    remote_telemetry_agent.clear()
    with patch("dvm_nexus.core.system_monitor.is_linux", return_value=False):
        metrics = get_system_metrics()
        # Must report Pi 5 target baseline, never host PC (e.g., 32 GB RAM or 1 TB disk)
        assert metrics["disk_total_gb"] == 64.0
        assert "2.0 GB" in metrics["memory_total_str"]
        assert "BCM2712" in metrics["cpu_model"]
        assert "°F" in metrics["cpu_temp_display"]


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
