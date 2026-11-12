from __future__ import annotations

import hashlib
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import winreg

from .config import BAGS_BASE_KEY

logger = logging.getLogger(__name__)


class LayoutNotReady(Exception):
    pass


class RegistryOperationError(Exception):
    pass


def _open_key(hive: Any, path: str) -> Any:
    try:
        return winreg.OpenKey(hive, path, 0, winreg.KEY_READ)
    except OSError:
        return None


def _walk_keys(hive: Any, key_path: str, depth: int, out: List[str]) -> None:
    if depth > 4:
        return
    key = _open_key(hive, key_path)
    if key is None:
        return
    try:
        index = 0
        while True:
            try:
                sub_name = winreg.EnumKey(key, index)
            except OSError:
                break
            index += 1
            sub_path = f"{key_path}\\{sub_name}"
            if sub_name.lower() == "desktop":
                out.append(sub_path)
            else:
                _walk_keys(hive, sub_path, depth + 1, out)
    finally:
        winreg.CloseKey(key)


def find_desktop_bag_keys() -> List[str]:
    """Recursively locate HKCU\\...\\Bags\\*\\Desktop layout keys."""
    found: List[str] = []
    base = _open_key(winreg.HKEY_CURRENT_USER, BAGS_BASE_KEY)
    if base is None:
        logger.info("未找到 Bags 基础键: %s", BAGS_BASE_KEY)
        return found
    try:
        _walk_keys(winreg.HKEY_CURRENT_USER, BAGS_BASE_KEY, 0, found)
    finally:
        winreg.CloseKey(base)
    logger.info("找到桌面 Bag 键: %s", found)
    return found


def _run_reg(args: List[str]) -> Any:
    try:
        return subprocess.run(
            ["reg.exe"] + args,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=45,
        )
    except FileNotFoundError:
        raise RegistryOperationError("找不到 reg.exe，无法完成注册表操作")
    except subprocess.TimeoutExpired as exc:
        raise RegistryOperationError(f"reg.exe 执行超时: {' '.join(args)}")


def export_layout(payload_dir: Path) -> List[Dict[str, str]]:
    keys = find_desktop_bag_keys()
    if not keys:
        raise LayoutNotReady("未找到桌面图标布局键，桌面视图可能尚未初始化")
    payload_dir = Path(payload_dir)
    payload_dir.mkdir(parents=True, exist_ok=True)
    exports: List[Dict[str, str]] = []
    for idx, key in enumerate(sorted(keys), start=1):
        file_name = f"desktop_{idx}.reg"
        dest = payload_dir / file_name
        reg_path = f"HKCU\\{key}"
        result = _run_reg(["export", reg_path, str(dest), "/y"])
        if result.returncode != 0:
            raise RegistryOperationError(
                f"导出注册表失败: {reg_path} "
                f"({result.stdout.strip()} {result.stderr.strip()})"
            )
        if not dest.exists() or dest.stat().st_size == 0:
            raise RegistryOperationError(f"导出的注册表文件为空: {dest}")
        exports.append({"bag_path": key, "file": file_name})
    return exports


def import_layout(reg_files: List[Dict[str, str]], backup_dir: Path) -> None:
    backup_dir = Path(backup_dir)
    resolved = []
    for item in reg_files:
        try:
            file_name = item["file"]
            bag_path = item["bag_path"]
        except (KeyError, TypeError):
            raise RegistryOperationError("备份中的注册表文件信息不完整")
        source = backup_dir / file_name
        if not source.is_file():
            raise RegistryOperationError(f"备份注册表文件缺失: {source.name}")
        resolved.append((bag_path, source))
    for bag_path, _ in resolved:
        result = _run_reg(["delete", f"HKCU\\{bag_path}", "/f"])
        if result.returncode != 0:
            logger.info("删除桌面 Bag 键未生效（可能不存在）: %s", bag_path)
    for bag_path, source in resolved:
        result = _run_reg(["import", str(source)])
        if result.returncode != 0:
            raise RegistryOperationError(
                f"导入注册表失败: {source.name} "
                f"({result.stdout.strip()} {result.stderr.strip()})"
            )
    logger.info("恢复注册表布局完成: %d 个键", len(resolved))


def payload_hash(payload_dir: Path, exports: List[Dict[str, str]]) -> str:
    sha = hashlib.sha256()
    for item in sorted(exports, key=lambda e: e.get("bag_path", "")):
        data = (Path(payload_dir) / item["file"]).read_bytes()
        sha.update(len(data).to_bytes(8, "little"))
        sha.update(data)
    return sha.hexdigest()

