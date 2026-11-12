from __future__ import annotations

import datetime
import json
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import MAX_BACKUPS
from .json_io import now_iso, read_json_strict, write_json_atomic
from .registry_layout import payload_hash

logger = logging.getLogger(__name__)


class BackupStore:
    """Manage the at most max_backups layout snapshots with LRU eviction."""

    def __init__(self, base_dir: Path, max_backups: int = MAX_BACKUPS):
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)
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

    def list_entries(self) -> List[Dict[str, Any]]:
        entries = self._load_index().get("backups", [])
        result = []
        for entry in entries:
            item = dict(entry)
            item["broken"] = False
            item["broken_reason"] = ""
            try:
                entry_dir = self._base / item["dir"]
                if not entry_dir.is_dir():
                    raise FileNotFoundError("备份目录不存在")
                if not (entry_dir / "manifest.json").is_file():
                    raise FileNotFoundError("清单文件缺失")
                reg_files = item.get("reg_files") or []
                if not isinstance(reg_files, list) or not reg_files:
                    raise FileNotFoundError("注册表文件信息缺失")
                for reg_info in reg_files:
                    if not isinstance(reg_info, dict) or "file" not in reg_info:
                        raise FileNotFoundError("注册表文件信息不完整")
                    if not (entry_dir / reg_info["file"]).is_file():
                        raise FileNotFoundError(f"注册表文件缺失: {reg_info['file']}")
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
        index = self._load_index()
        entries = index["backups"]
        newest = max(entries, key=lambda e: e.get("created_at", ""), default=None)
        if newest and newest.get("payload_hash") == digest:
            logger.info("布局与最新备份一致，跳过建档")
            shutil.rmtree(payload_dir, ignore_errors=True)
            return False, newest, "skip"

        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        dir_name = f"backup_{stamp}_{uuid.uuid4().hex[:6]}"
        final_dir = self._base / dir_name
        try:
            payload_dir.rename(final_dir)
        except OSError:
            shutil.move(str(payload_dir), str(final_dir))
        with (final_dir / "manifest.json").open("w", encoding="utf-8") as fh:
            json.dump({"items": manifest}, fh, ensure_ascii=False, indent=2)

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
        self._evict(entries)
        index["backups"] = entries
        self._save_index(index)
        return True, entry, "created"

    def _evict(self, entries: List[Dict[str, Any]]) -> None:
        while len(entries) > self.max_backups:
            victim = min(
                entries,
                key=lambda e: (e.get("last_used_at", ""), e.get("created_at", "")),
            )
            entries.remove(victim)
            self._remove_dir(victim.get("dir", ""))
            logger.info("自动淘汰备份 %s", victim.get("id"))

    def _remove_dir(self, dir_name: str) -> None:
        if not dir_name:
            return
        target = (self._base / dir_name).resolve()
        base = self._base.resolve()
        if not str(target).startswith(str(base) + os.sep):
            raise ValueError(f"拒绝删除备份基目录之外的路径: {target}")
        shutil.rmtree(target, ignore_errors=True)

    def delete_entry(self, entry_id: str) -> None:
        index = self._load_index()
        entries = index["backups"]
        for entry in entries:
            if entry.get("id") == entry_id:
                entries.remove(entry)
                self._remove_dir(entry.get("dir", ""))
                self._save_index(index)
                return
        raise KeyError("备份不存在")

    def clear_all(self) -> int:
        index = self._load_index()
        entries = index["backups"]
        count = len(entries)
        for entry in entries:
            self._remove_dir(entry.get("dir", ""))
        index["backups"] = []
        self._save_index(index)
        return count

    def mark_used(self, entry_id: str) -> Dict[str, Any]:
        index = self._load_index()
        for entry in index["backups"]:
            if entry.get("id") == entry_id:
                entry["last_used_at"] = now_iso()
                self._save_index(index)
                return entry
        raise KeyError("备份不存在")

