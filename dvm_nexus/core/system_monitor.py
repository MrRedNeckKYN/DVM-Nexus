"""
DVM Nexus - System Health and Resource Monitor
Supports Raspberry Pi 5 hardware metrics with cross-platform fallback.
"""

import os
import platform
import time
import shutil
import subprocess
from pathlib import Path
import psutil
from typing import Dict, Any, Optional, Tuple

# In-memory cache for Windows WMI queries to prevent subshell overhead during fast polling
_win_temp_cache: Optional[Tuple[float, float]] = None  # (timestamp, temp_c)


def get_cpu_temperature() -> Optional[float]:
    """
    Read CPU/SoC temperature in Celsius accurately across Raspberry Pi 5 / 4 / 3,
    standard Linux distributions, and Windows ACPI thermal zones.
    """
    global _win_temp_cache

    # Method 1: Linux sysfs thermal zones (Raspberry Pi 5 / 4 / 3 and generic Linux)
    # Raspberry Pi 5 uses bcm2712 thermal zone (often thermal_zone1 or thermal_zone2)
    try:
        thermal_base = Path("/sys/class/thermal")
        if thermal_base.exists() and thermal_base.is_dir():
            candidates = []
            for zone_dir in thermal_base.glob("thermal_zone*"):
                temp_file = zone_dir / "temp"
                type_file = zone_dir / "type"
                if temp_file.exists():
                    try:
                        zone_type = type_file.read_text(encoding="utf-8").strip().lower() if type_file.exists() else ""
                    except Exception:
                        zone_type = ""
                    try:
                        val = float(temp_file.read_text(encoding="utf-8").strip())
                        temp_c = val / 1000.0 if val > 200 else val
                        if 10.0 <= temp_c <= 115.0:
                            priority = 0
                            if any(k in zone_type for k in ["bcm2712", "cpu", "soc", "pkg", "core"]):
                                priority = 3
                            elif "rp1" in zone_type:
                                priority = 1
                            else:
                                priority = 2
                            candidates.append((priority, temp_c))
                    except Exception:
                        pass
            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                return round(candidates[0][1], 1)
    except Exception:
        pass

    # Method 2: Official Raspberry Pi vcgencmd utility (firmware direct query)
    try:
        vcgencmd = shutil.which("vcgencmd") or "/usr/bin/vcgencmd"
        if os.path.exists(vcgencmd):
            res = subprocess.run([vcgencmd, "measure_temp"], capture_output=True, text=True, timeout=1.5)
            if res.returncode == 0 and "temp=" in res.stdout:
                raw = res.stdout.strip().replace("temp=", "").replace("'C", "")
                val = float(raw)
                if 10.0 <= val <= 115.0:
                    return round(val, 1)
    except Exception:
        pass

    # Method 3: Windows ACPI WMI thermal query (with 2.5s caching)
    if os.name == "nt":
        now = time.time()
        if _win_temp_cache and (now - _win_temp_cache[0]) < 2.5:
            return _win_temp_cache[1]
        try:
            cmd = [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                "(Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction SilentlyContinue).CurrentTemperature"
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=2.0)
            if res.returncode == 0 and res.stdout.strip():
                tokens = res.stdout.strip().split()
                if tokens:
                    raw = float(tokens[0])
                    # WMI returns temperature in tenths of Kelvin (e.g. 2932 for 293.2 K = 20.1 C)
                    celsius = (raw / 10.0) - 273.15
                    if 0.0 <= celsius <= 125.0:
                        temp_rounded = round(celsius, 1)
                        _win_temp_cache = (now, temp_rounded)
                        return temp_rounded
        except Exception:
            pass

    # Method 4: psutil sensors_temperatures fallback
    try:
        if hasattr(psutil, "sensors_temperatures"):
            temps = psutil.sensors_temperatures()
            if temps:
                for pref in ["cpu_thermal", "bcm2712", "bcm2835_thermal", "soc_thermal", "coretemp", "k10temp"]:
                    if pref in temps and temps[pref]:
                        for entry in temps[pref]:
                            if entry.current and 10.0 <= entry.current <= 115.0:
                                return round(entry.current, 1)
                for name, entries in temps.items():
                    for entry in entries:
                        if entry.current and 10.0 <= entry.current <= 115.0:
                            return round(entry.current, 1)
    except Exception:
        pass

    return None


