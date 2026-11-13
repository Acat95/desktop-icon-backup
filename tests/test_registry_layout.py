import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from unittest import mock

from desktop_icon_backup import registry_layout
from desktop_icon_backup.registry_layout import RegistryOperationError

BAGS = "Software\\Microsoft\\Windows\\Shell\\Bags"
BAG1 = BAGS + "\\1"
DESKTOP1 = BAG1 + "\\Desktop"
DESKTOP2 = BAGS + "\\2\\Desktop"
DESKTOP3 = BAGS + "\\3\\Desktop"


def _write_reg(path, key, value):
    path.write_text(
        f"Windows Registry Editor Version 5.00\n\n"
        f"[HKEY_CURRENT_USER\\{key}]\n"
        f'"Layout"="{value}"\n',
        encoding="utf-16",
    )


class FakeRegistry:
    def __init__(self, values):
        self.values = values
        self.calls = []
        self.fail_export = False
        self.fail_import = None
        self.fail_delete = None
        self.fail_rollback = None

    def run(self, args):
        self.calls.append(args)
        command = args[0]
        if command == "export":
            if self.fail_export:
                return CompletedProcess(args, 1, "", "export failed")
            key = args[1].removeprefix("HKCU\\")
            _write_reg(Path(args[2]), key, self.values[key])
        elif command == "delete":
            key = args[1].removeprefix("HKCU\\")
            if self.fail_delete == key:
                self.fail_delete = None
                return CompletedProcess(args, 1, "", "delete failed")
            del self.values[key]
        elif command == "import":
            source = Path(args[1])
            lines = source.read_text(encoding="utf-16").splitlines()
            key = lines[2][len("[HKEY_CURRENT_USER\\"):-1]
            value = lines[3].split('"')[3]
            if value == self.fail_rollback:
                return CompletedProcess(args, 1, "", "rollback failed")
            self.values[key] = value
            if value == self.fail_import:
                # Simulate an import that writes data before the command times out.
                raise TimeoutExpired(args, 45)
        return CompletedProcess(args, 0, "", "")


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

    def test_import_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(registry_layout, "_run_reg") as run:
                with self.assertRaises(RegistryOperationError):
                    registry_layout.import_layout(
                        [{"bag_path": DESKTOP1, "file": "missing.reg"}],
                        Path(tmp),
                    )
                run.assert_not_called()

    def test_payload_hash_stable_and_bag_path_sensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp)
            (payload / "a.reg").write_bytes(b"abc")
            (payload / "b.reg").write_bytes(b"def")
            exports = [
                {"bag_path": BAG1, "file": "a.reg"},
                {"bag_path": DESKTOP2, "file": "b.reg"},
            ]
            h1 = registry_layout.payload_hash(payload, exports)
            swapped = [
                {"bag_path": DESKTOP2, "file": "a.reg"},
                {"bag_path": BAG1, "file": "b.reg"},
            ]
            h2 = registry_layout.payload_hash(payload, swapped)
            h3 = registry_layout.payload_hash(payload, exports)
            self.assertEqual(h1, h3)
            self.assertNotEqual(h1, h2)


class RestoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.backup_dir = Path(temp.name) / "backup"
        self.backup_dir.mkdir()
        self.work_dir = Path(temp.name) / "work"
        self.work_dir.mkdir()
        self.original = {DESKTOP1: "old-1", DESKTOP3: "old-3"}
        self.registry = FakeRegistry(self.original.copy())
        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(mock.patch.object(
            registry_layout,
            "_run_reg",
            side_effect=self.registry.run,
        ))
        patches.enter_context(mock.patch.object(
            registry_layout,
            "_key_exists",
            side_effect=self.registry.values.__contains__,
        ))
        patches.enter_context(mock.patch.object(
            registry_layout.tempfile, "mkdtemp", return_value=str(self.work_dir)
        ))
        self.reg_files = []
        for idx, key in enumerate([DESKTOP1, DESKTOP2, DESKTOP3], start=1):
            name = f"desktop_{idx}.reg"
            _write_reg(self.backup_dir / name, key, f"new-{idx}")
            self.reg_files.append({"bag_path": key, "file": name})
        self.digest = registry_layout.payload_hash(self.backup_dir, self.reg_files)

    def restore(self):
        registry_layout.import_layout(
            self.reg_files, self.backup_dir, expected_hash=self.digest
        )

    def test_snapshots_are_complete_before_replacing_keys(self):
        self.restore()
        self.assertEqual(
            [args[0] for args in self.registry.calls],
            ["export", "export", "delete", "import", "import", "delete", "import"],
        )
        self.assertEqual(
            self.registry.values,
            {DESKTOP1: "new-1", DESKTOP2: "new-2", DESKTOP3: "new-3"},
        )
        self.assertFalse(self.work_dir.exists())

    def test_corrupt_payload_is_rejected_before_touching_the_registry(self):
        (self.backup_dir / "desktop_3.reg").write_bytes(b"invalid")
        with self.assertRaises(RegistryOperationError):
            self.restore()
        self.assertEqual(self.registry.calls, [])

    def test_payload_hash_mismatch_is_rejected_before_touching_the_registry(self):
        _write_reg(self.backup_dir / "desktop_3.reg", DESKTOP3, "changed")
        with self.assertRaises(RegistryOperationError):
            self.restore()
        self.assertEqual(self.registry.calls, [])

    def test_unexpected_key_section_is_rejected(self):
        with (self.backup_dir / "desktop_3.reg").open("a", encoding="utf-16-le") as fh:
            fh.write("\n[HKEY_CURRENT_USER\\Software\\Unrelated]\n")
        with self.assertRaisesRegex(RegistryOperationError, "无效的键"):
            registry_layout.import_layout(self.reg_files, self.backup_dir)
        self.assertEqual(self.registry.calls, [])

    def test_snapshot_failure_leaves_registry_untouched(self):
        self.registry.fail_export = True
        with self.assertRaises(RegistryOperationError):
            self.restore()
        self.assertEqual([args[0] for args in self.registry.calls], ["export"])
        self.assertEqual(self.registry.values, self.original)
        self.assertFalse(self.work_dir.exists())

    def test_delete_failure_aborts_and_rolls_back_previous_keys(self):
        self.registry.fail_delete = DESKTOP3
        with self.assertRaisesRegex(RegistryOperationError, "已回滚"):
            self.restore()
        self.assertEqual(self.registry.values, self.original)
        self.assertFalse(self.work_dir.exists())

    def test_import_timeout_rolls_back_existing_and_new_keys(self):
        self.registry.fail_import = "new-3"
        with self.assertRaisesRegex(RegistryOperationError, "已回滚"):
            self.restore()
        self.assertEqual(self.registry.values, self.original)
        self.assertFalse(self.work_dir.exists())

    def test_rollback_failure_keeps_snapshots_and_continues_with_other_keys(self):
        self.registry.fail_import = "new-3"
        self.registry.fail_rollback = "old-3"
        with self.assertLogs(registry_layout.logger, level="ERROR"):
            with self.assertRaises(RegistryOperationError) as error:
                self.restore()
        snapshot = self.work_dir / "previous" / "desktop_3.reg"
        self.assertIn(str(snapshot.parent), str(error.exception))
        self.assertIn("old-3", snapshot.read_text(encoding="utf-16"))
        self.assertEqual(self.registry.values, {DESKTOP1: "old-1"})


if __name__ == "__main__":
    unittest.main()
