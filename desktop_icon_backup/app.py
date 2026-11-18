from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from . import __version__, autostart, config, desktop_layout, desktop_manifest, explorer, registry_layout
from .backup_store import BackupStore
from .json_io import read_json
from .logging_setup import init_logging
from .single_instance import acquire, release, show_already_running

logger = logging.getLogger(__name__)


class DesktopIconBackupApp:
    def __init__(self, data_dir: Path | None = None):
        self.data_dir = Path(data_dir or config.data_dir())
        self.settings = config.Settings.load()
        self.refresh_autostart()
        self.store = BackupStore(self.data_dir)
        self.exit_event = threading.Event()
        self._operation_lock = threading.Lock()
        self._operations = ThreadPoolExecutor(max_workers=1, thread_name_prefix="desktop")
        self._tray = None
        self._ui = None
        self._mutex = None

    def refresh_autostart(self) -> None:
        try:
            self.autostart_enabled = autostart.is_enabled()
        except OSError:
            logger.exception("无法读取 Windows 自启状态")
            self.autostart_enabled = None

    def set_autostart(self, enabled: bool) -> None:
        try:
            autostart.set_enabled(enabled)
        finally:
            self.refresh_autostart()
        if self.autostart_enabled is not enabled:
            raise RuntimeError("无法确认 Windows 自启设置已生效")

    def persist_settings(self, settings: config.Settings) -> None:
        settings.save()
        self.settings = settings

    def notify(self, message: str, title: str = config.APP_NAME) -> None:
        if self._tray is not None:
            self._tray.notify(message, title)

    def storage_writable(self) -> bool:
        if self.data_dir.exists():
            return os.access(self.data_dir, os.W_OK)
        parent = self.data_dir.parent
        return parent.exists() and os.access(parent, os.W_OK)

    def submit_operation(self, operation):
        def run():
            if self.exit_event.is_set():
                raise CancelledError()
            return operation()

        with self._operation_lock:
            if self.exit_event.is_set():
                raise RuntimeError("程序正在退出")
            return self._operations.submit(run)

    def shutdown_operations(self) -> None:
        self._operations.shutdown(wait=True, cancel_futures=True)

    def perform_backup(self, source: str):
        payload_dir = Path(
            tempfile.mkdtemp(prefix=".tmp_backup_", dir=str(self.data_dir))
        )
        try:
            layout = desktop_layout.capture()
            manifest = [{"name": icon.name, "path": icon.key} for icon in layout.icons]
            created, _entry, outcome = self.store.create_backup(
                payload_dir,
                [],
                manifest,
                source,
                layout={"version": 1, **asdict(layout)},
            )
            payload_dir = None
            if outcome == "skip":
                return False, "布局与最新备份相同，已跳过本次备份"
            return True, f"备份成功：{len(manifest)} 个图标"
        finally:
            if payload_dir is not None:
                shutil.rmtree(payload_dir, ignore_errors=True)

    def perform_restore(self, entry_id: str) -> str:
        entries = self.store.list_entries()
        entry = next((e for e in entries if e["id"] == entry_id), None)
        if entry is None:
            raise FileNotFoundError("备份不存在")
        if entry.get("broken"):
            raise RuntimeError("该备份已损坏，无法恢复")
        if entry.get("layout_format") == "shell":
            result = desktop_layout.restore(self.store.read_desktop_layout(entry))
            self.store.mark_used(entry_id)
            if result.different or result.missing or not result.settings_match:
                summary = f"布局未完全匹配，已确认 {result.matched} 个图标坐标"
            else:
                summary = f"已验证 {result.matched} 个图标坐标和桌面排列设置"
            if result.different:
                summary += _describe_items("坐标与备份不同", result.different)
            if result.missing:
                summary += _describe_items("桌面中缺失", result.missing)
            if not result.settings_match:
                summary += "\n当前排列设置与备份不同。"
            return summary

        manifest_path = self.store.manifest_file(entry["dir"])
        manifest_data = read_json(manifest_path, {"items": []})
        manifest_items = (
            manifest_data.get("items", [])
            if isinstance(manifest_data, dict)
            else []
        )
        registry_layout.import_layout(
            entry["reg_files"], manifest_path.parent, expected_hash=entry["payload_hash"]
        )
        self.store.mark_used(entry_id)

        current_items = desktop_manifest.enumerate_desktop_items()
        missing = desktop_manifest.missing_names(manifest_items, current_items)
        summary = "已导入旧版布局，图标坐标尚未验证"
        if missing:
            summary += _describe_items("桌面中缺失", missing)
        if self.settings.restart_explorer_on_restore:
            try:
                explorer.restart_explorer()
                summary += "\n资源管理器已重启，请检查实际布局。"
            except Exception as exc:
                logger.exception("重启资源管理器失败")
                summary += f"\n注册表已恢复，但资源管理器重启失败：{exc}"
        else:
            summary += "\n请手动重启资源管理器并检查实际布局。"
        return summary

    def request_exit(self) -> None:
        with self._operation_lock:
            if self.exit_event.is_set():
                return
            self.exit_event.set()
        self._operations.shutdown(wait=False, cancel_futures=True)
        if self._ui is None:
            self._wait_for_exit()
            return
        self._ui.begin_exit()
        threading.Thread(target=self._wait_for_exit, name="wait-for-exit", daemon=True).start()

    def _wait_for_exit(self) -> None:
        self.shutdown_operations()
        if self._ui is not None:
            self._ui.post_command(self._finish_exit)
        else:
            self._finish_exit()

    def _finish_exit(self) -> None:
        if self._tray is not None:
            self._tray.stop()
        if self._ui is not None:
            self._ui.root.after(0, self._ui.root.destroy)


