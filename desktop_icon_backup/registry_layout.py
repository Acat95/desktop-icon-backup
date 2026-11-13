from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
import tempfile
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


def _export_key(bag_path: str, dest: Path) -> None:
    reg_path = f"HKCU\\{bag_path}"
    result = _run_reg(["export", reg_path, str(dest), "/y"])
    if result.returncode != 0:
        raise RegistryOperationError(
            f"导出注册表失败: {reg_path} "
            f"({result.stdout.strip()} {result.stderr.strip()})"
        )
    if not dest.is_file() or dest.stat().st_size == 0:
        raise RegistryOperationError(f"导出的注册表文件为空或缺失: {dest}")


def export_layout(payload_dir: Path) -> List[Dict[str, str]]:
    keys = find_desktop_bag_keys()
    if not keys:
        raise LayoutNotReady("未找到桌面图标布局键，桌面视图可能尚未初始化")
    payload_dir = Path(payload_dir)
    payload_dir.mkdir(parents=True, exist_ok=True)
    exports: List[Dict[str, str]] = []
    for idx, key in enumerate(sorted(keys), start=1):
        file_name = f"desktop_{idx}.reg"
        _export_key(key, payload_dir / file_name)
        exports.append({"bag_path": key, "file": file_name})
    return exports


def _key_exists(bag_path: str) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, bag_path):
            return True
    except FileNotFoundError:
        return False


def _delete_key(bag_path: str) -> None:
    if not _key_exists(bag_path):
        return
    result = _run_reg(["delete", f"HKCU\\{bag_path}", "/f"])
    if result.returncode != 0:
        raise RegistryOperationError(
            f"删除桌面 Bag 键失败: {bag_path} "
            f"({result.stdout.strip()} {result.stderr.strip()})"
        )


def _import_file(source: Path) -> None:
    result = _run_reg(["import", str(source)])
    if result.returncode != 0:
        raise RegistryOperationError(
            f"导入注册表失败: {source.name} "
            f"({result.stdout.strip()} {result.stderr.strip()})"
        )


def _validate_reg_file(data: bytes, bag_path: str, file_name: str) -> None:
    try:
        # reg.exe export on supported Windows versions writes UTF-16 with a BOM.
        lines = data.decode("utf-16").split("\n")
    except UnicodeError as exc:
        raise RegistryOperationError(f"备份注册表文件编码无效: {file_name}") from exc
    if lines[0].rstrip("\r") != "Windows Registry Editor Version 5.00":
        raise RegistryOperationError(f"备份注册表文件头无效: {file_name}")
    root = f"HKEY_CURRENT_USER\\{bag_path}".casefold()
    has_root = False
    for line in lines[1:]:
        section = line.strip()
        if not section.startswith("["):
            continue
        key = section[1:-1].casefold()
        if not section.endswith("]") or (key != root and not key.startswith(root + "\\")):
            raise RegistryOperationError(f"备份注册表文件包含无效的键: {file_name}")
        has_root |= key == root
    if not has_root:
        raise RegistryOperationError(f"备份注册表文件缺少目标桌面键: {file_name}")


def _stage_restore_files(
    reg_files: List[Dict[str, str]], backup_dir: Path, staged_dir: Path
) -> Dict[str, Path]:
    targets = {}
    seen_keys = set()
    bag_prefix = BAGS_BASE_KEY.casefold() + "\\"
    for item in reg_files:
        if not isinstance(item, dict):
            raise RegistryOperationError("备份中的注册表文件信息不完整")
        file_name = item.get("file")
        bag_path = item.get("bag_path")
        if not isinstance(file_name, str) or not isinstance(bag_path, str):
            raise RegistryOperationError("备份中的注册表文件信息不完整")
        normalized_key = bag_path.casefold()
        key_parts = normalized_key.removeprefix(bag_prefix).split("\\")
        if (
            not normalized_key.startswith(bag_prefix)
            or len(key_parts) < 2
            or not all(key_parts)
            or key_parts[-1] != "desktop"
            or "desktop" in key_parts[:-1]
            or normalized_key in seen_keys
        ):
            raise RegistryOperationError(f"备份中的桌面布局键无效或重复: {bag_path}")
        if (
            Path(file_name).name != file_name
            or Path(file_name).suffix.lower() != ".reg"
        ):
            raise RegistryOperationError(f"备份注册表文件名无效: {file_name}")
        source = backup_dir / file_name
        if source.resolve().parent != backup_dir or not source.is_file():
            raise RegistryOperationError(f"备份注册表文件缺失或路径无效: {file_name}")
        data = source.read_bytes()
        _validate_reg_file(data, bag_path, file_name)
        staged_file = staged_dir / file_name
        with staged_file.open("xb") as fh:
            fh.write(data)
        seen_keys.add(normalized_key)
        targets[bag_path] = staged_file
    if not targets:
        raise RegistryOperationError("备份中没有桌面布局键")
    return targets


def import_layout(
    reg_files: List[Dict[str, str]], backup_dir: Path, *, expected_hash: str | None = None
) -> None:
    backup_dir = Path(backup_dir).resolve()
    work_dir = Path(tempfile.mkdtemp(prefix="desktop_icon_rollback_"))
    snapshot_dir = work_dir / "previous"
    keep_snapshot = False
    try:
        staged_dir = work_dir / "incoming"
        staged_dir.mkdir()
        targets = _stage_restore_files(reg_files, backup_dir, staged_dir)
        if expected_hash is not None and payload_hash(staged_dir, reg_files) != expected_hash:
            raise RegistryOperationError("备份注册表文件校验失败")

        snapshot_dir.mkdir()
        snapshots = {}
        for idx, bag_path in enumerate(targets, start=1):
            if _key_exists(bag_path):
                snapshot = snapshot_dir / f"desktop_{idx}.reg"
                _export_key(bag_path, snapshot)
                _validate_reg_file(snapshot.read_bytes(), bag_path, snapshot.name)
                snapshots[bag_path] = snapshot
        logger.info("恢复前的桌面布局快照目录: %s", snapshot_dir)

        touched = []
        keep_snapshot = True
        try:
            for bag_path, source in targets.items():
                # A failed or timed-out command may already have changed this key.
                touched.append(bag_path)
                _delete_key(bag_path)
                _import_file(source)
        except Exception as exc:
            rollback_errors = []
            for bag_path in reversed(touched):
                try:
                    _delete_key(bag_path)
                    if bag_path in snapshots:
                        _import_file(snapshots[bag_path])
                except Exception:
                    logger.exception("回滚桌面 Bag 键失败: %s", bag_path)
                    rollback_errors.append(bag_path)
            if rollback_errors:
                raise RegistryOperationError(
                    f"恢复失败，部分布局未能回滚。恢复前的快照目录：{snapshot_dir}\n"
                    f"原始错误：{exc}"
                ) from exc
            keep_snapshot = False
            raise RegistryOperationError(f"恢复失败，已回滚注册表布局：{exc}") from exc
        keep_snapshot = False
    finally:
        if not keep_snapshot:
            shutil.rmtree(work_dir, ignore_errors=True)
    logger.info("恢复注册表布局完成: %d 个键", len(targets))


def payload_hash(payload_dir: Path, exports: List[Dict[str, str]]) -> str:
    sha = hashlib.sha256()
    for item in sorted(exports, key=lambda e: e.get("bag_path", "")):
        data = (Path(payload_dir) / item["file"]).read_bytes()
        sha.update(len(data).to_bytes(8, "little"))
        sha.update(data)
    return sha.hexdigest()

