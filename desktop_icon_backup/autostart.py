from __future__ import annotations

import ctypes
import sys
import winreg
from pathlib import Path

from .config import AUTOSTART_RUN_KEY, RUN_VALUE_NAME

_RUN_COMMAND_MAX_BYTES = 260 * 2  # Windows counts UTF-16 code units.

def _launch_paths() -> tuple[Path, ...]:
    executable = Path(sys.executable)
    if getattr(sys, "frozen", False):
        return (executable,)
    pythonw = executable.with_name("pythonw.exe")
    script = Path(__file__).resolve().parent.parent / "run_app.py"
    return (pythonw if pythonw.is_file() else executable, script)


def command_for_current_run() -> str:
    return " ".join([*(f'"{path}"' for path in _launch_paths()), "--autostart"])


def _command_arguments(command: str) -> list[str]:
    parse = ctypes.WinDLL("shell32", use_last_error=True).CommandLineToArgvW
    parse.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    parse.restype = ctypes.POINTER(ctypes.c_wchar_p)
    count = ctypes.c_int()
    argv = parse(command, ctypes.byref(count))
    if not argv:
        raise ctypes.WinError(ctypes.get_last_error())
    free = ctypes.windll.kernel32.LocalFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_void_p
    try:
        return argv[:count.value]
    finally:
        free(ctypes.cast(argv, ctypes.c_void_p))


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            AUTOSTART_RUN_KEY,
            0,
            winreg.KEY_READ,
        ) as key:
            command, value_type = winreg.QueryValueEx(key, RUN_VALUE_NAME)
    except FileNotFoundError:
        return False
    if value_type != winreg.REG_SZ or not isinstance(command, str) or not command.strip():
        return False
    if (
        "\x00" in command
        or len(command.encode("utf-16-le", errors="surrogatepass")) > _RUN_COMMAND_MAX_BYTES
    ):
        return False
    args = _command_arguments(command)
    paths = _launch_paths()
    return (
        args[-1:] == ["--autostart"]
        and tuple(Path(arg) for arg in args[:-1]) == paths
        and all(path.is_file() for path in paths)
    )


def set_enabled(enabled: bool) -> None:
    if enabled:
        if not all(path.is_file() for path in _launch_paths()):
            raise FileNotFoundError("找不到自启所需的程序文件")
        command = command_for_current_run()
        if len(command.encode("utf-16-le")) > _RUN_COMMAND_MAX_BYTES:
            raise ValueError("自启命令超过 Windows 的 260 字符限制，请缩短程序路径")
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, AUTOSTART_RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, command)
        return

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, AUTOSTART_RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, RUN_VALUE_NAME)
    except FileNotFoundError:
        pass
