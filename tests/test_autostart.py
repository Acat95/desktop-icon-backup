import tempfile
import unittest
from pathlib import Path
from unittest import mock

from desktop_icon_backup import autostart, config
from desktop_icon_backup.app import DesktopIconBackupApp


class AutostartTests(unittest.TestCase):
    def test_set_enabled_writes_run_value(self):
        handle = mock.MagicMock()
        key = handle.__enter__.return_value
        with mock.patch.object(autostart.winreg, "CreateKeyEx", return_value=handle), \
             mock.patch.object(autostart.winreg, "SetValueEx") as set_value, \
             mock.patch.object(
                 autostart,
                 "command_for_current_run",
                 return_value='"prog.exe" --autostart',
             ):
            autostart.set_enabled(True)
        set_value.assert_called_once_with(
            key, config.RUN_VALUE_NAME, 0, autostart.winreg.REG_SZ, '"prog.exe" --autostart'
        )

    def test_set_disabled_removes_value(self):
        handle = mock.MagicMock()
        with mock.patch.object(autostart.winreg, "OpenKey", return_value=handle), \
             mock.patch.object(autostart.winreg, "DeleteValue") as delete_value, \
             mock.patch.object(autostart.winreg, "CreateKeyEx") as create:
            autostart.set_enabled(False)
        delete_value.assert_called_once_with(handle.__enter__.return_value, config.RUN_VALUE_NAME)
        create.assert_not_called()

    def test_run_value_must_target_current_installation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "解释器 with spaces.exe"
            script = root / "run_app.py"
            executable.touch()
            script.touch()
            with mock.patch.object(autostart, "_launch_paths", return_value=(executable, script)), \
                 mock.patch.object(autostart.winreg, "OpenKey"), \
                 mock.patch.object(autostart.winreg, "QueryValueEx") as query:
                correct = autostart.command_for_current_run()
                for value, expected in (
                    (correct, True),
                    (correct.replace("--autostart", "--check"), False),
                    (correct.replace("run_app.py", "old_app.py"), False),
                    (correct + " --check", False),
                ):
                    with self.subTest(value=value):
                        query.return_value = value, autostart.winreg.REG_SZ
                        self.assertIs(autostart.is_enabled(), expected)
                query.return_value = correct, autostart.winreg.REG_BINARY
                self.assertFalse(autostart.is_enabled())
                query.return_value = correct, autostart.winreg.REG_SZ
                script.unlink()
                self.assertFalse(autostart.is_enabled())

    def test_missing_value_is_disabled_and_read_denied_is_reported(self):
        with mock.patch.object(autostart.winreg, "OpenKey"), \
             mock.patch.object(autostart.winreg, "QueryValueEx", side_effect=FileNotFoundError):
            self.assertFalse(autostart.is_enabled())
        with mock.patch.object(autostart.winreg, "OpenKey", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                autostart.is_enabled()

    def test_startup_uses_registry_and_unrelated_settings_do_not_write_run_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config, "data_dir", return_value=Path(tmp)), \
                 mock.patch.object(autostart, "is_enabled", return_value=False), \
                 mock.patch.object(autostart, "set_enabled") as write_run:
                config.settings_path().write_text('{"autostart_enabled": true}', encoding="utf-8")
                app = DesktopIconBackupApp()
                try:
                    self.assertIs(app.autostart_enabled, False)
                    app.persist_settings(config.Settings(auto_backup_enabled=False))
                    write_run.assert_not_called()
                finally:
                    app.shutdown_operations()

    def test_switch_reads_back_the_actual_value_after_each_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(autostart, "is_enabled", return_value=False) as read, \
                 mock.patch.object(autostart, "set_enabled") as write:
                app = DesktopIconBackupApp(Path(tmp))
                try:
                    with self.assertRaisesRegex(RuntimeError, "无法确认"):
                        app.set_autostart(True)
                    self.assertIs(app.autostart_enabled, False)
                    read.return_value = True
                    app.set_autostart(True)
                    self.assertIs(app.autostart_enabled, True)
                    write.side_effect = PermissionError
                    with self.assertRaises(PermissionError):
                        app.set_autostart(False)
                    self.assertIs(app.autostart_enabled, True)
                finally:
                    app.shutdown_operations()


if __name__ == "__main__":
    unittest.main()
