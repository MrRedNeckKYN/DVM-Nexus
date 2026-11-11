"""
DVM Nexus - Atomic DVMHost REST API Client
Guarantees thread-safe token serialization to respect DVMHost's single-token-per-IP constraint.
"""

import os
import hashlib
import json
import logging
import threading
import time
from typing import Any, Dict, Optional
import requests
from dvm_nexus.core.config import settings

logger = logging.getLogger(__name__)


class DVMHostClient:
    """Thread-safe REST client for DVMHost."""

    def __init__(self, host: Optional[str] = None, port: Optional[int] = None, password: Optional[str] = None):
        self._host_override = host
        self._port_override = port
        self._password_override = password
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._session = requests.Session()

    def _sync_credentials_from_config(self):
        """Dynamically sync host, port, and password from active /opt/dvm/config.yml if present."""
        if self._host_override:
            self.host = self._host_override
        else:
            self.host = settings.dvmhost_api_host

        if self._port_override:
            self.port = self._port_override
        else:
            self.port = settings.dvmhost_api_port

        if self._password_override:
            self.password = self._password_override
        else:
            self.password = settings.dvmhost_api_password

        # Check active config
        try:
            from dvm_nexus.config_engine.dvm_yaml import dvm_config_manager
            conf = dvm_config_manager.load_config_dict()
            net = conf.get("network", {})
            if isinstance(net, dict):
                if not self._port_override and "restPort" in net:
                    self.port = int(net["restPort"])
                if not self._host_override and "restAddress" in net:
                    self.host = str(net["restAddress"])
                if not self._password_override and "restPassword" in net:
                    self.password = str(net["restPassword"])
        except Exception:
            pass

    @property
    def base_url(self) -> str:
        self._sync_credentials_from_config()
        return f"http://{self.host}:{self.port}"

    def authenticate(self) -> bool:
        """Authenticate with DVMHost and obtain a session token."""
        self._sync_credentials_from_config()
        hashed = hashlib.sha256(self.password.encode("utf-8")).hexdigest()
        try:
            resp = self._session.put(
                f"{self.base_url}/auth",
                json={"auth": hashed},
                headers={"Content-Type": "application/json"},
                timeout=2.0
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == 200 and "token" in data:
                    self._token = data["token"]
                    return True
            self._token = None
            return False
        except Exception as e:
            logger.debug(f"DVMHost auth error: {e}")
            self._token = None
            return False

    def request(self, method: str, endpoint: str, data: Optional[Dict[str, Any]] = None, retry_auth: bool = True) -> Dict[str, Any]:
        """Execute an atomic REST request with token locking and retry."""
        endpoint = "/" + endpoint.lstrip("/")
        with self._lock:
            if not self._token:
                if not self.authenticate():
                    return {"success": False, "error": "Authentication failed or DVMHost offline", "status": 503}

            headers = {
                "X-DVM-Auth-Token": self._token,
                "Content-Type": "application/json"
            }

            try:
                url = f"{self.base_url}{endpoint}"
                resp = self._session.request(
                    method=method.upper(),
                    url=url,
                    headers=headers,
                    json=data,
                    timeout=3.0
                )

                # DVMHost returns JSON with an internal status code
                try:
                    payload = resp.json()
                except Exception:
                    payload = {"raw": resp.text}

                # Check for 401 unauthorized
                if resp.status_code == 401 or (isinstance(payload, dict) and payload.get("status") == 401):
                    if retry_auth:
                        self._token = None
                        if self.authenticate():
                            headers["X-DVM-Auth-Token"] = self._token
                            retry_resp = self._session.request(
                                method=method.upper(),
                                url=url,
                                headers=headers,
                                json=data,
                                timeout=3.0
                            )
                            try:
                                retry_payload = retry_resp.json()
                            except Exception:
                                retry_payload = {"raw": retry_resp.text}
                            return {
                                "success": retry_resp.status_code in (200, 201),
                                "status_code": retry_resp.status_code,
                                "data": retry_payload
                            }
                    return {"success": False, "error": "Unauthorized (401)", "status": 401}

                return {
                    "success": resp.status_code in (200, 201),
                    "status_code": resp.status_code,
                    "data": payload
                }
            except requests.exceptions.ConnectionError:
                return {"success": False, "error": "Connection refused (DVMHost REST API not running)", "status": 503}
            except requests.exceptions.Timeout:
                return {"success": False, "error": "Request timed out", "status": 504}
            except Exception as e:
                return {"success": False, "error": str(e), "status": 500}

    # Convenient API wrappers
    def get_status(self) -> Dict[str, Any]:
        return self.request("GET", "/status")

    def get_version(self) -> Dict[str, Any]:
        return self.request("GET", "/version")

    def get_voice_channel(self) -> Dict[str, Any]:
        return self.request("GET", "/voice-ch")

    def set_mode(self, mode: str) -> Dict[str, Any]:
        """Set modem mode: 'idle', 'lockout', 'dmr', 'p25', 'nxdn'."""
        return self.request("PUT", "/mdm/mode", {"mode": mode})

    def dmr_rid_action(self, action: str, rid: int, slot: int = 1) -> Dict[str, Any]:
        """action: 'page', 'check', 'inhibit', 'uninhibit'"""
        return self.request("PUT", "/dmr/rid", {"cmd": action, "rid": rid, "slot": slot})

    def p25_rid_action(self, action: str, rid: int) -> Dict[str, Any]:
        """action: 'page', 'check', 'inhibit', 'uninhibit', 'dyn-regrp', 'unit-reg'"""
        return self.request("PUT", "/p25/rid", {"cmd": action, "rid": rid})

    def dmr_send_text(self, text: str, tg_or_rid: int, is_group: bool = True, slot: int = 1) -> Dict[str, Any]:
        """Send DMR Text Message (TMS/SMS) to talkgroup or individual subscriber."""
        endpoint = "/dmr/sms" if is_group else "/dmr/rid-sms"
        payload = {
            "text": text,
            "target": tg_or_rid,
            "is_group": is_group,
            "slot": slot
        }
        res = self.request("PUT", endpoint, payload)
        if not res.get("success"):
            if settings.debug or os.name == "nt":
                logger.info("[Simulated DMR TMS TX] Slot %d -> %s %d: '%s'", slot, "TG" if is_group else "RID", tg_or_rid, text)
                return {"success": True, "simulated": True, "target": tg_or_rid, "text": text}
            return {"success": False, "error": res.get("error", "DVMHost REST API unreachable"), "target": tg_or_rid, "text": text}
        return res

    def p25_send_text(self, text: str, tg_or_rid: int, is_group: bool = True) -> Dict[str, Any]:
        """Send P25 Text Message (PDU/TMS) to talkgroup or individual subscriber."""
        endpoint = "/p25/sms" if is_group else "/p25/rid-sms"
        payload = {
            "text": text,
            "target": tg_or_rid,
            "is_group": is_group
        }
        res = self.request("PUT", endpoint, payload)
        if not res.get("success"):
            if settings.debug or os.name == "nt":
                logger.info("[Simulated P25 TMS TX] -> %s %d: '%s'", "TG" if is_group else "Unit", tg_or_rid, text)
                return {"success": True, "simulated": True, "target": tg_or_rid, "text": text}
            return {"success": False, "error": res.get("error", "DVMHost REST API unreachable"), "target": tg_or_rid, "text": text}
        return res

    def get_dmr_affiliations(self) -> Dict[str, Any]:
        return self.request("GET", "/dmr/report-affiliations")

    def get_p25_affiliations(self) -> Dict[str, Any]:
        return self.request("GET", "/p25/report-affiliations")


dvm_client = DVMHostClient()
