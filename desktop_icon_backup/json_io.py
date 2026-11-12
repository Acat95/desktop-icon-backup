from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        logger.warning("读取 JSON 失败 %s: %s", path, exc)
        return default


def read_json_strict(path: Path, default: Any) -> Any:
    """Read JSON, or quarantine a corrupt file and return the default."""
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        logger.exception("JSON 损坏，隔离文件 %s", path)
        try:
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
            dest = path.with_name(f"{path.stem}.corrupt-{stamp}.json")
            os.replace(str(path), str(dest))
        except Exception:
            logger.exception("隔离损坏的 JSON 失败 %s", path)
        return default


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(str(tmp), str(path))
