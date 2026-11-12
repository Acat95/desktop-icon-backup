from __future__ import annotations

import ctypes
import os
import struct
from pathlib import Path
from typing import Dict, List, Optional

_FOLDERID_DESKTOP = (0xB4BFCC3A, 0xDB2C, 0x424C, bytes.fromhex("B0297FE99A87C641"))
_FOLDERID_PUBLIC_DESKTOP = (0xC4AA340D, 0xF20F, 0x4863, bytes.fromhex("AFEFF87EF2E6BA25"))


def _guid_bytes(folder_id) -> bytes:
    d1, d2, d3, d4 = folder_id
    return struct.pack("<IHH", d1, d2, d3) + bytes(d4)


def known_folder_path(folder_id) -> Optional[str]:
    if os.name != "nt":
        return None
    try:
        shell32 = ctypes.windll.shell32
        fn = shell32.SHGetKnownFolderPath
        fn.argtypes = [
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        fn.restype = ctypes.c_long
        buf = ctypes.c_wchar_p()
        guid = ctypes.create_string_buffer(_guid_bytes(folder_id))
        hr = fn(guid, 0, None, ctypes.byref(buf))
        if hr != 0 or not buf.value:
            return None
        value = str(buf.value)
        ctypes.windll.ole32.CoTaskMemFree(ctypes.cast(buf, ctypes.c_void_p))
        return value
    except Exception:
        return None


def desktop_directories() -> List[Path]:
    folders: List[Path] = []

    def add(path: Optional[str]) -> None:
        if path:
            candidate = Path(path)
            if candidate.is_dir() and candidate not in folders:
                folders.append(candidate)

    add(known_folder_path(_FOLDERID_DESKTOP))
    add(known_folder_path(_FOLDERID_PUBLIC_DESKTOP))
    if not folders:
        try:
            add(str(Path.home() / "Desktop"))
        except Exception:
            pass
        public = os.environ.get("PUBLIC")
        if public:
            add(str(Path(public) / "Desktop"))
    return folders


def _is_hidden(path: Path) -> bool:
    try:
        attrs = os.stat(path).st_file_attributes
    except Exception:
        return False
    return bool(attrs & 0x6)  # FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM


def _display_name(path: Path) -> str:
    if path.suffix.lower() == ".lnk":
        return path.stem
    return path.name


def enumerate_desktop_items() -> List[Dict[str, str]]:
    seen: Dict[str, str] = {}
    for folder in desktop_directories():
        try:
            children = list(folder.iterdir())
        except OSError:
            continue
        for child in children:
            try:
                if child.name.lower() == "desktop.ini":
                    continue
                if _is_hidden(child):
                    continue
                if not (child.is_file() or child.is_symlink()):
                    continue
            except OSError:
                continue
            name = _display_name(child)
            seen.setdefault(name, str(child))
    return [
        {"name": name, "path": path}
        for name, path in sorted(seen.items())
    ]


def missing_names(
    backup_items: List[Dict[str, str]],
    current_items: List[Dict[str, str]],
) -> List[str]:
    current = {item["name"] for item in current_items}
    return sorted({item["name"] for item in backup_items} - current)