def get_memory_info() -> Tuple[float, float, float, str, str, str]:
    """
    Compute accurate used memory and clean display formatting.
    Returns: (used_mb, total_mb, percent, used_str, total_str, display_str)
    """
    total_bytes = 0
    available_bytes = 0

    # On Linux, inspect /proc/meminfo directly for kernel-level accuracy
    meminfo_path = Path("/proc/meminfo")
    if meminfo_path.exists():
        try:
            info = {}
            for line in meminfo_path.read_text(encoding="utf-8").splitlines():
                parts = line.split(":")
                if len(parts) == 2:
                    key = parts[0].strip()
                    val_parts = parts[1].strip().split()
                    if val_parts:
                        info[key] = int(val_parts[0]) * 1024
            if "MemTotal" in info and "MemAvailable" in info:
                total_bytes = info["MemTotal"]
                available_bytes = info["MemAvailable"]
        except Exception:
            pass

    # Fallback to psutil virtual_memory
    if total_bytes == 0:
        mem = psutil.virtual_memory()
        total_bytes = mem.total
        available_bytes = mem.available

    used_bytes = max(0, total_bytes - available_bytes)
    percent = round((used_bytes / total_bytes * 100.0), 1) if total_bytes > 0 else 0.0

    used_mb = round(used_bytes / (1024 * 1024), 1)
    total_mb = round(total_bytes / (1024 * 1024), 1)

    # Detect installed physical RAM capacity by snapping to standard RAM module sizes
    usable_gb = total_bytes / (1024 ** 3)
    standard_sizes = [1, 2, 4, 8, 16, 32, 64, 128, 256]
    installed_gb = usable_gb
    for sz in standard_sizes:
        if 0.80 * sz <= usable_gb <= sz * 1.05:
            installed_gb = float(sz)
            break

    used_gb = used_bytes / (1024 ** 3)
    if installed_gb >= 1.0:
        total_str = f"{installed_gb:.0f} GB" if installed_gb == int(installed_gb) else f"{installed_gb:.1f} GB"
        used_str = f"{used_gb:.1f} GB"
        display_str = f"{used_str} / {total_str} ({percent}%)"
    else:
        total_str = f"{total_mb:.0f} MB"
        used_str = f"{used_mb:.0f} MB"
        display_str = f"{used_str} / {total_str} ({percent}%)"

    return used_mb, total_mb, percent, used_str, total_str, display_str


def get_system_metrics() -> Dict[str, Any]:
    """Collect current system metrics for dashboard and SSE polling."""
    cpu_percent = psutil.cpu_percent(interval=None)
    used_mb, total_mb, mem_percent, used_str, total_str, mem_display = get_memory_info()
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

    # Dynamic CPU & Thermal labels based on platform
    is_arm = platform.machine().lower().startswith(("arm", "aarch"))
    if is_arm:
        cpu_model = "Quad-Core Cortex-A76"
        temp_source = "BCM2712 SoC Thermal"
    elif os.name == "nt":
        cores = psutil.cpu_count(logical=False) or 4
        threads = psutil.cpu_count(logical=True) or 4
        cpu_model = f"{platform.machine()} {cores}C/{threads}T"
        temp_source = "ACPI Thermal Zone"
    else:
        cores = psutil.cpu_count(logical=False) or 4
        cpu_model = f"{platform.machine()} {cores}-Core"
        temp_source = "Hardware Thermal Zone"

    return {
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "cpu_percent": round(cpu_percent, 1),
        "cpu_model": cpu_model,
        "cpu_temp_c": temp_c,
        "cpu_temp_display": f"{temp_c:.1f} °C" if temp_c is not None else "N/A",
        "temp_source": temp_source,
        "memory_used_mb": used_mb,
        "memory_total_mb": total_mb,
        "memory_percent": mem_percent,
        "memory_used_str": used_str,
        "memory_total_str": total_str,
        "memory_display": mem_display,
        "disk_used_gb": round(disk.used / (1024 * 1024 * 1024), 1),
        "disk_total_gb": round(disk.total / (1024 * 1024 * 1024), 1),
        "disk_percent": round(disk.percent, 1),
        "uptime": uptime_str,
        "load_avg": load_avg,
    }
