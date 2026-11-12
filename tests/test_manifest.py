import tempfile
import unittest
from pathlib import Path
from unittest import mock

from desktop_icon_backup import desktop_manifest


class DesktopManifestTests(unittest.TestCase):
    def test_enumerate_desktop_items_filters_hidden_and_desktop_ini(self):
        with tempfile.TemporaryDirectory() as tmp:
            user = Path(tmp) / "user"
            public = Path(tmp) / "public"
            user.mkdir()
            public.mkdir()
            (user / "App.lnk").write_bytes(b"x")
            (user / "desktop.ini").write_bytes(b"x")
            (public / "Readme.txt").write_bytes(b"x")
            with mock.patch.object(
                desktop_manifest,
                "desktop_directories",
                return_value=[user, public],
            ):
                items = desktop_manifest.enumerate_desktop_items()
            self.assertEqual(
                [item["name"] for item in items],
                ["App", "Readme.txt"],
            )

    def test_missing_names(self):
        backup_items = [
            {"name": "Gone"},
            {"name": "Still Here"},
        ]
        current_items = [{"name": "Still Here"}, {"name": "New"}]
        self.assertEqual(
            desktop_manifest.missing_names(backup_items, current_items),
            ["Gone"],
        )


if __name__ == "__main__":
    unittest.main()

