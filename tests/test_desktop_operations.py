import json
import tempfile
import threading
import unittest
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path
from queue import Queue
from unittest.mock import Mock, patch

from desktop_icon_backup import desktop_layout
from desktop_icon_backup.app import DesktopIconBackupApp, _boot_backup_worker
from desktop_icon_backup.config import BAGS_BASE_KEY
from desktop_icon_backup.desktop_layout import Icon, Layout, ViewSettings


VIEW = ViewSettings(1, 48, 4, ())
ICON = Icon("shell-id", "图标", 10, 20)
LAYOUT = Layout(VIEW, (ICON,))


class DesktopOperationsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.app = DesktopIconBackupApp(Path(temp.name))
        self.addCleanup(self.app.shutdown_operations)

    def test_boot_backup_queues_after_in_progress_operation(self):
        entered = threading.Event()
        proceed = threading.Event()
        order = []
        self.app._ui = Mock()
        self.app.settings.auto_backup_enabled = True

        def manual():
            entered.set()
            if not proceed.wait(5):
                raise TimeoutError("test worker was not released")
            order.append("manual")

        def auto(source):
            order.append(source)
            return True, "saved"

        manual_future = self.app.submit_operation(manual)
        with patch.object(self.app, "perform_backup", side_effect=auto), \
             patch("desktop_icon_backup.app.explorer.wait_desktop_ready", return_value=True):
            try:
                self.assertTrue(entered.wait(5))
                _boot_backup_worker(self.app)
                clear_future = self.app.submit_operation(lambda: order.append("clear"))
                self.assertEqual(order, [])
                self.app._ui.post_command.assert_not_called()
            finally:
                proceed.set()
            manual_future.result(timeout=5)
            clear_future.result(timeout=5)

        self.assertEqual(order, ["manual", "auto", "clear"])
        self.app._ui.post_command.assert_called_once_with(self.app._ui.refresh)

    def test_tray_exit_keeps_ui_alive_until_running_restore_finishes(self):
        started = threading.Event()
        finish = threading.Event()
        callbacks = Queue()
        self.app._ui = Mock()
        self.app._ui.post_command.side_effect = callbacks.put
        self.app._tray = Mock()

        def restore():
            started.set()
            if not finish.wait(5):
                raise TimeoutError("test restore was not released")
            return "restore complete"

        restoring = self.app.submit_operation(restore)
        pending = Mock()
        waiting = self.app.submit_operation(pending)
        try:
            self.assertTrue(started.wait(5))
            self.app.request_exit()
            self.app.request_exit()
            self.app._ui.begin_exit.assert_called_once_with()
            self.app._tray.stop.assert_not_called()
            self.app._ui.root.after.assert_not_called()
            self.assertTrue(callbacks.empty())
            self.assertTrue(waiting.cancelled())
            with self.assertRaisesRegex(RuntimeError, "正在退出"):
                self.app.submit_operation(Mock())
        finally:
            finish.set()
        self.assertEqual(restoring.result(timeout=5), "restore complete")
        callbacks.get(timeout=5)()
        self.app._tray.stop.assert_called_once_with()
        self.app._ui.root.after.assert_called_once_with(0, self.app._ui.root.destroy)
        pending.assert_not_called()

    def test_coordinate_backup_and_restore_use_live_layout(self):
        moved = Layout(VIEW, (Icon(ICON.key, ICON.name, 60, 50),))
        with patch.object(desktop_layout, "capture", side_effect=[LAYOUT, LAYOUT, moved]):
            self.assertTrue(self.app.perform_backup("manual")[0])
            entry = self.app.store.list_entries()[0]
            self.assertFalse(self.app.perform_backup("manual")[0])
            self.assertTrue(self.app.perform_backup("manual")[0])
        self.assertEqual(len(self.app.store.list_entries()), 2)
        self.assertEqual(self.app.store.read_desktop_layout(entry), LAYOUT)

        result = desktop_layout.RestoreResult(0, (ICON.name,), (), True)
        with patch.object(desktop_layout, "restore", return_value=result) as restore, \
             patch("desktop_icon_backup.app.explorer.restart_explorer") as restart:
            message = self.app.perform_restore(entry["id"])
        restore.assert_called_once_with(LAYOUT)
        restart.assert_not_called()
        self.assertIn("布局未完全匹配", message)
        self.assertIn(ICON.name, message)

    def test_tampered_coordinates_are_rejected_before_calling_shell(self):
        with patch.object(desktop_layout, "capture", return_value=LAYOUT):
            self.app.perform_backup("manual")
        entry = self.app.store.list_entries()[0]
        path = self.app.data_dir / entry["dir"] / "layout.json"
        path.write_text("{}", encoding="utf-8")
        with patch.object(desktop_layout, "restore") as restore:
            with self.assertRaisesRegex(RuntimeError, "备份已损坏"):
                self.app.perform_restore(entry["id"])
        restore.assert_not_called()

    def test_registry_only_backup_is_restored_with_unverified_status(self):
        payload = self.app.data_dir / "staged"
        payload.mkdir()
        bag_path = BAGS_BASE_KEY + r"\1\Desktop"
        (payload / "desktop_1.reg").write_text(
            f"Windows Registry Editor Version 5.00\n\n[HKEY_CURRENT_USER\\{bag_path}]\n",
            encoding="utf-16",
        )
        exports = [{"file": "desktop_1.reg", "bag_path": bag_path}]
        _, entry, _ = self.app.store.create_backup(payload, exports, [], "manual")
        self.app.settings.restart_explorer_on_restore = True
        with patch("desktop_icon_backup.app.registry_layout.import_layout") as importer, \
             patch("desktop_icon_backup.app.explorer.restart_explorer") as restart, \
             patch("desktop_icon_backup.app.desktop_manifest.enumerate_desktop_items", return_value=[]):
            message = self.app.perform_restore(entry["id"])
        importer.assert_called_once()
        restart.assert_called_once()
        self.assertIn("图标坐标尚未验证", message)

    def test_missing_index_recovers_an_older_coordinate_backup(self):
        with patch.object(desktop_layout, "capture", return_value=LAYOUT):
            self.app.perform_backup("manual")
        entry, = self.app.store.list_entries()
        manifest = self.app.store.manifest_file(entry["dir"])
        data = json.loads(manifest.read_bytes())
        del data["backup"]
        manifest.write_text(json.dumps(data), encoding="utf-8")
        (self.app.data_dir / "index.json").unlink()
        recovered, = self.app.store.list_entries()
        self.assertFalse(recovered["broken"])
        self.assertEqual(self.app.store.read_desktop_layout(recovered), LAYOUT)


