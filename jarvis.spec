# -*- mode: python -*-
"""PyInstaller 打包配置（P8-3）：在 Sun 的 Windows 电脑上跑。

产出两个 exe：
  jarvis.exe      命令行版（chat.py，带控制台）
  jarvis-gui.exe  桌面版（gui.py，不带控制台）
  jarvis-tui.exe  全屏终端版（tui.py，带控制台；U0–U6 新增，Textual）

用法（Windows PowerShell，项目目录下）：
    .\build-exe.ps1

注意：不要在 Linux 沙箱里跑这个——打出来的是 Linux 二进制，
冒充不了 Windows exe。构建脚本只在 Windows 上跑。
"""

block_cipher = None

# Textual 动态 import 很多子模块（CSS 解析、驱动、widgets 子包），
# 静态分析收不全：jarvis-tui.exe 运行时 import textual 直接失败
# （2026-11-22 真机：“没装 textual”）。整包收进来最稳妥。
from PyInstaller.utils.hooks import collect_all as _collect_all

_tui_datas, _tui_binaries, _tui_hidden = _collect_all("textual")

common_datas = [
    ("skills", "skills"),          # 内置技能（code-review 等 4 个）
    (".env.example", "."),         # 首次配置的模板
    ("mcp.json.example", "."),     # MCP 配置示例
]

common_hidden = [
    "openai", "dotenv", "charset_normalizer",
]

a_cli = Analysis(
    ["jarvis/ui/chat.py"],
    pathex=[SPECPATH],
    binaries=[],
    datas=common_datas,
    hiddenimports=common_hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)
pyz_cli = PYZ(a_cli.pure, a_cli.zipped_data, cipher=block_cipher)
exe_cli = EXE(
    pyz_cli,
    a_cli.scripts,
    a_cli.binaries,
    a_cli.zipfiles,
    a_cli.datas,
    name="jarvis",
    console=True,
    icon=None,
)

a_gui = Analysis(
    ["jarvis/ui/gui.py"],
    pathex=[SPECPATH],
    binaries=[],
    datas=common_datas,
    hiddenimports=common_hidden + ["PySide6"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)
pyz_gui = PYZ(a_gui.pure, a_gui.zipped_data, cipher=block_cipher)
exe_gui = EXE(
    pyz_gui,
    a_gui.scripts,
    a_gui.binaries,
    a_gui.zipfiles,
    a_gui.datas,
    name="jarvis-gui",
    console=False,
    icon=None,
)

a_tui = Analysis(
    ["jarvis/ui/tui.py"],
    pathex=[SPECPATH],
    binaries=_tui_binaries,
    datas=common_datas + _tui_datas,
    hiddenimports=common_hidden + _tui_hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)
pyz_tui = PYZ(a_tui.pure, a_tui.zipped_data, cipher=block_cipher)
exe_tui = EXE(
    pyz_tui,
    a_tui.scripts,
    a_tui.binaries,
    a_tui.zipfiles,
    a_tui.datas,
    name="jarvis-tui",
    console=True,
    icon=None,
)
