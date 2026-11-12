from __future__ import annotations

import logging
import threading

from PIL import Image, ImageDraw

from .config import APP_NAME

logger = logging.getLogger(__name__)

_NORMAL_COLOR = (47, 128, 237, 255)
_ERROR_COLOR = (231, 76, 60, 255)


def make_icon_image(color=_NORMAL_COLOR, size: int = 64):
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = size // 10
    draw.ellipse(
        (margin, margin, size - margin - 1, size - margin - 1),
        fill=color,
    )
    unit = size // 8
    inner = size // 10
    for row in range(2):
        for col in range(2):
            x0 = inner * 3 + col * unit
            y0 = inner * 3 + row * unit
            draw.rounded_rectangle(
                (x0, y0, x0 + unit - 2, y0 + unit - 2),
                radius=3,
                fill=(255, 255, 255, 255),
            )
    return image


def build_menu(on_backup, on_open, on_restart, on_exit):
    import pystray
    from pystray import MenuItem

    return pystray.Menu(
        MenuItem("立即备份", on_backup, default=True),
        MenuItem("打开窗口", on_open),
        MenuItem("重启资源管理器", on_restart),
        MenuItem("退出", on_exit),
    )


class TrayController:
    def __init__(self, menu, title: str = APP_NAME):
        self._menu = menu
        self._title = title
        self._icon = None
        self._thread = None
        self._error_state = False

    def start(self) -> bool:
        try:
            import pystray  # noqa: F401
        except ImportError:
            logger.exception("pystray 不可用，托盘功能禁用")
            return False
        import pystray

        self._icon = pystray.Icon(
            "DesktopIconBackupAssistant",
            make_icon_image(_NORMAL_COLOR),
            self._title,
            self._menu,
        )
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()
        return True

    def _run(self) -> None:
        try:
            self._icon.run()
        except Exception:
            logger.exception("托盘图标运行失败")

    def set_error(self, enabled: bool) -> None:
        self._error_state = enabled
        if self._icon is not None:
            self._icon.icon = make_icon_image(
                _ERROR_COLOR if enabled else _NORMAL_COLOR
            )

    def notify(self, message: str, title: str | None = None) -> None:
        if self._icon is None:
            return
        try:
            self._icon.notify(message, title or self._title)
        except Exception:
            logger.exception("发送托盘通知失败")

    def stop(self) -> None:
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                logger.exception("停止托盘图标失败")
