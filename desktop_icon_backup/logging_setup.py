from __future__ import annotations

import logging
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def init_logging(log_file: Path, console: bool = True) -> logging.Logger:
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        logging.getLogger(__name__).exception("创建日志目录失败 %s", log_file.parent)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    formatter = logging.Formatter(_FORMAT)
    try:
        file_handler = logging.FileHandler(str(log_file), encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except Exception:
        logging.getLogger(__name__).exception("初始化文件日志失败 %s", log_file)
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)
    return logging.getLogger("app")
