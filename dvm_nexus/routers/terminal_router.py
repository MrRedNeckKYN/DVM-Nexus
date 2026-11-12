"""
DVM Nexus - Web Terminal Router
Interactive Web Terminal and Command Runner for Raspberry Pi and Linux diagnostics.
Features quick-action hardware diagnostic pills, interactive command shell, and audit logging.
"""

import os
import sys
import time
import shutil
import asyncio
import subprocess
import logging
from pathlib import Path
from typing import Dict, Any, Optional

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from dvm_nexus.core.templates import render_template
from dvm_nexus.core.database import log_audit
from dvm_nexus.core.system_monitor import is_linux, is_raspberry_pi_5

logger = logging.getLogger(__name__)

router = APIRouter(tags=["terminal"])

# Track persistent working directory for the web session
_current_working_dir: str = "/opt/dvm" if is_linux() and Path("/opt/dvm").exists() else str(Path.cwd())


class CommandRequest(BaseModel):
    command: str
    cwd: Optional[str] = None


@router.get("/terminal", response_class=HTMLResponse)
async def terminal_page(request: Request):
    """Render the interactive Web Terminal interface."""
    global _current_working_dir

    username = "root" if (os.name != "nt" and os.geteuid() == 0) else os.getenv("USER", os.getenv("USERNAME", "pi"))
    hostname = os.getenv("HOSTNAME", "raspberrypi")
    shell_name = os.getenv("SHELL", "/bin/bash" if is_linux() else "powershell.exe")

    return render_template(
        request,
        "terminal.html",
        {
            "active_tab": "terminal",
            "title": "Web Terminal",
            "terminal_user": username,
            "terminal_host": hostname,
            "terminal_shell": shell_name,
            "current_cwd": _current_working_dir,
            "is_pi": is_raspberry_pi_5(),
            "is_linux": is_linux(),
        },
    )


