from __future__ import annotations

import ctypes
import os

ERROR_ALREADY_EXISTS = 183
_MUTEX_NAME = "Local\\DesktopIconBackupAssistant"


def acquire():
    if os.name != "nt":
        return object()
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    if not handle:
        raise ctypes.WinError()
    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        ctypes.windll.kernel32.CloseHandle(handle)
        return None
    return handle


def release(handle) -> None:
    if handle:
        try:
            ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            pass


def show_already_running() -> None:
    try:
        ctypes.windll.user32.MessageBoxW(
            None,
            "程序已在运行，请查看系统托盘或现有窗口。",
            "桌面图标备份助手",
            0x40,
        )
    except Exception:
        pass

