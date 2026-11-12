import tempfile
import unittest
from pathlib import Path
from unittest import mock

from desktop_icon_backup import registry_layout
from desktop_icon_backup.registry_layout import RegistryOperationError

BAGS = "Software\\Microsoft\\Windows\\Shell\\Bags"
BAG1 = BAGS + "\\1"
DESKTOP1 = BAG1 + "\\Desktop"


class FakeKey:
    def __init__(self, children):
        self._children = children
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.closed = True


def _fake_open(children_map):
    def opener(hive, path, *args, **kwargs):
        if path not in children_map:
            raise OSError("missing key")
        return FakeKey(children_map[path])

    return opener


def _fake_enum(key, index):
    if index < len(key._children):
        return key._children[index]
    raise OSError("no more keys")


class RegistryLayoutTests(unittest.TestCase):
    def test_finds_desktop_bag_keys_recursively(self):
        children = {
            BAGS: ["1", "2"],
            BAG1: ["Desktop", "ShellViews"],
            BAGS + "\\2": ["Other"],
        }
        with mock.patch.object(registry_layout.winreg, "OpenKey", side_effect=_fake_open(children)), \
             mock.patch.object(registry_layout.winreg, "EnumKey", side_effect=_fake_enum), \
             mock.patch.object(registry_layout.winreg, "CloseKey", return_value=None):
            found = registry_layout.find_desktop_bag_keys()
        self.assertEqual(found, [DESKTOP1])

    def test_missing_base_returns_empty(self):
        with mock.patch.object(
            registry_layout.winreg,
            "OpenKey",
            side_effect=lambda *a, **k: (_ for _ in ()).throw(OSError("no base")),
        ):
            self.assertEqual(registry_layout.find_desktop_bag_keys(), [])

    def test_export_layout_writes_reg_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "payload"
            payload.mkdir()
            calls = []

            def fake_run_reg(args):
                calls.append(args)
                dest = Path(args[2])
                dest.write_bytes(b"Windows Registry Editor Version 5.00\r\n")
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch.object(
                registry_layout, "find_desktop_bag_keys", return_value=[DESKTOP1]
            ), mock.patch.object(registry_layout, "_run_reg", side_effect=fake_run_reg):
                exports = registry_layout.export_layout(payload)
            self.assertEqual(exports, [{"bag_path": DESKTOP1, "file": "desktop_1.reg"}])
            self.assertTrue((payload / "desktop_1.reg").is_file())
            self.assertEqual(calls[0][0], "export")

    def test_export_no_keys_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(registry_layout, "find_desktop_bag_keys", return_value=[]):
                with self.assertRaises(registry_layout.LayoutNotReady):
                    registry_layout.export_layout(Path(tmp))

    def test_import_layout_deletes_then_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp)
            (backup_dir / "desktop_1.reg").write_bytes(b"data")
            calls = []
            with mock.patch.object(
                registry_layout,
                "_run_reg",
                side_effect=lambda args: calls.append(args) or mock.Mock(returncode=0, stdout="", stderr=""),
            ):
                registry_layout.import_layout(
                    [{"bag_path": DESKTOP1, "file": "desktop_1.reg"}],
                    backup_dir,
                )
            self.assertEqual(calls[0][0], "delete")
            self.assertEqual(calls[1][0], "import")

    def test_import_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RegistryOperationError):
                registry_layout.import_layout(
                    [{"bag_path": DESKTOP1, "file": "missing.reg"}],
                    Path(tmp),
                )

    def test_payload_hash_stable_and_bag_path_sensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp)
            (payload / "a.reg").write_bytes(b"abc")
            (payload / "b.reg").write_bytes(b"def")
            exports = [
                {"bag_path": BAG1, "file": "a.reg"},
                {"bag_path": BAGS + "\\2\\Desktop", "file": "b.reg"},
            ]
            h1 = registry_layout.payload_hash(payload, exports)
            swapped = [
                {"bag_path": BAGS + "\\2\\Desktop", "file": "a.reg"},
                {"bag_path": BAG1, "file": "b.reg"},
            ]
            h2 = registry_layout.payload_hash(payload, swapped)
            h3 = registry_layout.payload_hash(payload, exports)
            self.assertEqual(h1, h3)
            self.assertNotEqual(h1, h2)


if __name__ == "__main__":
    unittest.main()
