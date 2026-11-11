"""
DVM Nexus - Modem & Hardware Configuration Router
Controls ADF7021 transceivers, UART serial port, and duplex/simplex RF parameters.
"""

import glob
import os
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse
from pathlib import Path

from dvm_nexus.core.templates import render_template
from dvm_nexus.config_engine.dvm_yaml import dvm_config_manager
from dvm_nexus.core.database import log_audit

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
    if not available_ports:
        available_ports = ["/dev/ttyAMA0", "/dev/serial0", "/dev/ttyUSB0", "/dev/ttyACM0"]
    available_ports = sorted(list(dict.fromkeys(available_ports)))

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
            "rx_invert": modem_conf.get("rxInvert", False),
            "tx_invert": modem_conf.get("txInvert", False),
            "ptt_invert": modem_conf.get("pttInvert", False),
            "rx_level": modem_conf.get("rxLevel", 50),
            "tx_level": modem_conf.get("txLevel", 50),
            "hotspot": hotspot_conf,
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
):
    """Save modem parameters to config.yml safely."""
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
        "components/toast.html",
        {"type": "success", "message": "Modem settings saved successfully! Restart service to apply."}
    )
