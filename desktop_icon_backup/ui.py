from __future__ import annotations

import logging
import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from . import config
from .explorer import restart_explorer

logger = logging.getLogger(__name__)


class AppUI:
    def __init__(self, app):
        self.app = app
        self._command_queue: "queue.Queue" = queue.Queue()
        self.entries = {}
        self.root = tk.Tk()
        self.root.title(config.APP_NAME)
        self.root.geometry("780x560")
        self.root.minsize(700, 480)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        self.status_var = tk.StringVar(value="就绪")
        self._build_widgets()
        self._poll_queue()

    def _build_widgets(self) -> None:
        self._build_backup_list()
        self._build_actions()
        self._build_settings()
        self._build_result()
        ttk.Separator(self.root, orient="horizontal").pack(fill="x", padx=10, pady=4)
        status = ttk.Label(
            self.root,
            textvariable=self.status_var,
            anchor="w",
        )
        status.pack(fill="x", side="bottom", padx=12, pady=(4, 8))

    def _build_backup_list(self) -> None:
        frame = ttk.LabelFrame(self.root, text="备份列表（最多保留 5 份）")
        frame.pack(fill="both", expand=True, padx=10, pady=(10, 6))
        columns = ("status", "created", "source", "used", "count")
        self._tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "status": ("状态", 70),
            "created": ("创建时间", 160),
            "source": ("来源", 60),
            "used": ("最后使用", 160),
            "count": ("图标数", 70),
        }
        for key, (text, width) in headings.items():
            self._tree.heading(key, text=text)
            self._tree.column(key, width=width, anchor="center")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=scroll.set)
        self._tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _build_actions(self) -> None:
        frame = ttk.Frame(self.root)
        frame.pack(fill="x", padx=10, pady=6)
        self._action_buttons = {}
        for text, command in (
            ("立即备份", self.run_backup),
            ("恢复选中", self.restore_selected),
            ("删除选中", self.delete_selected),
            ("清空全部", self.clear_all),
            ("刷新", self.refresh),
        ):
            button = ttk.Button(frame, text=text, command=command)
            button.pack(side="left", padx=(0, 8))
            if text != "刷新":
                self._action_buttons[text] = button

    def _build_settings(self) -> None:
        frame = ttk.LabelFrame(self.root, text="设置")
        frame.pack(fill="x", padx=10, pady=6)
        self.autostart_var = tk.IntVar(value=1)
        self.auto_backup_var = tk.IntVar(value=1)
        self.restart_var = tk.IntVar(value=1)
        self.autostart_check = ttk.Checkbutton(
            frame,
            text="开机自启",
            variable=self.autostart_var,
            command=self._on_setting_changed,
        )
        self.autostart_check.pack(side="left", padx=(8, 12))
        self.auto_backup_check = ttk.Checkbutton(
            frame,
            text="开机自动备份",
            variable=self.auto_backup_var,
            command=self._on_setting_changed,
        )
        self.auto_backup_check.pack(side="left", padx=(0, 12))
        self.restart_check = ttk.Checkbutton(
            frame,
            text="恢复后自动重启资源管理器",
            variable=self.restart_var,
            command=self._on_setting_changed,
        )
        self.restart_check.pack(side="left", padx=(0, 12))
        ttk.Button(frame, text="重启资源管理器", command=self.restart_explorer_click).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(frame, text="打开备份目录", command=self.open_backup_dir).pack(side="left")
        self.data_dir_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.data_dir_var, foreground="#666666").pack(
            side="right", padx=8
        )

    def _build_result(self) -> None:
        frame = ttk.LabelFrame(self.root, text="最近结果")
        frame.pack(fill="x", padx=10, pady=(2, 8))
        self.result_text = tk.Text(frame, height=8, state="disabled", wrap="none")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.result_text.yview)
        self.result_text.configure(yscrollcommand=scroll.set)
        self.result_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def start(self) -> None:
        self.refresh()
        self.root.mainloop()

    def refresh(self) -> None:
        self.entries = {
            entry["id"]: entry
            for entry in self.app.store.list_entries()
        }
        self._tree.delete(*self._tree.get_children())
        for entry in sorted(
            self.entries.values(),
            key=lambda e: e.get("created_at", ""),
            reverse=True,
        ):
            status = "损坏" if entry.get("broken") else "正常"
            source = "开机" if entry.get("source") == "auto" else "手动"
            values = (
                status,
                str(entry.get("created_at", "")).replace("T", " "),
                source,
                str(entry.get("last_used_at", "")).replace("T", " "),
                str(entry.get("icon_count", "")),
            )
            self._tree.insert("", "end", iid=entry["id"], values=values)
        self._set_setting_display()

    def _set_setting_display(self) -> None:
        settings = self.app.settings
        self.autostart_var.set(int(settings.autostart_enabled))
        self.auto_backup_var.set(int(settings.auto_backup_enabled))
        self.restart_var.set(int(settings.restart_explorer_on_restore))
        self.data_dir_var.set(str(self.app.data_dir))

    def set_storage_error(self, enabled: bool) -> None:
        state = "disabled" if enabled else "normal"
        for button in self._action_buttons.values():
            button.configure(state=state)
        for check in (
            self.autostart_check,
            self.auto_backup_check,
            self.restart_check,
        ):
            check.configure(state=state)
        if enabled:
            self.status_var.set("存储目录不可写，备份/恢复已禁用")
            self.append_result("存储目录不可写，请检查程序目录权限后重试。")

    def _on_setting_changed(self) -> None:
        self.app.settings = config.Settings(
            autostart_enabled=bool(self.autostart_var.get()),
            auto_backup_enabled=bool(self.auto_backup_var.get()),
            restart_explorer_on_restore=bool(self.restart_var.get()),
        )
        try:
            self.app.persist_settings()
            self.status_var.set("设置已保存")
        except Exception as exc:
            logger.exception("保存设置失败")
            self._error(exc)

    def run_backup(self) -> None:
        if self._busy():
            return
        self.status_var.set("正在备份...")
        self._run_worker(
            lambda: self.app.perform_backup("manual"),
            self._backup_done,
            self._error,
        )

    def _backup_done(self, result) -> None:
        created, message = result
        self.status_var.set(message)
        self.append_result(message)
        self.refresh()
        if created:
            self.app.notify(message)

    def restore_selected(self) -> None:
        if self._busy():
            return
        selection = self._tree.selection()
        if not selection:
            messagebox.showinfo(config.APP_NAME, "请先在列表中选择一份备份。")
            return
        entry = self.entries.get(selection[0])
        if entry is None:
            return
        if entry.get("broken"):
            messagebox.showwarning(
                config.APP_NAME,
                "该备份已损坏，无法恢复。请删除后重新备份。",
            )
            return
        warn = ""
        if self.app.settings.restart_explorer_on_restore:
            warn = "\n恢复后会重启资源管理器，当前打开的资源管理器窗口会关闭。"
        if not messagebox.askyesno(
            config.APP_NAME,
            f"确定恢复到 {entry.get('created_at', '')} 的备份吗？{warn}",
        ):
            return
        self.status_var.set("正在恢复...")
        self._run_worker(
            lambda: self.app.perform_restore(entry["id"]),
            self._restore_done,
            self._error,
        )

    def _restore_done(self, message: str) -> None:
        self.status_var.set("恢复完成")
        self.append_result(message)
        self.refresh()
        self.app.notify(message.splitlines()[0])

    def delete_selected(self) -> None:
        selection = self._tree.selection()
        if not selection:
            return
        entry = self.entries.get(selection[0])
        if entry is None:
            return
        if not messagebox.askyesno(
            config.APP_NAME,
            "确定删除这份备份吗？此操作不可撤销。",
        ):
            return
        try:
            self.app.store.delete_entry(entry["id"])
            message = f"已删除备份 {entry.get('created_at', '')}"
            self.status_var.set(message)
            self.append_result(message)
            self.refresh()
        except Exception as exc:
            logger.exception("删除备份失败")
            self._error(exc)

    def clear_all(self) -> None:
        if not self.entries:
            return
        if not messagebox.askyesno(
            config.APP_NAME,
            "确定清空全部备份吗？此操作不可撤销。",
        ):
            return
        try:
            count = self.app.store.clear_all()
            message = f"已清空 {count} 份备份"
            self.status_var.set(message)
            self.append_result(message)
            self.refresh()
        except Exception as exc:
            logger.exception("清空备份失败")
            self._error(exc)

    def restart_explorer_click(self) -> None:
        if self._busy():
            return
        if not messagebox.askyesno(
            config.APP_NAME,
            "重启资源管理器会关闭当前打开的文件资源管理器窗口，继续吗？",
        ):
            return
        self.status_var.set("正在重启资源管理器...")
        self._run_worker(
            restart_explorer,
            lambda: self._simple_done("资源管理器已重启"),
            self._error,
        )

    def open_backup_dir(self) -> None:
        self.app.data_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(self.app.data_dir))

    def show_window(self) -> None:
        self.refresh()
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_window(self) -> None:
        self.root.withdraw()

    def exit_app(self) -> None:
        self.app.request_exit()

    def _simple_done(self, message: str) -> None:
        self.status_var.set(message)
        self.append_result(message)

    def _busy(self) -> bool:
        if self.status_var.get().startswith("正在"):
            messagebox.showinfo(config.APP_NAME, "当前有任务正在执行，请稍候。")
            return True
        return False

    def _run_worker(self, fn, done, failed) -> None:
        def target() -> None:
            try:
                result = fn()
                self.root.after(0, done, result)
            except Exception as exc:
                logger.exception("后台任务失败")
                self.root.after(0, failed, exc)

        threading.Thread(target=target, daemon=True).start()

    def _error(self, exc) -> None:
        message = str(exc) or exc.__class__.__name__
        self.status_var.set("操作失败")
        self.append_result(f"操作失败：{message}")
        self.app.notify(f"操作失败：{message[:120]}")
        messagebox.showerror(config.APP_NAME, message)

    def append_result(self, message: str) -> None:
        from .json_io import now_iso

        line = f"[{now_iso()}] {message}\n"
        self.result_text.configure(state="normal")
        self.result_text.insert("end", line)
        self.result_text.see("end")
        self.result_text.configure(state="disabled")

    def post_command(self, fn) -> None:
        self._command_queue.put(fn)

    def _poll_queue(self) -> None:
        try:
            while True:
                command = self._command_queue.get_nowait()
                try:
                    command()
                except Exception:
                    logger.exception("执行托盘命令失败")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)
