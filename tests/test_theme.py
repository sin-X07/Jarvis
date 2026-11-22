"""主题的离线测试：不用 key、不联网。

验证（U6-1）：
  1. light.json 与 theme.json（dark）token 键完全对齐，不缺不少
  2. 三个生成器（CSS 变量 / QSS / Textual CSS）对 light 都出浅色，
     不漏深色值
  3. 网页版注入了深浅两份变量 + 主题切换按钮与 localStorage 逻辑

Run:  python test_theme.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis.ui.theme import load_theme, to_css_vars, to_qss, to_textual_css

DARK_BG = "#1e1e2e"
LIGHT_BG = "#eff1f5"


def test_light_tokens_align_with_dark():
    dark = load_theme("dark")
    light = load_theme("light")
    assert light["name"] == "light", light.get("name")
    assert set(light["color"]) == set(dark["color"]), (
        set(light["color"]) ^ set(dark["color"]))
    assert set(light["font"]["size"]) == set(dark["font"]["size"])
    assert set(light["radius"]) == set(dark["radius"])
    assert set(light["spacing"]) == set(dark["spacing"])
    assert light["color"]["bg"] == LIGHT_BG
    assert light["color"]["text"] != dark["color"]["text"]
    print("[OK] 主题：light.json 与 dark token 键完全对齐")


def test_generators_emit_light():
    light = load_theme("light")
    css = to_css_vars(light)
    assert LIGHT_BG in css and DARK_BG not in css, css
    qss = to_qss(light)
    assert LIGHT_BG in qss and DARK_BG not in qss, qss
    textual = to_textual_css(light)
    assert LIGHT_BG in textual and DARK_BG not in textual, textual
    print("[OK] 主题：三个生成器对 light 都出浅色，不漏深色值")


def test_server_injects_both_themes():
    from jarvis.ui.server import INDEX_HTML
    assert ":root {" in INDEX_HTML, "深色变量块缺失"
    assert 'html[data-theme="light"] {' in INDEX_HTML, "浅色变量块缺失"
    assert LIGHT_BG in INDEX_HTML
    assert 'id="theme-chip"' in INDEX_HTML
    assert "jarvis-theme" in INDEX_HTML
    print("[OK] 网页版：深浅两份变量 + 主题切换按钮都已注入")


if __name__ == "__main__":
    test_light_tokens_align_with_dark()
    test_generators_emit_light()
    test_server_injects_both_themes()
    print("全部通过。")
