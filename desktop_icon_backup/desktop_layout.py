from __future__ import annotations

import ctypes as ct
import logging
from contextlib import ExitStack, contextmanager
from ctypes.wintypes import DWORD, POINT, UINT
from dataclasses import dataclass
from uuid import UUID

logger = logging.getLogger(__name__)

_PTR = ct.c_void_p
_OUT_PTR = ct.POINTER(_PTR)
_INT = ct.c_int
_GUID = ct.c_ubyte * 16
_ARRANGE_FLAGS = 0x1 | 0x4  # FWF_AUTOARRANGE | FWF_SNAPTOGRID
_ALL_VIEW_ITEMS = 2  # SVGIO_ALLVIEW
_POSITION_ITEM = 0x80  # SVSI_POSITIONITEM


class _Variant(ct.Structure):
    # The VARIANT union can hold two pointers (BRECORD) on both architectures.
    _fields_ = [("type", ct.c_ushort), ("reserved", ct.c_ushort * 3), ("data", _PTR * 2)]


class _SortColumn(ct.Structure):
    _fields_ = [("format_id", _GUID), ("property_id", DWORD), ("direction", _INT)]


def _guid(value: str) -> _GUID:
    return _GUID.from_buffer_copy(UUID(value).bytes_le)


def _check(result: int, operation: str) -> None:
    if result < 0:
        raise OSError(f"{operation} 失败 (0x{result & 0xffffffff:08X})")


def _method(interface, slot: int, result, *argtypes):
    table = ct.cast(interface, ct.POINTER(ct.POINTER(_PTR))).contents
    return ct.WINFUNCTYPE(result, _PTR, *argtypes)(table[slot])


def _call(interface, method: str, slot: int, argtypes: tuple, *args) -> None:
    _check(_method(interface, slot, ct.c_long, *argtypes)(interface, *args), method)


def _owned_interface(stack: ExitStack, interface):
    if not interface:
        raise RuntimeError("Windows 桌面视图尚未就绪")
    stack.callback(_method(interface, 2, ct.c_ulong), interface)  # IUnknown::Release
    return interface


@contextmanager
def _desktop_view():
    ole32 = ct.windll.ole32
    initialize = ole32.CoInitializeEx
    initialize.argtypes = [_PTR, DWORD]
    initialize.restype = ct.c_long
    _check(initialize(None, 2), "CoInitializeEx")  # COINIT_APARTMENTTHREADED
    with ExitStack() as stack:
        stack.callback(ole32.CoUninitialize)
        create_instance = ole32.CoCreateInstance
        create_instance.argtypes = [_PTR, _PTR, DWORD, _PTR, _OUT_PTR]
        create_instance.restype = ct.c_long
        windows = _PTR()
        _check(create_instance(
            ct.byref(_guid("9ba05972-f6a8-11cf-a442-00a0c90a8f39")),  # CLSID_ShellWindows
            None,
            5,  # CLSCTX_INPROC_SERVER | CLSCTX_LOCAL_SERVER
            ct.byref(_guid("85cb6900-4d95-11cf-960c-0080c7f4ee85")),  # IShellWindows
            ct.byref(windows),
        ), "CoCreateInstance(ShellWindows)")
        _owned_interface(stack, windows)

        location = _Variant(type=3)  # VT_I4, with value CSIDL_DESKTOP (0)
        root = _Variant()  # VT_EMPTY
        hwnd = ct.c_long()
        dispatch, provider, browser, view, folder_view = (_PTR() for _ in range(5))
        _call(windows, "FindWindowSW", 15, (_PTR, _PTR, _INT, _PTR, _INT, _OUT_PTR),
              ct.byref(location), ct.byref(root), 8, ct.byref(hwnd), 1, ct.byref(dispatch))
        _owned_interface(stack, dispatch)
        _call(dispatch, "QueryInterface(IServiceProvider)", 0, (_PTR, _OUT_PTR),
              ct.byref(_guid("6d5140c1-7436-11ce-8034-00aa006009fa")), ct.byref(provider))
        _owned_interface(stack, provider)
        _call(provider, "QueryService(IShellBrowser)", 3, (_PTR, _PTR, _OUT_PTR),
              ct.byref(_guid("4c96be40-915c-11cf-99d3-00aa004ae837")),  # SID_STopLevelBrowser
              ct.byref(_guid("000214e2-0000-0000-c000-000000000046")), ct.byref(browser))
        _owned_interface(stack, browser)
        _call(browser, "QueryActiveShellView", 15, (_OUT_PTR,), ct.byref(view))
        _owned_interface(stack, view)
        _call(view, "QueryInterface(IFolderView2)", 0, (_PTR, _OUT_PTR),
              ct.byref(_guid("1af3a467-214f-4298-908e-06b03e0b39f9")), ct.byref(folder_view))
        _owned_interface(stack, folder_view)
        yield _FolderView(folder_view)


