import tempfile
import unittest
from pathlib import Path
from unittest import mock

from desktop_icon_backup.config import Settings


class SettingsTests(unittest.TestCase):
    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("desktop_icon_backup.config.data_dir", return_value=Path(tmp)):
                settings = Settings(False, auto_backup_enabled=False, restart_explorer_on_restore=False)
                settings.save()
                loaded = Settings.load()
            self.assertFalse(loaded.autostart_enabled)
            self.assertFalse(loaded.auto_backup_enabled)
            self.assertFalse(loaded.restart_explorer_on_restore)

    def test_missing_file_uses_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("desktop_icon_backup.config.data_dir", return_value=Path(tmp)):
                loaded = Settings.load()
            self.assertTrue(loaded.autostart_enabled)
            self.assertTrue(loaded.auto_backup_enabled)
            self.assertTrue(loaded.restart_explorer_on_restore)


if __name__ == "__main__":
    unittest.main()

