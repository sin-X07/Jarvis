# -*- mode: python -*-
"""PyInstaller 打包配置（P8-3）：在 Sun 的 Windows 电脑上跑。

产出两个 exe：
  jarvis.exe      命令行版（chat.py，带控制台）
  jarvis-gui.exe  桌面版（gui.py，不带控制台）

用法（Windows PowerShell，项目目录下）：
    .\build-exe.ps1

注意：不要在 Linux 沙箱里跑这个——打出来的是 Linux 二进制，
冒充不了 Windows exe。构建脚本只在 Windows 上跑。
"""

block_cipher = None

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
