from __future__ import annotations

import ctypes
import logging
import subprocess
import time

logger = logging.getLogger(__name__)


def explorer_running() -> bool:
    try:
        result = subprocess.run(
            ["tasklist.exe", "/FI", "IMAGENAME eq explorer.exe", "/NH"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
        )
        return "explorer.exe" in result.stdout.lower()
    except Exception:
        logger.exception("检查 explorer 进程失败")
        return False


def shell_window_ready() -> bool:
    try:
        return bool(ctypes.windll.user32.GetShellWindow())
    except Exception:
        return False


def wait_desktop_ready(min_delay: float = 8.0, timeout: float = 60.0) -> bool:
    time.sleep(min_delay)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if explorer_running() and shell_window_ready():
            return True
        time.sleep(0.5)
    return False


def restart_explorer(timeout: float = 25.0) -> None:
    subprocess.run(
        ["taskkill.exe", "/f", "/im", "explorer.exe"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=15,
    )
    time.sleep(1.2)
    subprocess.Popen(["explorer.exe"])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if explorer_running() and shell_window_ready():
            return
        time.sleep(0.5)
    raise RuntimeError("资源管理器未在限定时间内重启")

