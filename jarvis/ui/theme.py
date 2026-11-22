"""设计 token（U0-3）：`theme.json` 是唯一事实来源。

三个界面（桌面 QSS / 网页 CSS / TUI 与 CLI 的 ANSI）不再各写一遍
颜色，全部从同一份 JSON 生成：

    load_theme("dark")  -> token 字典
    to_css_vars(theme)  -> ":root { --color-bg: ... }"（网页版用）
    to_qss(theme)       -> Qt 样式表字符串（桌面版用）
    to_textual_css(theme) -> Textual CSS 变量块（全屏 TUI 用）
    to_ansi_roles(theme)-> {角色: ANSI 转义码}（CLI inline fallback 用）

换主题 = 换一份 JSON，不动组件代码。组件里不许出现十六进制颜色
字面量，只许引用语义角色（bg / surface / text / accent /
success / warning / danger …）。light.json 在 U6 再补。
"""

import json
import os

from jarvis.core.config import default_config_dir

_HERE = os.path.dirname(os.path.abspath(__file__))

# ANSI 颜色（CLI inline fallback 用）：角色 -> 转义码。
# 终端配色各异，这里只用最通用的 8 色 + 亮色，不追求精确还原。
_ANSI_MAP = {
    "text": "",
    "textMuted": "\x1b[90m",
    "textFaint": "\x1b[90m",
    "accent": "\x1b[94m",
    "success": "\x1b[92m",
    "warning": "\x1b[93m",
    "danger": "\x1b[91m",
    "info": "\x1b[96m",
    "diffAdd": "\x1b[92m",
    "diffDel": "\x1b[91m",
}
ANSI_RESET = "\x1b[0m"


def load_ui_prefs() -> dict:
    """~/.jarvis/ui.json：主题选择等界面偏好（U2 只有 theme）。

    本来在 tui.py，U3 挪到 theme.py——GUI 也要读它，但不能 import tui
   （那会把 Textual 拖进桌面进程）。
    """
    path = os.path.join(default_config_dir(), "ui.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_ui_prefs(prefs: dict) -> None:
    """写 ~/.jarvis/ui.json；写坏了不拦调用方。"""
    path = os.path.join(default_config_dir(), "ui.json")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(prefs, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def load_theme(name: str = "dark") -> dict:
    """读 jarvis/ui/<name>.json；找不到时回退到内置 dark 配色。

    dark 主题的文件就叫 theme.json（计划里的名字），所以 name="dark"
    时先找 dark.json、再找 theme.json；light.json 是浅色主题（U6）。
    """
    candidates = [os.path.join(_HERE, name + ".json")]
    if name == "dark":
        candidates.append(os.path.join(_HERE, "theme.json"))
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as f:
                theme = json.load(f)
            if isinstance(theme, dict) and "color" in theme:
                return theme
        except (OSError, json.JSONDecodeError):
            continue
    return _BUILTIN_DARK


_BUILTIN_DARK = {
    "name": "dark",
    "color": {
        "bg": "#1e1e2e", "surface": "#181825",
        "surfaceRaised": "#313244", "surfaceDisabled": "#45475a",
        "border": "#313244", "text": "#cdd6f4",
        "textMuted": "#a6adc8", "textFaint": "#6c7086",
        "accent": "#89b4fa", "success": "#a6e3a1",
        "warning": "#f9e2af", "danger": "#f38ba8",
        "info": "#89dceb", "diffAdd": "#a6e3a1",
        "diffDel": "#f38ba8",
    },
    "font": {"sans": "system-ui", "mono": "monospace",
             "size": {"xs": 12, "sm": 13, "md": 14, "lg": 16}},
    "spacing": {"1": 4, "2": 8, "3": 12, "4": 16},
    "radius": {"sm": 6, "md": 8},
}


def to_css_vars(theme: dict) -> str:
    """theme -> CSS 变量块（网页版 <style> 里直接插）。"""
    color = theme.get("color", {})
    font = theme.get("font", {})
    size = font.get("size", {})
    lines = [":root {"]
    for key in ("bg", "surface", "surfaceRaised", "surfaceDisabled",
                "border", "text", "textMuted", "textFaint", "accent",
                "success", "warning", "danger", "info",
                "diffAdd", "diffDel", "codeBg"):
        if key in color:
            lines.append(f"  --color-{key}: {color[key]};")
    lines.append(f"  --font-sans: {font.get('sans', 'system-ui')};")
    lines.append(f"  --font-mono: {font.get('mono', 'monospace')};")
    for key in ("xs", "sm", "md", "lg"):
        if key in size:
            lines.append(f"  --font-size-{key}: {size[key]}px;")
    lines.append("}")
    return "\n".join(lines)


def to_qss(theme: dict) -> str:
    """theme -> Qt 样式表（桌面版用）。视觉与 0.2.0 的手写 DARK_STYLE 一致。"""
    c = theme.get("color", {})
    size = theme.get("font", {}).get("size", {})
    md = size.get("md", 14)
    sm = size.get("sm", 13)
    radius = theme.get("radius", {}).get("sm", 6)  # 旧 DARK_STYLE 全是 6px
    return f"""
QMainWindow {{ background: {c['bg']}; }}
QTextEdit {{ background: {c['surface']}; color: {c['text']}; border: 1px solid {c['border']};
            border-radius: {radius}px; font-size: {md}px; }}
QLineEdit {{ background: {c['surface']}; color: {c['text']}; border: 1px solid {c['border']};
            border-radius: {radius}px; padding: 6px; font-size: {md}px; }}
QPushButton {{ background: {c['accent']}; color: {c['bg']}; border: none;
              border-radius: {radius}px; padding: 6px 16px; font-size: {md}px; }}
QPushButton:disabled {{ background: {c['surfaceDisabled']}; color: {c['textMuted']}; }}
QListWidget {{ background: {c['surface']}; color: {c['text']}; border: 1px solid {c['border']};
              border-radius: {radius}px; font-size: {sm}px; }}
QLabel {{ color: {c['textMuted']}; font-size: {sm}px; }}
QComboBox, QSpinBox, QDoubleSpinBox {{ background: {c['surface']}; color: {c['text']};
            border: 1px solid {c['border']}; border-radius: {radius}px; padding: 4px; }}
"""


def to_textual_vars(theme: dict) -> dict:
    """theme -> Textual CSS 变量 dict（to_textual_css 由它拼出）。"""
    color = theme.get("color", {})
    keys = ("bg", "surface", "surfaceRaised", "surfaceDisabled",
            "border", "text", "textMuted", "textFaint", "accent",
            "success", "warning", "danger", "info",
            "diffAdd", "diffDel", "codeBg")
    return {k: color[k] for k in keys if k in color}


def to_textual_css(theme: dict) -> str:
    """theme -> Textual CSS 变量块（全屏 TUI 用）。

    Textual 的变量用 $ 前缀、写在样式表顶层（不在选择器里），
    例如 `$bg: #1e1e2e;`，组件里用 `background: $surface;` 引用。
    """
    return "\n".join(f"${k}: {v};"
                     for k, v in to_textual_vars(theme).items())


def to_ansi_roles(theme: dict | None = None) -> dict:
    """角色 -> ANSI 转义码（CLI inline fallback 用）。

    终端配色不可控，这里不按 theme.json 的 hex 值换算，只按角色给
    一套固定映射；theme 参数留着以后按亮暗主题微调用。
    """
    return dict(_ANSI_MAP)