class DesktopLayoutTests(unittest.TestCase):
    def test_invalid_coordinates_are_rejected(self):
        data = json.loads(json.dumps({"version": 1, **asdict(LAYOUT)}))
        data["icons"][0]["x"] = 2**31
        with self.assertRaises(ValueError):
            desktop_layout.decode_layout(data)

    def test_reports_applied_coordinates_settings_and_missing_icons(self):
        before = Layout(VIEW, (Icon(ICON.key, ICON.name, 0, 0),))
        actual = Layout(VIEW, (ICON, Icon("shifted", "偏移", 50, 60)))
        wanted = Layout(ViewSettings(1, 32, 5, ()), (
            ICON, Icon("shifted", "偏移", 55, 60), Icon("missing", "缺失", 0, 30),
        ))
        view = Mock()
        view.snapshot.side_effect = [before, actual]
        with patch.object(desktop_layout, "_desktop_view", return_value=nullcontext(view)):
            result = desktop_layout.restore(wanted)
        self.assertEqual(result, desktop_layout.RestoreResult(1, ("偏移",), ("缺失",), False))
        self.assertEqual([call.args[0] for call in view.set_arrangement.call_args_list], [0, 5])
        view.position.assert_called_once_with(wanted.icons)

    def test_failure_tries_to_restore_previous_positions_and_settings(self):
        before = Layout(VIEW, (Icon(ICON.key, ICON.name, 0, 0),))
        view = Mock()
        view.snapshot.side_effect = [before, before]
        view.settings.return_value = VIEW
        view.position.side_effect = [OSError("position failed"), None]
        with patch.object(desktop_layout, "_desktop_view", return_value=nullcontext(view)):
            with self.assertRaisesRegex(RuntimeError, "已回滚"):
                desktop_layout.restore(LAYOUT)
        self.assertEqual([call.args[0] for call in view.position.call_args_list],
                         [LAYOUT.icons, before.icons])
