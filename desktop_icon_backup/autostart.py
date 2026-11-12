from __future__ import annotations

import logging
import sys
import winreg
from pathlib import Path

from .config import AUTOSTART_RUN_KEY, RUN_VALUE_NAME

logger = logging.getLogger(__name__)


def command_for_current_run() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --autostart'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = Path(sys.executable)
    script = Path(__file__).resolve().parent.parent / "run_app.py"
    return f'"{pythonw}" "{script}" --autostart'


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            AUTOSTART_RUN_KEY,
            0,
            winreg.KEY_READ,
        ) as key:
            try:
                winreg.QueryValueEx(key, RUN_VALUE_NAME)
            except FileNotFoundError:
                return False
            return True
    except OSError:
        return False


def set_enabled(enabled: bool) -> None:
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        AUTOSTART_RUN_KEY,
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        if enabled:
            key.SetValueEx(
                RUN_VALUE_NAME,
                0,
                winreg.REG_SZ,
                command_for_current_run(),
            )
        else:
            try:
                key.DeleteValue(RUN_VALUE_NAME)
            except FileNotFoundError:
                pass
