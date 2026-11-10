"""
DVM Nexus - System Health and Resource Monitor
Supports Raspberry Pi 5 hardware metrics with cross-platform fallback and target clamping.
Target Hardware: Raspberry Pi 5 (Quad-core Cortex-A76 ARMv8-A, 2 GB LPDDR4X RAM, 64 GB MicroSD/NVMe storage).
"""

import os
import platform
import time
import shutil
import subprocess
from pathlib import Path
import psutil
from typing import Dict, Any, Optional, Tuple

# Target hardware specifications for Raspberry Pi 5
TARGET_PI5_NAME = "Raspberry Pi 5"
TARGET_PI5_CPU = "BCM2712 (Cortex-A76 4C)"
TARGET_PI5_RAM_GB = 2.0
TARGET_PI5_RAM_MB = 2048.0
TARGET_PI5_DISK_GB = 64.0

# In-memory cache for Windows WMI queries to prevent subshell overhead during fast polling
_win_temp_cache: Optional[Tuple[float, float]] = None  # (timestamp, temp_c)


def is_raspberry_pi_5() -> bool:
    """
    Check if the running system is a physical Raspberry Pi 5.
    Inspects Linux device-tree model and /proc/cpuinfo.
    """
    for model_path in [
        Path("/sys/firmware/devicetree/base/model"),
        Path("/proc/device-tree/model"),
    ]:
        try:
            if model_path.exists():
                text = model_path.read_text(encoding="utf-8", errors="ignore").lower()
                if "raspberry pi 5" in text:
                    return True
        except Exception:
            pass

    try:
        cpuinfo = Path("/proc/cpuinfo")
        if cpuinfo.exists():
            text = cpuinfo.read_text(encoding="utf-8", errors="ignore").lower()
            if "bcm2712" in text or "raspberry pi 5" in text:
                return True
    except Exception:
        pass

    return False


def get_cpu_temperature_c() -> Optional[float]:
    """
    Read CPU/SoC temperature in Celsius across Raspberry Pi 5, Linux sysfs, and Windows.
    On Raspberry Pi 5:
    - /sys/class/thermal/thermal_zone0/temp (and zone1/zone2) provides millidegrees Celsius
      (e.g. 45120 for 45.12 °C), which is divided by 1000.
    """
    global _win_temp_cache

    # Method 1: Dedicated Pi 5 /sys/class/thermal/thermal_zone0/temp check
    try:
        pi_zone0 = Path("/sys/class/thermal/thermal_zone0/temp")
        if pi_zone0.exists():
            val = float(pi_zone0.read_text(encoding="utf-8").strip())
            temp_c = val / 1000.0 if val > 200 else val
            if 10.0 <= temp_c <= 115.0:
                return round(temp_c, 1)
    except Exception:
        pass

    # Method 2: Comprehensive Linux sysfs thermal zone scan
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

    # Method 3: Official Raspberry Pi vcgencmd utility (firmware direct query)
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

    # Method 4: Windows ACPI WMI thermal query (with 2.5s caching)
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

    # Method 5: psutil sensors_temperatures fallback
    try:
        if hasattr(psutil, "sensors_temperatures"):
            temps = psutil.sensors_temperatures()
            if temps:
                for pref in ["bcm2712", "cpu_thermal", "bcm2835_thermal", "soc_thermal", "coretemp", "k10temp"]:
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


def get_cpu_temperature(unit: str = "F") -> Optional[float]:
    """
    Read CPU/SoC temperature in Fahrenheit (default) or Celsius.
    Converts Celsius to Fahrenheit using: (C * 9/5) + 32.
    """
    temp_c = get_cpu_temperature_c()
    if temp_c is None:
        return None
    if unit.upper() == "C":
        return round(temp_c, 1)
    temp_f = (temp_c * 9.0 / 5.0) + 32.0
    return round(temp_f, 1)