@router.post("/api/terminal/exec")
async def execute_command(req: CommandRequest):
    """
    Execute a shell command asynchronously and return output, returncode, and updated cwd.
    Supports directory navigation (e.g. 'cd /opt/dvm').
    """
    global _current_working_dir

    raw_cmd = req.command.strip()
    if not raw_cmd:
        return JSONResponse({
            "command": "",
            "stdout": "",
            "stderr": "",
            "exit_code": 0,
            "cwd": _current_working_dir,
            "duration_ms": 0,
        })

    # Directory change handling (cd)
    if raw_cmd.startswith("cd ") or raw_cmd == "cd":
        parts = raw_cmd.split(maxsplit=1)
        target = parts[1].strip() if len(parts) > 1 else str(Path.home())
        target_path = Path(target)
        if not target_path.is_absolute():
            target_path = (Path(_current_working_dir) / target).resolve()
        
        if target_path.exists() and target_path.is_dir():
            _current_working_dir = str(target_path)
            log_audit("TERMINAL", "CD", f"Changed directory to {_current_working_dir}")
            return JSONResponse({
                "command": raw_cmd,
                "stdout": f"Directory changed to: {_current_working_dir}\n",
                "stderr": "",
                "exit_code": 0,
                "cwd": _current_working_dir,
                "duration_ms": 1,
            })
        else:
            return JSONResponse({
                "command": raw_cmd,
                "stdout": "",
                "stderr": f"cd: no such directory: {target}\n",
                "exit_code": 1,
                "cwd": _current_working_dir,
                "duration_ms": 1,
            })

    # Check execution working directory
    exec_cwd = req.cwd or _current_working_dir
    if not Path(exec_cwd).exists():
        exec_cwd = str(Path.cwd())
        _current_working_dir = exec_cwd

    start_time = time.perf_counter()
    log_audit("TERMINAL", "EXEC", raw_cmd[:120])

    try:
        # Determine shell
        if is_linux():
            shell_cmd = ["/bin/bash", "-c", raw_cmd]
        else:
            shell_cmd = ["powershell.exe", "-NoProfile", "-Command", raw_cmd]

        proc = await asyncio.create_subprocess_exec(
            *shell_cmd,
            cwd=exec_cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            # 30 second timeout to protect server from runaway commands
            stdout_data, stderr_data = await asyncio.wait_for(proc.communicate(), timeout=30.0)
            stdout_str = stdout_data.decode("utf-8", errors="replace")
            stderr_str = stderr_data.decode("utf-8", errors="replace")
            exit_code = proc.returncode
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            stdout_str = ""
            stderr_str = "Error: Command timed out after 30 seconds.\n"
            exit_code = 124

    except Exception as exc:
        stdout_str = ""
        stderr_str = f"Execution error: {str(exc)}\n"
        exit_code = 1

    duration_ms = round((time.perf_counter() - start_time) * 1000.0, 1)

    return JSONResponse({
        "command": raw_cmd,
        "stdout": stdout_str,
        "stderr": stderr_str,
        "exit_code": exit_code,
        "cwd": _current_working_dir,
        "duration_ms": duration_ms,
    })


@router.websocket("/ws/terminal")
async def websocket_terminal(websocket: WebSocket):
    """
    WebSocket endpoint for live interactive terminal streaming.
    Provides full interactive terminal sessions on Linux via pty or subprocess streaming.
    """
    await websocket.accept()
    global _current_working_dir

    if is_linux():
        import pty
        import termios
        import fcntl

        # Fork pseudo-terminal
        master_fd, slave_fd = pty.openpty()

        # Set non-blocking on master_fd
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        shell_path = "/bin/bash" if Path("/bin/bash").exists() else "/bin/sh"
        proc = await asyncio.create_subprocess_exec(
            shell_path,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=_current_working_dir,
            env={**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"},
        )
        os.close(slave_fd)

        async def read_from_pty():
            loop = asyncio.get_running_loop()
            try:
                while proc.returncode is None:
                    # Wait until master_fd is readable
                    await loop.run_in_executor(None, lambda: fcntl.fcntl(master_fd, fcntl.F_GETFL))
                    try:
                        data = os.read(master_fd, 4096)
                        if data:
                            await websocket.send_text(data.decode("utf-8", errors="replace"))
                        else:
                            break
                    except (BlockingIOError, InterruptedError):
                        await asyncio.sleep(0.05)
            except Exception:
                pass

        async def write_to_pty():
            try:
                while True:
                    msg = await websocket.receive_text()
                    if msg:
                        os.write(master_fd, msg.encode("utf-8"))
            except WebSocketDisconnect:
                pass
            except Exception:
                pass

        task_read = asyncio.create_task(read_from_pty())
        task_write = asyncio.create_task(write_to_pty())

        done, pending = await asyncio.wait(
            [task_read, task_write],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()

        try:
            os.close(master_fd)
        except Exception:
            pass
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.5)
            except asyncio.TimeoutError:
                proc.kill()
        except Exception:
            pass
    else:
        # Fallback for non-Linux (e.g. Windows dev): Simple message-based terminal loop
        welcome = (
            f"\r\n\x1b[1;36m========================================================\x1b[0m\r\n"
            f"\x1b[1;32m  DVM Nexus Interactive Console (v{os.getenv('APP_VERSION', '1.0.22')})\x1b[0m\r\n"
            f"\x1b[0;90m  Current working dir: {_current_working_dir}\x1b[0m\r\n"
            f"\x1b[1;36m========================================================\x1b[0m\r\n\r\n"
            f"PS {_current_working_dir}> "
        )
        await websocket.send_text(welcome)
        try:
            while True:
                data = await websocket.receive_text()
                cmd = data.strip()
                if not cmd:
                    await websocket.send_text(f"\r\nPS {_current_working_dir}> ")
                    continue
                if cmd.lower() in ("exit", "quit"):
                    await websocket.send_text("\r\nSession closed.\r\n")
                    await websocket.close()
                    break

                proc = await asyncio.create_subprocess_exec(
                    "powershell.exe", "-NoProfile", "-Command", cmd,
                    cwd=_current_working_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                out = stdout.decode("utf-8", errors="replace") + stderr.decode("utf-8", errors="replace")
                formatted = out.replace("\n", "\r\n")
                await websocket.send_text(f"\r\n{formatted}\r\nPS {_current_working_dir}> ")
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.debug(f"Terminal websocket error: {e}")
