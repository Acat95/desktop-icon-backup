import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from desktop_icon_backup.backup_store import BackupStore


class BackupStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name) / "data"
        self.base.mkdir()
        self.store = BackupStore(self.base, max_backups=5)

    def tearDown(self):
        self._tmp.cleanup()

    def _payload(self, name, content):
        payload = self.base / name
        payload.mkdir()
        (payload / "desktop_1.reg").write_bytes(content)
        exports = [
            {
                "bag_path": "Software\\Microsoft\\Windows\\Shell\\Bags\\1\\Desktop",
                "file": "desktop_1.reg",
            }
        ]
        return payload, exports

    def _stamped_now(self, values):
        iterator = iter(values)
        return lambda: next(iterator)

    def test_create_evicts_least_recently_used(self):
        stamps = [f"2026-01-01T00:00:{i:02d}+08:00" for i in range(6)]
        with mock.patch("desktop_icon_backup.backup_store.now_iso", side_effect=stamps):
            for i in range(6):
                payload, exports = self._payload(f"payload_{i}", b"content-%d" % i)
                created, _entry, outcome = self.store.create_backup(
                    payload, exports, [{"name": f"icon{i}"}], "auto"
                )
                self.assertTrue(created)
                self.assertEqual(outcome, "created")
        entries = self.store.list_entries()
        self.assertEqual(len(entries), 5)
        self.assertFalse(any(e["created_at"].startswith("2026-01-01T00:00:00") for e in entries))

    def test_failed_index_replace_preserves_every_old_backup(self):
        self.store.max_backups = 1
        payload, exports = self._payload("first", b"first")
        _, old, _ = self.store.create_backup(payload, exports, [], "manual")
        old_directory = self.base / old["dir"]
        index = self.base / "index.json"
        original_index = index.read_bytes()
        payload, exports = self._payload("second", b"second")
        with mock.patch("desktop_icon_backup.json_io.os.replace", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.store.create_backup(payload, exports, [], "manual")
        self.assertEqual(index.read_bytes(), original_index)
        self.assertEqual(list(self.base.glob("backup_*")), [old_directory])
        self.assertEqual((old_directory / "desktop_1.reg").read_bytes(), b"first")

    def test_failed_eviction_cleanup_is_retried_after_reopening_store(self):
        self.store.max_backups = 1
        times = ["2026-01-01T00:00:01+08:00", "2026-01-01T00:00:02+08:00"]
        with mock.patch("desktop_icon_backup.backup_store.now_iso", side_effect=times):
            payload, exports = self._payload("first", b"first")
            _, old, _ = self.store.create_backup(payload, exports, [], "manual")
            payload, exports = self._payload("second", b"second")
            with mock.patch.object(self.store, "_remove_dir", side_effect=PermissionError("locked")):
                with self.assertLogs("desktop_icon_backup.backup_store", level="WARNING"):
                    _, new, _ = self.store.create_backup(payload, exports, [], "manual")

        reopened = BackupStore(self.base, max_backups=1)
        self.assertEqual([item["id"] for item in reopened.list_entries()], [new["id"]])
        self.assertTrue((self.base / old["dir"]).exists())
        payload, exports = self._payload("retry", b"second")
        _, _, outcome = reopened.create_backup(payload, exports, [], "manual")
        self.assertEqual(outcome, "skip")
        self.assertFalse((self.base / old["dir"]).exists())
        self.assertTrue((self.base / new["dir"]).is_dir())

    def test_failed_manual_delete_preserves_backup_directory(self):
        payload, exports = self._payload("first", b"first")
        _, entry, _ = self.store.create_backup(payload, exports, [], "manual")
        with mock.patch.object(self.store, "_save_index", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                self.store.delete_entry(entry["id"])
        self.assertFalse(self.store.list_entries()[0]["broken"])

    def test_tampered_restore_directory_and_missing_hash_are_broken(self):
        payload, exports = self._payload("first", b"first")
        _, entry, _ = self.store.create_backup(payload, exports, [], "manual")
        index = self.base / "index.json"
        cases = [
            ({**entry, "dir": str(self.base / entry["dir"])}, "目录名无效"),
            ({**entry, "dir": "..\\" + entry["dir"]}, "目录名无效"),
            ({**entry, "payload_hash": None}, "校验值缺失或无效"),
        ]
        for item, reason in cases:
            with self.subTest(reason=reason, path=item["dir"]):
                index.write_text(json.dumps({"backups": [item]}), encoding="utf-8")
                self.assertIn(reason, self.store.list_entries()[0]["broken_reason"])

    def test_directory_redirect_cannot_be_restored_or_removed(self):
        payload, exports = self._payload("first", b"first")
        _, entry, _ = self.store.create_backup(payload, exports, [], "manual")
        target = self.base.resolve() / entry["dir"]
        external = self.base.parent.resolve() / "outside"
        external.mkdir()
        original_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            if path == target:
                return external
            return original_resolve(path, *args, **kwargs)

        with mock.patch.object(type(target), "resolve", resolve):
            self.assertTrue(self.store.list_entries()[0]["broken"])
            with self.assertLogs("desktop_icon_backup.backup_store", level="WARNING"):
                self.store.delete_entry(entry["id"])
        self.assertTrue(external.is_dir())
        self.assertTrue(target.is_dir())

    def test_duplicate_payload_is_skipped(self):
        payload1, exports1 = self._payload("payload_1", b"same")
        created, _entry, outcome = self.store.create_backup(
            payload1, exports1, [{"name": "a"}], "manual"
        )
        self.assertTrue(created)
        count_after_first = len(self.store.list_entries())
        payload2, exports2 = self._payload("payload_2", b"same")
        created2, _entry2, outcome2 = self.store.create_backup(
            payload2, exports2, [{"name": "a"}], "auto"
        )
        self.assertFalse(created2)
        self.assertEqual(outcome2, "skip")
        self.assertFalse(payload2.exists())
        self.assertEqual(len(self.store.list_entries()), count_after_first)

    def test_mark_used_refreshes_last_used_at(self):
        stamps = ["2026-02-01T00:00:00+08:00", "2026-02-01T00:00:01+08:00"]
        with mock.patch("desktop_icon_backup.backup_store.now_iso", side_effect=stamps):
            payload, exports = self._payload("payload_1", b"one")
            _created, entry, _outcome = self.store.create_backup(
                payload, exports, [], "manual"
            )
            marked = self.store.mark_used(entry["id"])
        self.assertNotEqual(marked["last_used_at"], entry["last_used_at"])

    def test_delete_entry_and_clear_all(self):
        payload, exports = self._payload("payload_1", b"one")
        _created, entry, _outcome = self.store.create_backup(
            payload, exports, [], "manual"
        )
        self.store.delete_entry(entry["id"])
        self.assertEqual(self.store.list_entries(), [])
        payload2, exports2 = self._payload("payload_2", b"two")
        self.store.create_backup(payload2, exports2, [], "manual")
        self.store.clear_all()
        self.assertEqual(self.store.list_entries(), [])
        self.assertEqual(list(self.base.glob("backup_*")), [])

    def test_broken_entry_is_reported(self):
        payload, exports = self._payload("payload_1", b"one")
        _created, entry, _outcome = self.store.create_backup(
            payload, exports, [], "manual"
        )
        shutil.rmtree(self.base / entry["dir"])
        listed = self.store.list_entries()
        self.assertEqual(len(listed), 1)
        self.assertTrue(listed[0]["broken"])
        self.assertIn("备份目录不存在", listed[0]["broken_reason"])


if __name__ == "__main__":
    unittest.main()

