# 桌面图标备份助手

Windows 桌面图标布局备份与还原工具。开机自启后驻留系统托盘，自动备份当前桌面图标坐标和排列设置，最多保留 5 份，超出时删除最久未使用的备份。恢复时，程序会回读坐标并报告匹配结果。

## 运行源码

需要 Python 3.10+，Windows 10/11。

```powershell
python -m pip install -r requirements.txt
python run_app.py
```

开机自启、开机自动备份、恢复旧版备份时重启资源管理器三个开关均在窗口“设置”区。

备份数据存放在程序目录的 `backup\<Windows用户名>\` 下，包括索引、设置、日志和各份备份。新版备份保存 `layout.json` 和 `manifest.json`，旧版备份保存 `.reg` 快照和 `manifest.json`。

## 打包 exe

```powershell
.\build.ps1
```

产物为 `dist\DesktopIconBackup.exe`，单文件、无控制台窗口，可直接复制到任意目录运行。放在程序目录下运行时，备份目录固定为 exe 所在目录的 `backup\<Windows用户名>\`；若目录不可写，程序会显示错误状态并禁用备份/恢复，不会静默更换目录。

## 运行检查

```powershell
python run_app.py --check
```

输出 Python、tkinter、pystray、Pillow、reg.exe、可读取的桌面图标坐标数和数据目录。

## 测试

```powershell
python -m unittest discover -s tests -v
```

覆盖注册表键定位、导出/导入流程、载荷哈希去重、LRU 淘汰、备份删除/清空、损坏备份容错、桌面清单与缺失检测、设置持久化和自启开关。

## 说明

- 新版备份通过 Windows Shell 接口保存实际坐标、图标大小和排列设置，并直接在当前桌面恢复。自动排列、缩放或显示器变化可能使 Windows 重新定位图标，结果会列出坐标不同或当前桌面缺失的项目。
- 旧版注册表备份仍可导入。结果会提示坐标尚未验证，默认会重启资源管理器。
- 自动备份与手动备份、恢复、删除及清空按提交顺序执行。操作只作用于当前 Windows 用户，无需管理员权限。
