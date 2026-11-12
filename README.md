# 桌面图标备份助手

Windows 桌面图标布局备份与还原工具。开机自启后驻留系统托盘，自动备份桌面图标位置与排列设置，最多保留 5 份，超出时删除“最久未使用”的备份；可从列表中选择备份一键恢复，恢复后默认自动重启资源管理器立即生效。

## 运行源码

需要 Python 3.10+，Windows 10/11。

```powershell
python -m pip install -r requirements.txt
python run_app.py
```

开机自启、开机自动备份、恢复后自动重启资源管理器三个开关均在窗口“设置”区，勾选后立即生效。

备份数据存放在程序目录的 `backup\<Windows用户名>\` 下，包括 `index.json`、`settings.json`、`app.log` 和每一份备份目录（内含 `.reg` 布局快照与 `manifest.json`）。

## 打包 exe

```powershell
.\build.ps1
```

产物为 `dist\DesktopIconBackup.exe`，单文件、无控制台窗口，可直接复制到任意目录运行。放在程序目录下运行时，备份目录固定为 exe 所在目录的 `backup\<Windows用户名>\`；若目录不可写，程序会显示错误状态并禁用备份/恢复，不会静默更换目录。

## 运行检查

```powershell
python run_app.py --check
```

输出 Python、tkinter、pystray、Pillow、reg.exe、桌面布局键与数据目录的检查结果。

## 测试

```powershell
python -m unittest discover -s tests -v
```

覆盖注册表键定位、导出/导入流程、载荷哈希去重、LRU 淘汰、备份删除/清空、损坏备份容错、桌面清单与缺失检测、设置持久化和自启开关。

## 说明

- 布局快照通过 `reg.exe` 导出 `HKCU\Software\Microsoft\Windows\Shell\Bags\*\Desktop` 全键，不解析二进制，也不触碰 `BagMRU`。
- 备份中记录的图标源文件已删除或改名时，恢复后该位置保持空白、不补位、不显示占位符，程序会在结果中列出缺失项。
- 全部操作只作用于当前 Windows 用户（HKCU），不需要管理员权限；恢复会替换当前桌面布局为所选备份，不做合并。
