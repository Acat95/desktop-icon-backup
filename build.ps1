$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

python -m pip install -r requirements.txt
python -m pip install -r requirements-build.txt
python tools\make_icon.py
python -m PyInstaller --noconfirm --clean --onefile --noconsole --name DesktopIconBackup --icon assets\app.ico run_app.py

Write-Host ""
Write-Host "打包完成：dist\DesktopIconBackup.exe"

