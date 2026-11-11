"""
DVM Nexus - Embedded Modem Firmware Flasher Engine
Automates flashing DVMProject dvmfirmware-hs and standard MMDVM_HS binaries
to STM32-based Raspberry Pi GPIO duplex/simplex hats using stm32flash and pinctrl.
"""

import os
import sys
import time
import shutil
import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional

from dvm_nexus.core.config import settings
from dvm_nexus.core.database import log_audit
from dvm_nexus.core.service_controller import service_controller
from dvm_nexus.core.system_monitor import is_linux

logger = logging.getLogger(__name__)

# Firmware targets and profiles
FIRMWARE_TARGETS = {
    "dvm_hs_dual_14mhz": {
        "name": "DVMProject dvmfirmware-hs (Duplex Hat 14.7456 MHz)",
        "description": "Recommended for Aursinc, BI7JTA, and dual-ADF7021 duplex hotspot hats.",
        "repo": "https://github.com/DVMProject/dvmfirmware-hs.git",
        "make_target": "mmdvm-hs-hat-dual",
        "tcxo_mhz": "14.7456",
        "type": "dvm",
    },
    "dvm_hs_dual_12mhz": {
        "name": "DVMProject dvmfirmware-hs (Duplex Hat 12.2880 MHz)",
        "description": "For dual-antenna hats equipped with 12.288 MHz TCXO crystal oscillator.",
        "repo": "https://github.com/DVMProject/dvmfirmware-hs.git",
        "make_target": "mmdvm-hs-hat-dual-12mhz",
        "tcxo_mhz": "12.288",
        "type": "dvm",
    },
    "dvm_hs_simplex_14mhz": {
        "name": "DVMProject dvmfirmware-hs (Simplex Hat 14.7456 MHz)",
        "description": "For single-antenna simplex MMDVM hotspot hats with 14.7456 MHz TCXO.",
        "repo": "https://github.com/DVMProject/dvmfirmware-hs.git",
        "make_target": "mmdvm-hs-hat",
        "tcxo_mhz": "14.7456",
        "type": "dvm",
    },
    "mmdvm_hs_dual_wpsd": {
        "name": "Standard MMDVM_HS Dual Hat (WPSD / Pi-Star Rollback)",
        "description": "Revert to standard CA6JAU/W0CHP MMDVM_HS firmware for Pi-Star compatibility.",
        "repo": "https://github.com/juribeparada/MMDVM_HS.git",
        "make_target": "mmdvm-hs-hat-dual",
        "tcxo_mhz": "14.7456",
        "type": "mmdvm",
    },
}


