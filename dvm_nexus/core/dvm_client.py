"""
DVM Nexus - Atomic DVMHost REST API Client
Guarantees thread-safe token serialization to respect DVMHost's single-token-per-IP constraint.
"""

import os
import hashlib
import json
import logging
import socket
import threading
import time
from typing import Any, Dict, Optional, Tuple
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

    def invalidate_token(self) -> None:
        """Clear cached authentication token (e.g. when dvmhost starts/restarts with a new PID)."""
        self._token = None

    def _raw_http_request(
        self,
        method: str,
        endpoint: str,
        headers: Optional[Dict[str, str]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: float = 2.5,
    ) -> Tuple[int, str]:
        """
        Send a single-write HTTP/1.0 request to DVMHost's embedded Asio HTTP server.
        Why single-write HTTP/1.0:
          1. DVMHost's ServerConnection.h is non-persistent HTTP/1.0 (m_persistent = false).
          2. Python's http.client / requests splits headers and body into two separate socket.send()
             calls; on PUT/POST requests, if DVMHost's async_read_some reads headers before the body
             arrives (consumed == recvLength), ServerConnection.h enters a broken continuation state
             that hangs until client timeout and logs 'E: (RESTAPI) ServerConnection::read(), End of file, code = 2'.
          3. Combining request line + headers + body into a single sock.sendall() guarantees DVMHost
             receives the complete frame in one read without ever hitting EOF on a second read.
        """
        self._sync_credentials_from_config()
        path = "/" + endpoint.lstrip("/")
        body_bytes = b""
        if json_body is not None:
            body_bytes = json.dumps(json_body, separators=(",", ":")).encode("utf-8")

        req_headers: Dict[str, str] = {
            "Host": f"{self.host}:{self.port}",
            "User-Agent": "DVM-Nexus/1.0",
            "Accept": "*/*",
            "Connection": "close",
        }
        if headers:
            req_headers.update(headers)
        if json_body is not None or method.upper() in ("PUT", "POST"):
            req_headers["Content-Type"] = "application/json"
            req_headers["Content-Length"] = str(len(body_bytes))

        header_lines = [f"{method.upper()} {path} HTTP/1.0"]
        for k, v in req_headers.items():
            header_lines.append(f"{k}: {v}")
        header_block = ("\r\n".join(header_lines) + "\r\n\r\n").encode("ascii")
        packet = header_block + body_bytes

        with socket.create_connection((self.host, int(self.port)), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(packet)
            chunks = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)

        raw_resp = b"".join(chunks).decode("utf-8", errors="replace")
        if not raw_resp:
            raise ConnectionError("Empty HTTP response from DVMHost REST API")

        head_part, _, body_part = raw_resp.partition("\r\n\r\n")
        status_line = head_part.splitlines()[0] if head_part else ""
        parts = status_line.split(" ", 2)
        status_code = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 500
        return status_code, body_part

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
            status_code, body_text = self._raw_http_request(
                method="PUT",
                endpoint="/auth",
                json_body={"auth": hashed},
                timeout=2.0,
            )
            if status_code == 200:
                data = json.loads(body_text)
                if isinstance(data, dict) and data.get("status") == 200 and "token" in data:
                    self._token = str(data["token"])
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
                "X-DVM-Auth-Token": str(self._token),
            }

            try:
                status_code, body_text = self._raw_http_request(
                    method=method.upper(),
                    endpoint=endpoint,
                    headers=headers,
                    json_body=data,
                    timeout=3.0,
                )

                # DVMHost returns JSON with an internal status code
                try:
                    payload = json.loads(body_text)
                except Exception:
                    payload = {"raw": body_text}

                # Check for 401 unauthorized or invalid token
                has_token_err = False
                if status_code == 401:
                    has_token_err = True
                elif isinstance(payload, dict):
                    p_status = payload.get("status")
                    msg = str(payload.get("message", "")).lower()
                    if p_status in (401, 403) or "illegal authentication token" in msg or "invalid token" in msg or "expired" in msg:
                        has_token_err = True

                if has_token_err:
                    if retry_auth:
                        logger.debug("Stale or illegal DVMHost token detected; re-authenticating...")
                        self._token = None
                        if self.authenticate():
                            headers["X-DVM-Auth-Token"] = str(self._token)
                            retry_code, retry_body = self._raw_http_request(
                                method=method.upper(),
                                endpoint=endpoint,
                                headers=headers,
                                json_body=data,
                                timeout=3.0,
                            )
                            try:
                                retry_payload = json.loads(retry_body)
                            except Exception:
                                retry_payload = {"raw": retry_body}
                            return {
                                "success": retry_code in (200, 201) and (
                                    not isinstance(retry_payload, dict) or retry_payload.get("status") in (None, 200, 201)
                                ),
                                "status_code": retry_code,
                                "data": retry_payload
                            }
                    return {"success": False, "error": "Unauthorized / Stale Token (401)", "status": 401}

                return {
                    "success": status_code in (200, 201),
                    "status_code": status_code,
                    "data": payload
                }
            except socket.timeout:
                return {"success": False, "error": "Request timed out", "status": 504}
            except (ConnectionError, OSError):
                return {"success": False, "error": "Connection refused (DVMHost REST API not running)", "status": 503}
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
