import sys
import unittest
from unittest import mock

from desktop_icon_backup import autostart


class FakeRunKey:
    def __init__(self):
        self.values = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def SetValueEx(self, name, reserved, value_type, value):
        self.values[name] = value

    def DeleteValue(self, name):
        del self.values[name]


class AutostartTests(unittest.TestCase):
    def test_set_enabled_writes_run_value(self):
        key = FakeRunKey()
        with mock.patch.object(autostart.winreg, "CreateKeyEx", return_value=key), \
             mock.patch.object(
                 autostart,
                 "command_for_current_run",
                 return_value='"prog.exe" --autostart',
             ):
            autostart.set_enabled(True)
        self.assertEqual(key.values, {"DesktopIconBackupAssistant": '"prog.exe" --autostart'})

    def test_set_disabled_removes_value(self):
        key = FakeRunKey()
        key.values["DesktopIconBackupAssistant"] = "x"
        with mock.patch.object(autostart.winreg, "CreateKeyEx", return_value=key):
            autostart.set_enabled(False)
        self.assertEqual(key.values, {})

    def test_command_contains_autostart_flag(self):
        command = autostart.command_for_current_run()
        self.assertIn("--autostart", command)


if __name__ == "__main__":
    unittest.main()