def get_memory_info() -> Tuple[float, float, float, str, str, str]:
    """
    Compute accurate used memory and clean display formatting.
    If running natively on Raspberry Pi 5, reads direct kernel memory (/proc/meminfo).
    If running in host/dev environment, clamps to target Raspberry Pi 5 specifications (2 GB LPDDR4X RAM).
    Returns: (used_mb, total_mb, percent, used_str, total_str, display_str)
    """
    on_pi = is_raspberry_pi_5()

    if on_pi:
        total_bytes = 0
        available_bytes = 0
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

        if total_bytes == 0:
            mem = psutil.virtual_memory()
            total_bytes = mem.total
            available_bytes = mem.available

        used_bytes = max(0, total_bytes - available_bytes)
        percent = round((used_bytes / total_bytes * 100.0), 1) if total_bytes > 0 else 0.0

        used_mb = round(used_bytes / (1024 * 1024), 1)
        total_mb = round(total_bytes / (1024 * 1024), 1)

        # Detect physical RAM module size (snaps e.g. 1.9 GB to 2 GB)
        usable_gb = total_bytes / (1024 ** 3)
        standard_sizes = [1, 2, 4, 8, 16]
        installed_gb = usable_gb
        for sz in standard_sizes:
            if 0.80 * sz <= usable_gb <= sz * 1.05:
                installed_gb = float(sz)
                break

        used_gb = used_bytes / (1024 ** 3)
        total_str = f"{installed_gb:.0f} GB"
        used_str = f"{used_gb:.1f} GB"
        display_str = f"{used_str} / {total_str} ({percent}%)"
        return used_mb, total_mb, percent, used_str, total_str, display_str

    else:
        # Dev host environment: Clamp telemetry to Raspberry Pi 5 specs (2 GB LPDDR4X)
        try:
            mem = psutil.virtual_memory()
            percent = round(mem.percent, 1)
        except Exception:
            percent = 25.0

        total_mb = TARGET_PI5_RAM_MB  # 2048.0 MB
        used_mb = round(total_mb * (percent / 100.0), 1)
        used_gb = round(TARGET_PI5_RAM_GB * (percent / 100.0), 1)
        total_str = "2 GB"
        used_str = f"{used_gb:.1f} GB"
        display_str = f"{used_str} / {total_str} ({percent}%)"
        return used_mb, total_mb, percent, used_str, total_str, display_str


def get_system_metrics() -> Dict[str, Any]:
    """Collect current system metrics for dashboard and SSE polling targeting Raspberry Pi 5."""
    cpu_percent = psutil.cpu_percent(interval=None)
    used_mb, total_mb, mem_percent, used_str, total_str, mem_display = get_memory_info()

    on_pi = is_raspberry_pi_5()

    if on_pi:
        disk = psutil.disk_usage("/")
        disk_total_gb = round(disk.total / (1024 ** 3), 1)
        disk_used_gb = round(disk.used / (1024 ** 3), 1)
        disk_percent = round(disk.percent, 1)
    else:
        # Dev host environment: Clamp storage telemetry to Raspberry Pi 5 specs (64 GB MicroSD/NVMe)
        try:
            raw_disk = psutil.disk_usage("C:\\" if os.name == "nt" else "/")
            disk_percent = round(raw_disk.percent, 1)
        except Exception:
            disk_percent = 15.0
        disk_total_gb = TARGET_PI5_DISK_GB  # 64.0 GB
        disk_used_gb = round(disk_total_gb * (disk_percent / 100.0), 1)

    # Temperature calculation in Fahrenheit: (C * 9/5) + 32
    temp_c = get_cpu_temperature_c()
    if temp_c is not None:
        temp_f = round((temp_c * 9.0 / 5.0) + 32.0, 1)
    else:
        # Default nominal Pi 5 operating temp when no sensor available (44.5 C = 112.1 F)
        temp_c = 44.5
        temp_f = round((44.5 * 9.0 / 5.0) + 32.0, 1)

    temp_display = f"{temp_f:.1f} °F"
    # Temperature progress bar fill percentage (mapped 32 F - 185 F to 0% - 100%)
    temp_pct = min(100.0, max(0.0, round(((temp_f - 32.0) / 153.0) * 100.0, 1)))

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

    cpu_model = TARGET_PI5_CPU  # "BCM2712 (Cortex-A76 4C)"
    temp_source = "BCM2712 SoC Thermal"

    return {
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "arch": "aarch64" if on_pi else f"{platform.machine()} (Target Pi 5)",
        "cpu_percent": round(cpu_percent, 1),
        "cpu_model": cpu_model,
        "cpu_temp_c": temp_c,
        "cpu_temp_f": temp_f,
        "cpu_temp_display": temp_display,
        "cpu_temp_percent": temp_pct,
        "temp_source": temp_source,
        "memory_used_mb": used_mb,
        "memory_total_mb": total_mb,
        "memory_percent": mem_percent,
        "memory_used_str": used_str,
        "memory_total_str": total_str,
        "memory_display": mem_display,
        "disk_used_gb": disk_used_gb,
        "disk_total_gb": disk_total_gb,
        "disk_percent": disk_percent,
        "uptime": uptime_str,
        "load_avg": load_avg,
    }
