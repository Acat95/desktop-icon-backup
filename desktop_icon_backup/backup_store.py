from __future__ import annotations

import datetime
import json
import logging
import re
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import MAX_BACKUPS
from .json_io import now_iso, read_json_strict, write_json_atomic
from .registry_layout import backup_reg_file, payload_hash

logger = logging.getLogger(__name__)
_BACKUP_DIR_NAME = re.compile(r"backup_[0-9]{8}_[0-9]{6}_[0-9a-f]{6}", re.IGNORECASE | re.ASCII)
_PAYLOAD_HASH = re.compile(r"[0-9a-f]{64}")


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
        index = read_json_strict(self._index_path(), {"backups": []})
        if not isinstance(index, dict) or not isinstance(index.get("backups"), list):
            logger.warning("index.json 结构异常，重置索引")
            index = {"backups": []}
        return index

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
        path = self.backup_directory(dir_name) / "manifest.json"
        if path.resolve() != path or not path.is_file():
            raise FileNotFoundError("清单文件缺失或路径无效")
        return path

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
                self.manifest_file(item["dir"])
                digest = item.get("payload_hash")
                if not isinstance(digest, str) or not _PAYLOAD_HASH.fullmatch(digest):
                    raise ValueError("备份校验值缺失或无效")
                reg_files = item.get("reg_files") or []
                if not isinstance(reg_files, list) or not reg_files:
                    raise FileNotFoundError("注册表文件信息缺失")
                for reg_info in reg_files:
                    if not isinstance(reg_info, dict) or "file" not in reg_info:
                        raise FileNotFoundError("注册表文件信息不完整")
                    backup_reg_file(entry_dir, reg_info["file"])
            except Exception as exc:
                item["broken"] = True
                item["broken_reason"] = str(exc)
            result.append(item)
        return sorted(result, key=lambda e: e.get("created_at", ""), reverse=True)

    def create_backup(
        self,
        payload_dir: Path,
        exports: List[Dict[str, str]],
        manifest: List[Dict[str, str]],
        source: str,
    ) -> Tuple[bool, Optional[Dict[str, Any]], str]:
        payload_dir = Path(payload_dir)
        digest = payload_hash(payload_dir, exports)
        with self._index_lock:
            index = self._load_index()
            entries = index["backups"]
            pending = self._retry_cleanup(index)
            newest = max(entries, key=lambda e: e.get("created_at", ""), default=None)
            if newest and newest.get("payload_hash") == digest:
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
                json.dump({"items": manifest}, fh, ensure_ascii=False, indent=2)
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
            try:
                self._remove_dir(name)
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
                    return entry
        raise KeyError("备份不存在")

