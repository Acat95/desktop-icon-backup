from __future__ import annotations

import datetime
import hashlib
import json
import logging
import re
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import MAX_BACKUPS
from .desktop_layout import Layout, decode_layout
from .json_io import now_iso, read_json_strict, write_json_atomic
from .registry_layout import (
    RegistryOperationError,
    payload_hash,
    recover_export_index,
    validated_payload_hash,
)

logger = logging.getLogger(__name__)
_BACKUP_DIR_NAME = re.compile(r"backup_[0-9]{8}_[0-9]{6}_[0-9a-f]{6}", re.IGNORECASE | re.ASCII)
_BACKUP_ID = re.compile(r"[0-9a-f]{32}")
_PAYLOAD_HASH = re.compile(r"[0-9a-f]{64}")
_BACKUP_ERRORS = (OSError, ValueError, RecursionError, RegistryOperationError)


def _index_entry_valid(entry: Any) -> bool:
    return (
        isinstance(entry, dict)
        and all(
            isinstance(entry.get(field), str) and entry[field]
            for field in ("id", "dir", "created_at", "last_used_at")
        )
        and _BACKUP_ID.fullmatch(entry["id"]) is not None
    )


class BackupStore:
    """Manage the at most max_backups layout snapshots with LRU eviction."""

    def __init__(self, base_dir: Path, max_backups: int = MAX_BACKUPS):
        self._base = Path(base_dir).resolve()
        self._base.mkdir(parents=True, exist_ok=True)
        self._index_lock = threading.Lock()
        self.max_backups = max_backups

    def _index_path(self) -> Path:
        return self._base / "index.json"

    def _load_index(self) -> Dict[str, Any]:
        original = read_json_strict(self._index_path(), None)
        index = original if isinstance(original, dict) else {}
        records = index.get("backups")
        entries = []
        known_dirs = set()
        known_ids = set()
        if isinstance(records, list):
            for entry in records:
                if not _index_entry_valid(entry):
                    continue
                name = entry["dir"].lower()
                if name in known_dirs or entry["id"] in known_ids:
                    continue
                entries.append(entry)
                known_dirs.add(name)
                known_ids.add(entry["id"])
            if len(entries) == len(records):
                return index

        pending = index.get("pending_cleanup")
        pending = (
            [name for name in pending if isinstance(name, str) and _BACKUP_DIR_NAME.fullmatch(name)]
            if isinstance(pending, list) else []
        )
        # Keep committed deletions out of a rebuilt index even if cleanup failed.
        for marker in self._base.glob(".backup_*.deleted"):
            name = marker.name[1:-len(".deleted")]
            if _BACKUP_DIR_NAME.fullmatch(name) and name not in pending:
                pending.append(name)
        known_dirs.update(name.lower() for name in pending)
        for directory in sorted(self._base.iterdir()):
            if not _BACKUP_DIR_NAME.fullmatch(directory.name) or directory.name.lower() in known_dirs:
                continue
            try:
                entry = self._recover_entry(directory.name)
            except _BACKUP_ERRORS as exc:
                logger.warning("跳过无法重建索引的备份 %s: %s", directory.name, exc)
                continue
            if entry["id"] in known_ids:
                logger.warning("跳过标识重复的备份目录: %s", directory.name)
                continue
            entries.append(entry)
            known_ids.add(entry["id"])
        index["backups"] = entries
        if pending:
            index["pending_cleanup"] = pending
        if original is not None or entries or pending:
            try:
                self._save_index(index)
            except OSError:
                logger.exception("无法保存重建的备份索引")
            else:
                logger.info("已重建备份索引，收录 %d 份备份", len(entries))
        return index

    def _recover_entry(self, dir_name: str) -> Dict[str, Any]:
        directory = self.backup_directory(dir_name)
        manifest = self._read_manifest(dir_name)
        entry = manifest.get("backup")
        if "backup" in manifest:
            if not _index_entry_valid(entry) or entry["dir"] != dir_name:
                raise ValueError("备份目录中的索引记录无效")
            self._validate_payload(entry)
            return entry

        # Older manifests contain only items. The directory name carries the
        # creation time and the layout files carry their own format and keys.
        stamp = datetime.datetime.strptime(dir_name[7:22], "%Y%m%d_%H%M%S")
        created = stamp.astimezone().isoformat(timespec="milliseconds")
        entry = {
            "id": uuid.uuid5(uuid.NAMESPACE_OID, dir_name.lower()).hex,
            "dir": dir_name,
            "created_at": created,
            "last_used_at": created,
            "source": "manual",
            "icon_count": len(manifest["items"]),
            "reg_files": [],
        }
        if (directory / "layout.json").exists():
            data = self._backup_file(dir_name, "layout.json").read_bytes()
            decode_layout(json.loads(data))
            entry["layout_format"] = "shell"
            entry["payload_hash"] = hashlib.sha256(data).hexdigest()
        else:
            entry["reg_files"] = recover_export_index(directory)
            entry["payload_hash"] = validated_payload_hash(directory, entry["reg_files"])
        return entry

    def _save_index(self, index: Dict[str, Any]) -> None:
        write_json_atomic(self._index_path(), index)

    def _retry_cleanup(self, index: Dict[str, Any]) -> List[str]:
        pending = index.get("pending_cleanup")
        if not isinstance(pending, list):
            return []
        return self._cleanup_dirs(pending, index["backups"])

    def backup_directory(self, dir_name: str) -> Path:
        if not isinstance(dir_name, str) or not _BACKUP_DIR_NAME.fullmatch(dir_name):
            raise ValueError(f"备份目录名无效: {dir_name}")
        target = self._base / dir_name
        if target.resolve() != target:
            raise ValueError(f"备份目录不能指向其他路径: {dir_name}")
        return target

    def manifest_file(self, dir_name: str) -> Path:
        return self._backup_file(dir_name, "manifest.json")

    def _backup_file(self, dir_name: str, file_name: str) -> Path:
        path = self.backup_directory(dir_name) / file_name
        if path.resolve() != path or not path.is_file():
            raise FileNotFoundError(f"备份文件缺失或路径无效: {file_name}")
        return path

    def _read_manifest(self, dir_name: str) -> Dict[str, Any]:
        with self.manifest_file(dir_name).open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError("备份清单格式无效")
        for item in data["items"]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("name"), str)
                or ("path" in item and not isinstance(item["path"], str))
            ):
                raise ValueError("备份清单的图标记录无效")
        return data

    def _validate_payload(self, entry: Dict[str, Any]) -> None:
        digest = entry.get("payload_hash")
        if not isinstance(digest, str) or not _PAYLOAD_HASH.fullmatch(digest):
            raise ValueError("备份校验值缺失或无效")
        if entry.get("layout_format") == "shell":
            self.read_desktop_layout(entry)
        else:
            directory = self.backup_directory(entry["dir"])
            if validated_payload_hash(directory, entry.get("reg_files")) != digest:
                raise ValueError("备份注册表文件校验失败")

    def read_desktop_layout(self, entry: Dict[str, Any]) -> Layout:
        data = self._backup_file(entry["dir"], "layout.json").read_bytes()
        if hashlib.sha256(data).hexdigest() != entry["payload_hash"]:
            raise ValueError("桌面坐标备份校验失败")
        return decode_layout(json.loads(data))

    def list_entries(self) -> List[Dict[str, Any]]:
        with self._index_lock:
            entries = self._load_index()["backups"]
        result = []
        for entry in entries:
            item = dict(entry)
            item["broken"] = False
            item["broken_reason"] = ""
            try:
                entry_dir = self.backup_directory(item["dir"])
                if not entry_dir.is_dir():
                    raise FileNotFoundError("备份目录不存在")
                self._read_manifest(item["dir"])
                self._validate_payload(item)
            except _BACKUP_ERRORS as exc:
                item["broken"] = True
                item["broken_reason"] = str(exc)
            result.append(item)
        return sorted(result, key=lambda e: e.get("created_at", ""), reverse=True)

    def _matches_latest(self, entry, digest: str, manifest: List[Dict[str, str]]) -> bool:
        if not entry or entry.get("payload_hash") != digest:
            return False
        try:
            if self._read_manifest(entry["dir"])["items"] != manifest:
                return False
            self._validate_payload(entry)
        except _BACKUP_ERRORS as exc:
            logger.warning("最新备份未通过校验，继续创建新备份: %s", exc)
            return False
        return True

    def create_backup(
        self,
        payload_dir: Path,
        exports: List[Dict[str, str]],
        manifest: List[Dict[str, str]],
        source: str,
        *,
        layout: Dict[str, Any] | None = None,
    ) -> Tuple[bool, Optional[Dict[str, Any]], str]:
        payload_dir = Path(payload_dir)
        if layout is None:
            digest = payload_hash(payload_dir, exports)
        else:
            layout_file = payload_dir / "layout.json"
            write_json_atomic(layout_file, layout)
            digest = hashlib.sha256(layout_file.read_bytes()).hexdigest()
        with self._index_lock:
            index = self._load_index()
            entries = index["backups"]
            pending = self._retry_cleanup(index)
            newest = max(entries, key=lambda e: e.get("created_at", ""), default=None)
            if self._matches_latest(newest, digest, manifest):
                logger.info("布局与最新备份一致，跳过建档")
                shutil.rmtree(payload_dir, ignore_errors=True)
                return False, newest, "skip"

            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            dir_name = f"backup_{stamp}_{uuid.uuid4().hex[:6]}"
            final_dir = self._base / dir_name
            created = now_iso()
            entry: Dict[str, Any] = {
                "id": uuid.uuid4().hex,
                "created_at": created,
                "last_used_at": created,
                "source": source,
                "dir": dir_name,
                "icon_count": len(manifest),
                "payload_hash": digest,
                "reg_files": exports,
            }
            if layout is not None:
                entry["layout_format"] = "shell"
            entries.append(entry)
            evicted_dirs = []
            while len(entries) > self.max_backups:
                victim = min(
                    entries,
                    key=lambda item: (item.get("last_used_at", ""), item.get("created_at", "")),
                )
                entries.remove(victim)
                evicted_dirs.append(victim.get("dir"))

            with (payload_dir / "manifest.json").open("w", encoding="utf-8") as fh:
                json.dump({"items": manifest, "backup": entry}, fh, ensure_ascii=False, indent=2)
            payload_dir.rename(final_dir)
            try:
                index["pending_cleanup"] = pending + evicted_dirs
                self._save_index(index)
            except Exception:
                self._cleanup_dirs([dir_name], [])
                raise
            self._cleanup_dirs(evicted_dirs, entries)
            return True, entry, "created"

    def _cleanup_dirs(self, names: list, entries: List[Dict[str, Any]]) -> List[str]:
        active = {
            item["dir"].lower() for item in entries if isinstance(item.get("dir"), str)
        }
        remaining = []
        for name in names:
            if not isinstance(name, str) or not _BACKUP_DIR_NAME.fullmatch(name):
                logger.warning("忽略无效的待清理备份目录: %r", name)
                continue
            if name.lower() in active:
                continue
            marker = self._base / f".{name}.deleted"
            try:
                try:
                    with marker.open("xb"):
                        pass
                except FileExistsError:
                    pass
                self._remove_dir(name)
                marker.unlink(missing_ok=True)
            except (OSError, ValueError):
                logger.warning("清理备份目录失败: %s", name, exc_info=True)
                remaining.append(name)
        return remaining

    def _remove_dir(self, dir_name: str) -> None:
        target = self.backup_directory(dir_name)
        try:
            shutil.rmtree(target)
        except FileNotFoundError:
            if target.exists():
                raise

    def delete_entry(self, entry_id: str) -> None:
        with self._index_lock:
            index = self._load_index()
            entries = index["backups"]
            for position, entry in enumerate(entries):
                if entry.get("id") == entry_id:
                    pending = self._retry_cleanup(index)
                    removed = [entry.get("dir")]
                    del entries[position]
                    index["pending_cleanup"] = pending + removed
                    self._save_index(index)
                    self._cleanup_dirs(removed, entries)
                    return
        raise KeyError("备份不存在")

    def clear_all(self) -> int:
        with self._index_lock:
            index = self._load_index()
            pending = self._retry_cleanup(index)
            removed = [entry.get("dir") for entry in index["backups"]]
            index["backups"] = []
            index["pending_cleanup"] = pending + removed
            self._save_index(index)
            self._cleanup_dirs(removed, [])
            return len(removed)

    def mark_used(self, entry_id: str) -> Dict[str, Any]:
        with self._index_lock:
            index = self._load_index()
            for entry in index["backups"]:
                if entry.get("id") == entry_id:
                    entry["last_used_at"] = now_iso()
                    self._save_index(index)
                    try:
                        manifest = self._read_manifest(entry["dir"])
                        manifest["backup"] = entry
                        write_json_atomic(self.manifest_file(entry["dir"]), manifest)
                    except _BACKUP_ERRORS:
                        logger.warning("无法更新备份目录中的最后使用时间", exc_info=True)
                    return entry
        raise KeyError("备份不存在")