@dataclass(frozen=True)
class Icon:
    key: str
    name: str
    x: int
    y: int


@dataclass(frozen=True)
class ViewSettings:
    mode: int
    size: int
    flags: int
    sort: tuple[tuple[str, int, int], ...]


@dataclass(frozen=True)
class Layout:
    view: ViewSettings
    icons: tuple[Icon, ...]


@dataclass(frozen=True)
class RestoreResult:
    matched: int
    different: tuple[str, ...]
    missing: tuple[str, ...]
    settings_match: bool


def decode_layout(data) -> Layout:
    try:
        if type(data["version"]) is not int or data["version"] != 1:
            raise ValueError()
        if not isinstance(data["icons"], list):
            raise ValueError()
        icons = tuple(Icon(**item) for item in data["icons"])
        view = ViewSettings(**data["view"])
        sort = tuple((str(UUID(guid)), prop, direction) for guid, prop, direction in view.sort)
        if (
            len({icon.key for icon in icons}) != len(icons)
            or not all(
                isinstance(icon.key, str) and icon.key and isinstance(icon.name, str)
                and all(type(p) is int and -(2**31) <= p < 2**31 for p in (icon.x, icon.y))
                for icon in icons
            )
            or type(view.mode) is not int or not 1 <= view.mode <= 8
            or type(view.size) is not int or not 0 <= view.size <= 1024
            or type(view.flags) is not int or view.flags & ~_ARRANGE_FLAGS
            or any(type(prop) is not int or not 0 <= prop < 2**32
                   or type(direction) is not int or direction not in (-1, 1)
                   for _, prop, direction in sort)
        ):
            raise ValueError()
    except (AttributeError, KeyError, ValueError, TypeError) as exc:
        raise ValueError("桌面坐标备份格式无效") from exc
    return Layout(ViewSettings(view.mode, view.size, view.flags, sort), icons)


