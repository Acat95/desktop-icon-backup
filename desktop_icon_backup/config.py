from __future__ import annotations

import getpass
import sys
from dataclasses import dataclass
from pathlib import Path

from .json_io import read_json, write_json_atomic

APP_NAME = "桌面图标备份助手"
RUN_VALUE_NAME = "DesktopIconBackupAssistant"
MAX_BACKUPS = 5
AUTOSTART_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
BAGS_BASE_KEY = r"Software\Microsoft\Windows\Shell\Bags"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def user_name() -> str:
    try:
        return getpass.getuser() or "default"
    except Exception:
        return "default"


def data_dir() -> Path:
    return app_root() / "backup" / user_name()


def settings_path() -> Path:
    return data_dir() / "settings.json"


def index_path() -> Path:
    return data_dir() / "index.json"


def log_path() -> Path:
    return data_dir() / "app.log"


@dataclass
class Settings:
    autostart_enabled: bool = True
    auto_backup_enabled: bool = True
    restart_explorer_on_restore: bool = True

    @classmethod
    def load(cls) -> "Settings":
        data = read_json(settings_path(), {})
        if not isinstance(data, dict):
            data = {}
        return cls(
            autostart_enabled=bool(data.get("autostart_enabled", True)),
            auto_backup_enabled=bool(data.get("auto_backup_enabled", True)),
            restart_explorer_on_restore=bool(data.get("restart_explorer_on_restore", True)),
        )

    def save(self) -> None:
        data_dir().mkdir(parents=True, exist_ok=True)
        write_json_atomic(settings_path(), {
            "autostart_enabled": self.autostart_enabled,
            "auto_backup_enabled": self.auto_backup_enabled,
            "restart_explorer_on_restore": self.restart_explorer_on_restore,
        })

