# 在 Sun 的 Windows 电脑上打包 Jarvis（P8-3）。
# 用法：在项目目录下右键“使用 PowerShell 运行”，或：
#     powershell -ExecutionPolicy Bypass -File .\build-exe.ps1
# 产出：dist\jarvis.exe（命令行版）、dist\jarvis-gui.exe（桌面版）、dist\jarvis-tui.exe（全屏终端版）。
# 注意：不要在 Linux 上跑这个脚本——PyInstaller 打出来的是当前平台的
# 二进制，Linux 上打不出 Windows exe。

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "1/3 检查 Python..."
python --version
if ($LASTEXITCODE -ne 0) { throw "没找到 python，先装 Python 3.11+ 并勾选 Add to PATH。" }

Write-Host "2/3 安装依赖..."
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r requirements-gui.txt
python -m pip install pyinstaller

Write-Host "3/3 打包（jarvis.spec）..."
# 用 python -m 调：pip 装的 pyinstaller.exe 在 Scripts 目录下，
# 那个目录常常不在 PATH 里，直接调命令会报找不到。
python -m PyInstaller --noconfirm --clean jarvis.spec

Write-Host ""
Write-Host "打包完成："
Get-ChildItem dist\*.exe | ForEach-Object { Write-Host "  $($_.FullName)  ($([math]::Round($_.Length / 1MB, 1)) MB)" }
Write-Host ""
Write-Host "首次使用：把 dist\jarvis.exe 所在的目录加进 PATH，或直接双击 dist\jarvis-gui.exe。"
Write-Host "然后在命令行跑：jarvis --version（应打印版本号）。"