class FirmwareFlasher:
    """Manages compilation, toolchain verification, GPIO strobe, and stm32flash execution."""

    def __init__(self):
        self.state: str = "idle"  # idle, flashing, completed, failed
        self.current_target: Optional[str] = None
        self.logs: List[str] = []
        self.firmware_dir = settings.dvm_dir / "firmware"
        self._lock = asyncio.Lock()

    def get_status(self) -> Dict[str, Any]:
        """Return current flasher state, target options, and recent log output."""
        return {
            "state": self.state,
            "current_target": self.current_target,
            "has_stm32flash": shutil.which("stm32flash") is not None or not is_linux(),
            "has_pinctrl": shutil.which("pinctrl") is not None or not is_linux(),
            "targets": FIRMWARE_TARGETS,
            "logs": self.logs[-40:],
        }

    def _append_log(self, msg: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        entry = f"[{timestamp}] {msg}"
        self.logs.append(entry)
        logger.info(entry)

    async def flash(self, target_key: str = "dvm_hs_dual_14mhz", port: str = "/dev/ttyAMA0") -> Dict[str, Any]:
        """
        Execute full automated firmware build and flash sequence:
        1. Stop dvmhost.service to release the UART port.
        2. Verify/install stm32flash, git, and gcc-arm-none-eabi.
        3. Clone/build dvmfirmware-hs repository.
        4. Strobe GPIO 20 (BOOT0) and GPIO 21 (RESET) to enter STM32 ROM bootloader.
        5. Program flash with stm32flash and verify checksum.
        6. Release GPIO 20/21 into run mode.
        7. Restart dvmhost.service.
        """
        if self._lock.locked():
            return {"success": False, "message": "A firmware flash operation is already in progress."}

        async with self._lock:
            self.state = "flashing"
            self.current_target = target_key
            self.logs = []
            target = FIRMWARE_TARGETS.get(target_key, FIRMWARE_TARGETS["dvm_hs_dual_14mhz"])

            self._append_log(f"Initiating firmware installation: {target['name']}")
            log_audit("FIRMWARE", "FLASH_START", f"Target: {target_key} on {port}")

            if not is_linux():
                # Dev simulation mode
                self._append_log("[Dev Host Simulation] Simulating stm32flash operation on Windows...")
                await asyncio.sleep(1.0)
                self._append_log("Simulated: Stopping dvmhost.service")
                await asyncio.sleep(0.5)
                self._append_log("Simulated: Strobe GPIO 20/21 into STM32 ROM bootloader mode")
                await asyncio.sleep(1.0)
                self._append_log("Simulated: stm32flash -v -w bin/dvm-firmware-hs_f1.bin -b 115200")
                self._append_log("Simulated: Write 48296 bytes (100% verified)")
                await asyncio.sleep(0.5)
                self._append_log("Simulated: Restarting dvmhost.service")
                self.state = "completed"
                log_audit("FIRMWARE", "FLASH_SUCCESS", f"Simulated flash of {target_key}")
                return {"success": True, "message": "Firmware flash completed successfully (simulated dev mode)."}

            try:
                # 1. Stop dvmhost service to release /dev/ttyAMA0
                self._append_log("Stopping dvmhost service to release serial port...")
                service_controller.stop()
                await asyncio.sleep(1.0)

                # 2. Check and install prerequisite tools
                if not shutil.which("stm32flash") or not shutil.which("arm-none-eabi-gcc"):
                    self._append_log("Installing required flashing tools (stm32flash, gcc-arm-none-eabi)...")
                    cmd_pkg = ["sudo", "apt-get", "update", "-y"]
                    p1 = await asyncio.create_subprocess_exec(*cmd_pkg, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    await p1.communicate()

                    cmd_install = ["sudo", "apt-get", "install", "-y", "stm32flash", "gcc-arm-none-eabi", "git", "make"]
                    p2 = await asyncio.create_subprocess_exec(*cmd_install, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    await p2.communicate()

                if not shutil.which("stm32flash"):
                    self._append_log("Error: stm32flash utility could not be found or installed.")
                    self.state = "failed"
                    return {"success": False, "message": "stm32flash utility not found."}

                # 3. Clone / Update dvmfirmware-hs repository
                build_dir = Path("/tmp/dvm_firmware_build")
                if build_dir.exists():
                    shutil.rmtree(build_dir, ignore_errors=True)
                build_dir.mkdir(parents=True, exist_ok=True)

                self._append_log(f"Cloning {target['repo']}...")
                cmd_clone = ["git", "clone", "--recurse-submodules", "--depth", "1", target["repo"], str(build_dir)]
                p_clone = await asyncio.create_subprocess_exec(*cmd_clone, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                _, err_clone = await p_clone.communicate()
                if p_clone.returncode != 0:
                    self._append_log(f"Git clone error: {err_clone.decode(errors='replace')}")
                    self.state = "failed"
                    return {"success": False, "message": "Failed to clone firmware repository."}

                # 4. Prepare Defines.h / Config.h and build binary
                if "dual" in target_key.lower():
                    defines_h = build_dir / "Defines.h"
                    if defines_h.exists():
                        d_txt = defines_h.read_text(encoding="utf-8", errors="ignore")
                        d_txt = d_txt.replace("// #define DUPLEX", "#define DUPLEX").replace("//#define DUPLEX", "#define DUPLEX")
                        if "#define DUPLEX" not in d_txt:
                            d_txt = "#define DUPLEX\n" + d_txt
                        defines_h.write_text(d_txt, encoding="utf-8")
                        self._append_log("Enforced #define DUPLEX in Defines.h for dual ADF7021 hardware.")

                    cfg_src = build_dir / "configs" / "MMDVM_HS_Dual_Hat.h"
                    if not cfg_src.exists() and (build_dir / "configs").exists():
                        for cand in (build_dir / "configs").glob("*Dual*.h"):
                            cfg_src = cand
                            break
                    if cfg_src.exists():
                        shutil.copyfile(cfg_src, build_dir / "Config.h")
                        self._append_log(f"Applied configuration template: {cfg_src.name}")

                    config_h = build_dir / "Config.h"
                    if config_h.exists():
                        c_txt = config_h.read_text(encoding="utf-8", errors="ignore")
                        c_txt = c_txt.replace("//#define DUPLEX", "#define DUPLEX").replace("// #define DUPLEX", "#define DUPLEX")
                        if "#define DUPLEX" not in c_txt:
                            c_txt = "#define DUPLEX\n" + c_txt
                        config_h.write_text(c_txt, encoding="utf-8")
                        self._append_log("Enforced #define DUPLEX in Config.h for dual ADF7021 hardware.")

                # Clean prior build artifacts
                cmd_clean = ["make", "-f", "Makefile.STM32FX", "clean"]
                p_clean = await asyncio.create_subprocess_exec(
                    *cmd_clean,
                    cwd=str(build_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await p_clean.communicate()

                self._append_log(f"Compiling target '{target['make_target']}'...")
                cmd_make = ["make", "-f", "Makefile.STM32FX", target["make_target"]]
                p_make = await asyncio.create_subprocess_exec(
                    *cmd_make,
                    cwd=str(build_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out_make, err_make = await p_make.communicate()
                if p_make.returncode != 0:
                    self._append_log(f"Compilation warning/log: {err_make.decode(errors='replace')[:200]}")

                # Locate compiled binary (prioritizing newest dual binary)
                bin_candidates = sorted(
                    list(build_dir.glob("bin/*.bin")) + list(build_dir.glob("*.bin")),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True
                )
                if not bin_candidates:
                    self._append_log("Error: No compiled .bin firmware binary was found after compilation.")
                    self.state = "failed"
                    return {"success": False, "message": "Compilation produced no binary."}

                if "dual" in target_key.lower():
                    dual_bins = [b for b in bin_candidates if "dual" in b.name.lower()]
                    bin_file = dual_bins[0] if dual_bins else bin_candidates[0]
                else:
                    bin_file = bin_candidates[0]

                self._append_log(f"Firmware binary ready: {bin_file.name} ({bin_file.stat().st_size} bytes)")

                # 5. Strobe STM32 into ROM bootloader mode via Raspberry Pi GPIO 20 and 21
                self._append_log("Strobing GPIO 20 (BOOT0) and GPIO 21 (RESET) into bootloader mode...")
                if shutil.which("pinctrl"):
                    subprocess.run(["pinctrl", "set", "20", "op", "dh"], check=False)  # BOOT0 = High
                    subprocess.run(["pinctrl", "set", "21", "op", "dl"], check=False)  # RESET = Low
                    await asyncio.sleep(0.3)
                    subprocess.run(["pinctrl", "set", "21", "op", "dh"], check=False)  # RESET = High (Enter ROM)
                    await asyncio.sleep(0.4)

                # 6. Flash using stm32flash (using direct serial communication since pinctrl manages GPIO)
                self._append_log(f"Flashing {bin_file.name} to {port} at 115200 baud...")
                cmd_flash = [
                    "sudo", "stm32flash",
                    "-v",
                    "-w", str(bin_file),
                    "-b", "115200",
                    port,
                ]
                p_flash = await asyncio.create_subprocess_exec(
                    *cmd_flash,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout_flash, stderr_flash = await p_flash.communicate()
                out_str = stdout_flash.decode("utf-8", errors="replace") + stderr_flash.decode("utf-8", errors="replace")

                for line in out_str.splitlines():
                    if any(k in line.lower() for k in ["version", "interface", "erasing", "writing", "wroten", "bytes", "verified", "done"]):
                        self._append_log(f"stm32flash: {line.strip()}")

                if p_flash.returncode != 0:
                    self._append_log(f"stm32flash failed with exit code {p_flash.returncode}: {out_str[:250]}")
                    self.state = "failed"
                    # Restore GPIO run mode
                    if shutil.which("pinctrl"):
                        subprocess.run(["pinctrl", "set", "20", "op", "dl"], check=False)
                        subprocess.run(["pinctrl", "set", "21", "op", "dh"], check=False)
                    return {"success": False, "message": "stm32flash programming failed."}

                self._append_log("Programming completed and verified successfully!")

                # 7. Restore normal run mode pins
                self._append_log("Resetting STM32 into normal execution mode (BOOT0=Low, RESET=High)...")
                if shutil.which("pinctrl"):
                    subprocess.run(["pinctrl", "set", "20", "op", "dl"], check=False)
                    subprocess.run(["pinctrl", "set", "21", "op", "dl"], check=False)
                    await asyncio.sleep(0.2)
                    subprocess.run(["pinctrl", "set", "21", "op", "dh"], check=False)
                    await asyncio.sleep(0.5)

                # 8. Start dvmhost with safety pre-flight check
                self._append_log("Pre-flight safety audit: Verifying single-protocol mutual exclusion in /opt/dvm/config.yml...")
                service_controller.preflight_safety_check()
                self._append_log("Starting dvmhost service...")
                service_controller.start()
                await asyncio.sleep(2.0)

                self.state = "completed"
                log_audit("FIRMWARE", "FLASH_SUCCESS", f"Successfully flashed {target_key} to {port}")
                return {"success": True, "message": "DVMProject firmware installed and active!"}

            except Exception as exc:
                self._append_log(f"Unhandled error during firmware flash: {str(exc)}")
                self.state = "failed"
                return {"success": False, "message": str(exc)}
            finally:
                # Ensure pins are always left in run mode
                if shutil.which("pinctrl"):
                    subprocess.run(["pinctrl", "set", "20", "op", "dl"], check=False)
                    subprocess.run(["pinctrl", "set", "21", "op", "dh"], check=False)


firmware_flasher = FirmwareFlasher()
