"""
DVM Nexus - Modem & Hardware Configuration Router
Controls ADF7021 transceivers, UART serial port, and duplex/simplex RF parameters.
"""

import glob
import os
import asyncio
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse
from pathlib import Path

from dvm_nexus.core.templates import render_template
from dvm_nexus.config_engine.dvm_yaml import dvm_config_manager
from dvm_nexus.config_engine.network_validator import validate_serial_port, validate_int_range
from dvm_nexus.core.database import log_audit
from dvm_nexus.core.firmware_flasher import firmware_flasher

router = APIRouter()


@router.get("/modem", response_class=HTMLResponse)
async def modem_page(request: Request):
    """Render Modem & Hardware Configuration interface."""
    conf = dvm_config_manager.load_config_dict()
    sys_conf = conf.get("system", {})
    modem_conf = sys_conf.get("modem", {})
    proto_conf = modem_conf.get("protocol", {})
    uart_conf = proto_conf.get("uart", {})
    hotspot_conf = modem_conf.get("hotspot", {})

    available_ports = []
    if os.name != "nt":
        for pattern in ["/dev/ttyAMA*", "/dev/ttyUSB*", "/dev/ttyACM*", "/dev/serial*"]:
            available_ports.extend(glob.glob(pattern))
        available_ports = sorted(list(dict.fromkeys(available_ports)))
        configured_port = str(uart_conf.get("port", "/dev/ttyAMA0"))
        port_detected = os.path.exists(configured_port)
    else:
        # Development host (Windows): there is no serial device to detect; offer typical Pi names
        available_ports = ["/dev/ttyAMA0", "/dev/serial0", "/dev/ttyUSB0", "/dev/ttyACM0"]
        port_detected = True

    return render_template(
        request,
        "modem.html",
        {
            "title": "Modem & RF Configuration",
            "active_tab": "modem",
            "duplex": sys_conf.get("duplex", True),
            "uart_port": uart_conf.get("port", "/dev/ttyAMA0"),
            "uart_speed": uart_conf.get("speed", 115200),
            "available_ports": available_ports,
            "port_detected": port_detected,
            "any_ports_detected": bool(available_ports),
            "flasher": firmware_flasher.get_status(),
            "rx_invert": modem_conf.get("rxInvert", False),
            "tx_invert": modem_conf.get("txInvert", False),
            "ptt_invert": modem_conf.get("pttInvert", False),
            "rx_level": modem_conf.get("rxLevel", 50),
            "tx_level": modem_conf.get("txLevel", 50),
            "hotspot": hotspot_conf,
            "config_rev": dvm_config_manager.get_config_revision(),
        }
    )


@router.post("/modem/save", response_class=HTMLResponse)
async def modem_save(
    request: Request,
    duplex: bool = Form(True),
    uart_port: str = Form("/dev/ttyAMA0"),
    uart_speed: int = Form(115200),
    rx_invert: bool = Form(False),
    tx_invert: bool = Form(False),
    ptt_invert: bool = Form(False),
    rx_level: int = Form(50),
    tx_level: int = Form(50),
    adf_gain_mode: int = Form(2),
    afc_enable: bool = Form(True),
    tx_tuning: int = Form(0),
    rx_tuning: int = Form(0),
    dmr_disc_bw: int = Form(0),
    dmr_post_bw: int = Form(0),
    p25_disc_bw: int = Form(0),
    p25_post_bw: int = Form(0),
    config_rev: str = Form(""),
):
    """Save modem parameters to config.yml safely."""
    if config_rev and config_rev != dvm_config_manager.get_config_revision():
        return render_template(
            request,
            "components/toast.html",
            {"type": "error", "message": "Not saved: config.yml changed since this page was loaded (another tab/user, auto-repair or a restore). Reload the page to see the latest values, then re-apply your edit."}
        )

    errors = validate_serial_port(uart_port, uart_speed)
    errors += validate_int_range("RX level", rx_level, 0, 100)
    errors += validate_int_range("TX level", tx_level, 0, 100)
    if errors:
        return render_template(
            request,
            "components/toast.html",
            {"type": "error", "message": "Not saved - " + "; ".join(errors)}
        )

    conf = dvm_config_manager.load_config_dict()
    conf.setdefault("system", {})
    conf["system"]["duplex"] = duplex

    conf["system"].setdefault("modem", {})
    conf["system"]["modem"].setdefault("protocol", {})
    conf["system"]["modem"]["protocol"]["type"] = "uart"
    conf["system"]["modem"]["protocol"]["mode"] = "air"
    conf["system"]["modem"]["protocol"].setdefault("uart", {})
    conf["system"]["modem"]["protocol"]["uart"]["port"] = uart_port.strip()
    conf["system"]["modem"]["protocol"]["uart"]["speed"] = uart_speed

    conf["system"]["modem"]["rxInvert"] = rx_invert
    conf["system"]["modem"]["txInvert"] = tx_invert
    conf["system"]["modem"]["pttInvert"] = ptt_invert
    conf["system"]["modem"]["rxLevel"] = rx_level
    conf["system"]["modem"]["txLevel"] = tx_level

    conf["system"]["modem"].setdefault("hotspot", {})
    conf["system"]["modem"]["hotspot"]["adfGainMode"] = adf_gain_mode
    conf["system"]["modem"]["hotspot"]["afcEnable"] = afc_enable
    conf["system"]["modem"]["hotspot"]["txTuning"] = tx_tuning
    conf["system"]["modem"]["hotspot"]["rxTuning"] = rx_tuning
    conf["system"]["modem"]["hotspot"]["dmrDiscBWAdj"] = dmr_disc_bw
    conf["system"]["modem"]["hotspot"]["dmrPostBWAdj"] = dmr_post_bw
    conf["system"]["modem"]["hotspot"]["p25DiscBWAdj"] = p25_disc_bw
    conf["system"]["modem"]["hotspot"]["p25PostBWAdj"] = p25_post_bw

    dvm_config_manager.save_config_dict(conf, summary=f"Update Modem parameters (Port {uart_port})")
    log_audit("MODEM", "SAVE_CONFIG", f"Updated modem params: Port={uart_port}, Duplex={duplex}, GainMode={adf_gain_mode}")

    return render_template(
        request,
        "components/toast_saved.html",
        {"type": "success", "message": "Modem settings saved. Restart the radio to apply.", "config_rev": dvm_config_manager.get_config_revision()}
    )


@router.post("/api/modem/firmware/flash", response_class=HTMLResponse)
async def flash_firmware_endpoint(
    request: Request,
    target: str = Form("dvm_hs_dual_14mhz"),
    port: str = Form("/dev/ttyAMA0"),
):
    """Initiate background firmware flashing process."""
    asyncio.create_task(firmware_flasher.flash(target_key=target, port=port))
    status = firmware_flasher.get_status()
    return render_template(
        request,
        "components/firmware_modal.html",
        {"flasher": status, "selected_target": target, "selected_port": port}
    )


@router.get("/components/firmware-status", response_class=HTMLResponse)
async def firmware_status_component(request: Request):
    """HTMX polling endpoint for live firmware flashing progress."""
    status = firmware_flasher.get_status()
    return render_template(
        request,
        "components/firmware_modal.html",
        {"flasher": status}
    )