def _describe_items(label, names) -> str:
    shown = "、".join(names[:20])
    suffix = " 等" if len(names) > 20 else ""
    return f"\n{label}的图标（{len(names)} 个）：{shown}{suffix}"


def _boot_backup_worker(app: DesktopIconBackupApp) -> None:
    logger.info("等待桌面就绪后执行开机自动备份")
    ready = explorer.wait_desktop_ready()
    if not ready:
        logger.warning("桌面未在限定时间内就绪，本次跳过自动备份")
        return
    def backup():
        if app.exit_event.is_set() or not app.settings.auto_backup_enabled:
            return
        try:
            created, message = app.perform_backup("auto")
            logger.info("开机自动备份结果: %s", message)
            if created:
                app._ui.post_command(app._ui.refresh)
                app.notify(message)
        except Exception:
            logger.exception("开机自动备份失败")
            app.notify("开机自动备份失败，详见日志")

    if not app.exit_event.is_set():
        try:
            app.submit_operation(backup)
        except RuntimeError:
            if not app.exit_event.is_set():
                raise


def _make_tray_menu(app: DesktopIconBackupApp):
    from .tray import build_menu

    def backup(icon, item):
        app._ui.post_command(app._ui.run_backup)

    def open_window(icon, item):
        app._ui.post_command(app._ui.show_window)

    def restart(icon, item):
        app._ui.post_command(app._ui.restart_explorer_click)

    def exit_program(icon, item):
        app._ui.post_command(app.request_exit)

    return build_menu(backup, open_window, restart, exit_program)


def run_checks() -> int:
    report = []
    report.append(("Python 版本", sys.version.split()[0], True))
    try:
        import tkinter  # noqa: F401
        report.append(("tkinter", "可用", True))
    except Exception:
        report.append(("tkinter", "不可用", False))
    try:
        import pystray  # noqa: F401
        report.append(("pystray", "可用", True))
    except Exception:
        report.append(("pystray", "不可用", False))
    try:
        import PIL  # noqa: F401
        report.append(("Pillow", "可用", True))
    except Exception:
        report.append(("Pillow", "不可用", False))
    report.append(("reg.exe", "可用" if shutil.which("reg.exe") else "不可用", bool(shutil.which("reg.exe"))))
    try:
        layout = desktop_layout.capture()
        report.append(("桌面视图", f"读取到 {len(layout.icons)} 个图标坐标", True))
    except (OSError, RuntimeError) as exc:
        report.append(("桌面视图", str(exc), False))
    data_dir = config.data_dir()
    writable = not data_dir.exists() or os.access(data_dir, os.W_OK)
    report.append(("数据目录", str(data_dir), writable))
    print(json.dumps({"app": config.APP_NAME, "version": __version__, "checks": report}, ensure_ascii=False, indent=2))
    return 0 if all(item[2] for item in report) else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog=config.APP_NAME)
    parser.add_argument("--autostart", action="store_true", help="开机自启模式")
    parser.add_argument("--check", action="store_true", help="检查运行环境后退出")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    init_logging(config.log_path(), console=True)
    logger.info(
        "启动 %s v%s，参数: %s",
        config.APP_NAME,
        __version__,
        sys.argv[1:],
    )
    if args.check:
        return run_checks()
    if sys.platform != "win32":
        logger.error("本工具仅支持 Windows")
        return 1

    mutex = acquire()
    if mutex is None:
        logger.warning("已有实例在运行")
        show_already_running()
        return 0

    app = DesktopIconBackupApp()
    app._mutex = mutex
    try:
        from .tray import TrayController
        from .ui import AppUI

        app._ui = AppUI(app)
        app._tray = TrayController(_make_tray_menu(app))
        app._tray.start()
        if not app.storage_writable():
            app._tray.set_error(True)
            app._ui.set_storage_error(True)
            app.notify("存储目录不可写，备份/恢复已禁用")
        elif args.autostart and app.settings.auto_backup_enabled:
            threading.Thread(
                target=_boot_backup_worker,
                args=(app,),
                daemon=True,
                name="boot-backup",
            ).start()
        app._ui.start()
    except Exception:
        logger.exception("程序启动失败")
        return 1
    finally:
        app.exit_event.set()
        app.shutdown_operations()
        release(mutex)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
