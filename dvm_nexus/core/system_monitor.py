"""
DVM Nexus - System Health and Resource Monitor
Supports Raspberry Pi 5 hardware metrics with cross-platform fallback.
"""

import os
import platform
import time
import psutil
from typing import Dict, Any, Optional


def get_cpu_temperature() -> Optional[float]:
    """Read CPU temperature in Celsius, especially on Raspberry Pi."""
    # Method 1: Linux /sys filesystem (standard for Raspberry Pi)
    thermal_path = "/sys/class/thermal/thermal_zone0/temp"
    if os.path.exists(thermal_path):
        try:
            with open(thermal_path, "r") as f:
                temp_raw = f.read().strip()
                return round(float(temp_raw) / 1000.0, 1)
        except Exception:
            pass

    # Method 2: psutil sensors_temperatures if supported
    try:
        if hasattr(psutil, "sensors_temperatures"):
            temps = psutil.sensors_temperatures()
            if temps:
                for name, entries in temps.items():
                    for entry in entries:
                        if entry.current is not None and entry.current > 0:
                            return round(entry.current, 1)
    except Exception:
        pass

    # Fallback for dev/mock environments
    return None


def get_system_metrics() -> Dict[str, Any]:
    """Collect current system metrics for dashboard and SSE polling."""
    cpu_percent = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/") if os.name != "nt" else psutil.disk_usage("C:\\")
    temp_c = get_cpu_temperature()

    uptime_sec = int(time.time() - psutil.boot_time())
    days, rem = divmod(uptime_sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    uptime_str = f"{days}d {hours}h {minutes}m" if days > 0 else f"{hours}h {minutes}m"

    load_avg = []
    if hasattr(os, "getloadavg"):
        try:
            load_avg = [round(x, 2) for x in os.getloadavg()]
        except Exception:
            load_avg = []

    return {
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "cpu_percent": round(cpu_percent, 1),
        "cpu_temp_c": temp_c,
        "cpu_temp_display": f"{temp_c:.1f} °C" if temp_c is not None else "N/A",
        "memory_used_mb": round(mem.used / (1024 * 1024), 1),
        "memory_total_mb": round(mem.total / (1024 * 1024), 1),
        "memory_percent": round(mem.percent, 1),
        "disk_used_gb": round(disk.used / (1024 * 1024 * 1024), 1),
        "disk_total_gb": round(disk.total / (1024 * 1024 * 1024), 1),
        "disk_percent": round(disk.percent, 1),
        "uptime": uptime_str,
        "load_avg": load_avg,
    }