class _FolderView:
    def __init__(self, interface):
        self._interface = interface

    def _call(self, method, slot, argtypes, *args):
        _call(self._interface, method, slot, argtypes, *args)

    def settings(self) -> ViewSettings:
        mode, size, count, flags = _INT(), _INT(), _INT(), DWORD()
        self._call("GetViewModeAndIconSize", 36, (_PTR, _PTR), ct.byref(mode), ct.byref(size))
        self._call("GetCurrentFolderFlags", 25, (_PTR,), ct.byref(flags))
        self._call("GetSortColumnCount", 26, (_PTR,), ct.byref(count))
        columns = (_SortColumn * count.value)()
        if columns:
            self._call("GetSortColumns", 28, (_PTR, _INT), columns, len(columns))
        sort = tuple(
            (str(UUID(bytes_le=bytes(col.format_id))), col.property_id, col.direction)
            for col in columns
        )
        return ViewSettings(mode.value, size.value, flags.value & _ARRANGE_FLAGS, sort)

    def set_settings(self, target: ViewSettings, current: ViewSettings) -> None:
        if (target.mode, target.size) != (current.mode, current.size):
            self._call("SetViewModeAndIconSize", 35, (_INT, _INT), target.mode, target.size)
        if target.sort and target.sort != current.sort:
            columns = (_SortColumn * len(target.sort))(
                *(_SortColumn(_guid(guid), prop, direction) for guid, prop, direction in target.sort)
            )
            self._call("SetSortColumns", 27, (_PTR, _INT), columns, len(columns))

    def set_arrangement(self, flags: int) -> None:
        self._call("SetCurrentFolderFlags", 24, (DWORD, DWORD), _ARRANGE_FLAGS, flags)

    @contextmanager
    def items(self):
        shell32 = ct.windll.shell32
        free = ct.windll.ole32.CoTaskMemFree
        free.argtypes = [_PTR]
        free.restype = None
        desktop_folder = shell32.SHGetSpecialFolderLocation
        desktop_folder.argtypes = [_PTR, _INT, _OUT_PTR]
        desktop_folder.restype = ct.c_long
        combine = shell32.ILCombine
        combine.argtypes = [_PTR, _PTR]
        combine.restype = _PTR
        get_name = shell32.SHGetNameFromIDList
        get_name.argtypes = [_PTR, DWORD, _OUT_PTR]
        get_name.restype = ct.c_long

        def name(pidl, kind):
            result = _PTR()
            try:
                _check(get_name(pidl, kind, ct.byref(result)), "SHGetNameFromIDList")
                if not result:
                    raise RuntimeError("Windows 未返回桌面图标名称")
                return ct.wstring_at(result)
            finally:
                free(result)

        with ExitStack() as stack:
            root = _PTR()
            _check(desktop_folder(None, 0, ct.byref(root)), "SHGetSpecialFolderLocation")
            stack.callback(free, root)
            count = _INT()
            self._call("ItemCount", 7, (UINT, _PTR), _ALL_VIEW_ITEMS, ct.byref(count))
            found = {}
            for index in range(count.value):
                child = _PTR()
                self._call("Item", 6, (_INT, _OUT_PTR), index, ct.byref(child))
                if not child:
                    raise RuntimeError("读取桌面图标时桌面视图发生变化，请重试")
                stack.callback(free, child)
                absolute = combine(root, child)
                if not absolute:
                    raise MemoryError("无法分配桌面图标标识")
                try:
                    key = name(absolute, 0x80028000)  # SIGDN_DESKTOPABSOLUTEPARSING
                    display_name = name(absolute, 0)  # SIGDN_NORMALDISPLAY
                finally:
                    free(absolute)
                if not key or key in found:
                    raise RuntimeError(f"无法唯一识别桌面图标：{display_name}")
                position = POINT()
                self._call("GetItemPosition", 11, (_PTR, _PTR), child, ct.byref(position))
                found[key] = (Icon(key, display_name, position.x, position.y), child)
            yield found

    def position(self, icons: tuple[Icon, ...]) -> None:
        with self.items() as items:
            pairs = [(items[icon.key][1], POINT(icon.x, icon.y))
                     for icon in icons if icon.key in items]
            if not pairs:
                return
            pidls, points = zip(*pairs)
            self._call("SelectAndPositionItems", 16, (UINT, _PTR, _PTR, DWORD),
                       len(pairs), (_PTR * len(pairs))(*pidls),
                       (POINT * len(pairs))(*points), _POSITION_ITEM)

    def snapshot(self) -> Layout:
        settings = self.settings()
        with self.items() as items:
            return Layout(settings, tuple(items[key][0] for key in sorted(items)))


def capture() -> Layout:
    with _desktop_view() as view:
        return view.snapshot()


def _apply(view: _FolderView, layout: Layout, current: ViewSettings) -> None:
    view.set_settings(layout.view, current)
    # Temporarily disable Windows' automatic repositioning while placing items.
    if layout.icons:
        view.set_arrangement(0)
        view.position(layout.icons)
    view.set_arrangement(layout.view.flags)


def restore(layout: Layout) -> RestoreResult:
    with _desktop_view() as view:
        previous = view.snapshot()
        try:
            if layout != previous:
                _apply(view, layout, previous.view)
            actual = view.snapshot()
        except Exception as exc:
            try:
                _apply(view, previous, view.settings())
                if view.snapshot() != previous:
                    raise RuntimeError("回读结果与恢复前的桌面布局不同")
            except Exception:
                logger.exception("恢复失败后无法确认桌面布局已回滚")
                raise RuntimeError(f"恢复失败，无法确认原布局已回滚：{exc}") from exc
            raise RuntimeError(f"恢复失败，已回滚桌面布局：{exc}") from exc

    positions = {icon.key: (icon.x, icon.y) for icon in actual.icons}
    missing = tuple(icon.name for icon in layout.icons if icon.key not in positions)
    different = tuple(
        icon.name for icon in layout.icons
        if icon.key in positions and positions[icon.key] != (icon.x, icon.y)
    )
    return RestoreResult(
        len(layout.icons) - len(missing) - len(different),
        different, missing, actual.view == layout.view,
    )
