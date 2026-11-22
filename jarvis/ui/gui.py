"""Jarvis GUI (PySide6，U3 重构)：气泡聊天 + 工具卡片 + 右侧 Tab + 内嵌确认。

布局：
    +-------------------------------+------------------+
    | [模型芯片][档位芯片] [设置][轨迹] | Tab: 会话/Diff/检查点 |
    | chat scroll（气泡/卡片）        |                  |
    | [排队提示]                     |                  |
    | [多行输入            ] [发送]   |                  |
    | 状态栏：用量 · context 条 · 费用 |                 |
    +-------------------------------+------------------+

聊天区是一串组件（QScrollArea + QVBoxLayout），不再是一个 QTextEdit：
- MessageBubble：气泡（QTextBrowser 渲染 HTML/Markdown）+ 悬停工具栏
  （复制 / 重新生成）；流式逐 token 追加进气泡，收尾时只有含 Markdown
  的回答才原地换渲染版，纯文本不换、不闪烁。
- ToolCardWidget：标题行（摘要 + 状态 + 耗时）+ 可折叠正文（参数/输出）。
- ThinkingWidget：思考 disclosure，默认折叠。
- ConfirmCardWidget：确认长在聊天流里（允许一次 / 总是允许 / 拒绝 / 编辑），
  不再弹 QMessageBox。
- ErrorCardWidget：错误 + 重试按钮。

Threading：同 P7——worker 在 QThread 里跑 run_agent，只发信号；
主线程的槽更新界面。Stop 不杀线程，在下一个事件处抛 _StopRequested
掐断（worker.py）。

颜色全部来自 theme.json：to_qss 出基础样式，本文件再按 token 拼一
份组件样式（芯片/卡片/代码块头），不写死十六进制颜色。

Run:  python -m jarvis.ui.gui
"""

import glob
import html
import os
import re
import sys
import time
from datetime import datetime, timezone

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPushButton, QScrollArea, QSpinBox,
    QSplitter, QTabWidget, QTextBrowser, QTextEdit, QVBoxLayout, QWidget,
)

from jarvis.core.agent import MODEL, SYSTEM_PROMPT, build_client
from jarvis.core.config import load_config, save_config
from jarvis.memory.checkpoint import (
    CheckpointStore, set_current_store, snapshot_if_active)
from jarvis.safety.diffutil import parse_diff
from jarvis.ui.events import friendly_error
from jarvis.ui.md import to_html
from jarvis.memory.memory import ConversationMemory, FactStore, memory_tools
from jarvis.memory.planner import parse_plan
from jarvis.memory.todo import TodoList, attach_todo_tools, todos_path
from jarvis.safety.permissions import ApprovalGate
from jarvis.memory.session_store import SessionStore
from jarvis.tools.tools import register_before_write_hook, working_tree_diff
from jarvis.ui.theme import (load_theme, load_ui_prefs, save_ui_prefs,
                             to_qss)
from jarvis.core.usage import UsageTracker
from jarvis.ui.worker import AgentWorker

# QSS 由 theme.json 生成（U0-2）：视觉与旧手写 DARK_STYLE 一致，
# 颜色只在 theme.json 里改一处。
DARK_STYLE = to_qss(load_theme("dark"))


def _theme_colors() -> dict:
    """当前主题的 color token（芯片/卡片等组件样式用它拼，不写死颜色）。"""
    prefs = load_ui_prefs()
    return load_theme(prefs.get("theme", "dark")).get("color", {})


def _component_style() -> str:
    """芯片/气泡/卡片/代码块头的 QSS，全部用 token 拼。"""
    c = _theme_colors()
    return f"""
QLabel#chip {{ background: {c.get('surfaceRaised', '#313244')};
  color: {c.get('accent', '#89b4fa')}; border: 1px solid {c.get('border', '#313244')};
  border-radius: 10px; padding: 2px 10px; font-size: 13px; }}
QPushButton#chipBtn {{ background: {c.get('surfaceRaised', '#313244')};
  color: {c.get('accent', '#89b4fa')}; border: 1px solid {c.get('border', '#313244')};
  border-radius: 10px; padding: 2px 10px; font-size: 13px; }}
QPushButton#toolBtn {{ background: transparent; color: {c.get('textMuted', '#a6adc8')};
  border: 1px solid {c.get('border', '#313244')}; border-radius: 6px;
  padding: 1px 8px; font-size: 12px; }}
QPushButton#toolBtn:hover {{ color: {c.get('text', '#cdd6f4')};
  border-color: {c.get('accent', '#89b4fa')}; }}
QWidget#bubbleUser {{ background: {c.get('surfaceRaised', '#313244')};
  border-radius: 8px; }}
QWidget#bubbleJarvis {{ background: {c.get('surface', '#181825')};
  border: 1px solid {c.get('border', '#313244')}; border-radius: 8px; }}
QWidget#card {{ background: {c.get('surface', '#181825')};
  border: 1px solid {c.get('border', '#313244')}; border-radius: 8px; }}
QWidget#cardDanger {{ background: {c.get('surface', '#181825')};
  border: 1px solid {c.get('danger', '#f38ba8')}; border-radius: 8px; }}
QWidget#cardWarn {{ background: {c.get('surface', '#181825')};
  border: 1px solid {c.get('warning', '#f9e2af')}; border-radius: 8px; }}
QTextBrowser#body {{ background: transparent; border: none; }}
QTextBrowser#code {{ background: {c.get('codeBg', '#11111b')}; border: none; }}
"""


def _context_bar(pct: float | None, width: int = 10) -> str:
    """context 占比画成 [██████░░░░] 60% 这种文本条；没数时显示 —。
    与 TUI 的同名函数同一算法（TUI 在 textual 里，GUI 不能 import 它）。"""
    if pct is None:
        return "[----------] —"
    filled = max(0, min(width, round(pct * width)))
    return f"[{'█' * filled}{'░' * (width - filled)}] {pct * 100:.0f}%"


def _looks_like_markdown(text: str) -> bool:
    """回答里有没有 Markdown 构造：有才在收尾时换渲染版，没有就留着
    流式插进去的纯文本，不闪烁。"""
    if "```" in text:
        return True
    for line in text.split("\n"):
        s = line.strip()
        if (s.startswith(("# ", "## ", "> ", "- ", "* "))
                or "**" in s or re.search(r"`[^`]+`", s)
                or re.search(r"\[[^\]]+\]\([^)]+\)", s)):
            return True
    return False


def _html_with_code_headers(html_text: str):
    """给 to_html 出的代码块加语言标签 + 复制链接。

    返回 (html, code_texts)：code_texts[i] 是第 i 块代码的原文，
    气泡收到 copy-code:i 的锚点点击时把它放进剪贴板。
    """
    codes = []
    colors = _theme_colors()
    muted = colors.get("textMuted", "#a6adc8")
    code_bg = colors.get("codeBg", "#11111b")

    def _wrap(match):
        lang = match.group(1) or ""
        escaped = match.group(2)
        codes.append(html.unescape(escaped))
        idx = len(codes) - 1
        label = lang or "code"
        return (f'<div><span style="color:{muted}">{html.escape(label)}'
                f'</span> &nbsp;<a href="copy-code:{idx}">复制</a></div>'
                f'<pre style="background:{code_bg}"><code>{escaped}</code></pre>')

    wrapped = re.sub(r'<pre><code(?: class="language-([^"]*)")?>(.*?)'
                     r'</code></pre>', _wrap, html_text, flags=re.S)
    return wrapped, codes


class ComposerEdit(QTextEdit):
    """多行输入框：Enter 发送 / Shift+Enter 换行；1–6 行自增高。"""

    send_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setPlaceholderText("和 Jarvis 说点什么…（回车发送，Shift+回车换行）")
        self.setFixedHeight(34)
        self.textChanged.connect(self._auto_grow)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not (
                event.modifiers() & Qt.ShiftModifier):
            self.send_requested.emit()
            return
        super().keyPressEvent(event)

    def _auto_grow(self):
        doc_height = self.document().size().height()
        lines = max(1, min(6, round(doc_height / 20)))
        self.setFixedHeight(14 + lines * 20)


class MessageBubble(QWidget):
    """一条消息气泡：正文（QTextBrowser）+ 悬停工具栏。

    role="user" 时右对齐、只有复制；role="jarvis" 时有复制和重新生成。
    悬停还有“恢复检查点”（U5-2，on_restore 给了才出现）：文件回到
    这条消息挂载之前，对话内容不动。时间戳放 tooltip 里（悬停显示，
    U3-6）；挂载时刻记在 mounted_at（UTC ISO，给 rewind_to 当 cutoff）。
    """

    def __init__(self, role: str, on_copy, on_regenerate=None,
                 on_restore=None, parent=None):
        super().__init__(parent)
        self.role = role
        self._on_copy = on_copy
        self._on_regenerate = on_regenerate
        self._on_restore = on_restore
        self.mounted_at = datetime.now(timezone.utc).isoformat()
        self._code_texts: list = []
        self._plain = ""
        self.setObjectName("bubbleUser" if role == "user" else "bubbleJarvis")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(2)
        # 悬停工具栏：平时隐藏，鼠标进来才露出来
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.addStretch()
        self.copy_btn = QPushButton("复制")
        self.copy_btn.setObjectName("toolBtn")
        self.copy_btn.clicked.connect(self._copy_clicked)
        bar.addWidget(self.copy_btn)
        self.regen_btn = None
        if role == "jarvis" and on_regenerate is not None:
            self.regen_btn = QPushButton("重新生成")
            self.regen_btn.setObjectName("toolBtn")
            self.regen_btn.clicked.connect(
                lambda: self._on_regenerate(self))
            bar.addWidget(self.regen_btn)
        self.restore_btn = None
        if on_restore is not None:
            self.restore_btn = QPushButton("恢复检查点")
            self.restore_btn.setObjectName("toolBtn")
            self.restore_btn.setToolTip(
                "文件回到这条消息出现之前，对话内容不动")
            self.restore_btn.clicked.connect(
                lambda: self._on_restore(self))
            bar.addWidget(self.restore_btn)
        self._bar_widget = QWidget()
        self._bar_widget.setLayout(bar)
        self._bar_widget.setVisible(False)
        layout.addWidget(self._bar_widget)
        self.body = QTextBrowser()
        self.body.setObjectName("body")
        self.body.setReadOnly(True)
        self.body.setOpenLinks(False)
        self.body.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.body.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.body.anchorClicked.connect(self._on_anchor)
        layout.addWidget(self.body)
        self.setToolTip(time.strftime("%Y-%m-%d %H:%M:%S"))

    # -- 悬停工具栏 ------------------------------------------------------
    def enterEvent(self, event):
        self._bar_widget.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._bar_widget.setVisible(False)
        super().leaveEvent(event)

    def resizeEvent(self, event):
        self._fit()
        super().resizeEvent(event)

    # -- 内容 ------------------------------------------------------------
    def set_plain(self, text: str):
        """整段纯文本（恢复会话 / 用户消息用）。"""
        self._plain = text
        align = ' align="right"' if self.role == "user" else ""
        self.body.setHtml(f"<p{align}>{html.escape(text)}</p>")
        self._fit()

    def append_text(self, text: str):
        """流式：逐 token 追加（纯文本，不重渲染整段）。"""
        self._plain += text
        cursor = self.body.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(text)
        self._fit()

    def set_markdown(self, text: str):
        """收尾：有 Markdown 构造时才换渲染版（纯文本不换，不闪烁）。"""
        self._plain = text
        wrapped, self._code_texts = _html_with_code_headers(to_html(text))
        self.body.setHtml(wrapped)
        self._fit()

    def plain_text(self) -> str:
        return self._plain or self.body.toPlainText()

    def _fit(self):
        """按文档高度定 body 的高（气泡自己长个子，不出内滚动条）。"""
        doc = self.body.document()
        width = self.body.viewport().width()
        if width > 0:
            # setHtml 后文档布局是懒的，立刻量高度会拿到旧值（曾坍到 1 行）；
            # 先把宽度同步进去，强制当场排版再量。
            doc.setTextWidth(width)
        doc_height = doc.size().height()
        self.body.setFixedHeight(max(24, int(doc_height) + 4))

    def _copy_clicked(self):
        self._on_copy(self.plain_text())

    def _on_anchor(self, url):
        """代码块头的“复制”链接：copy-code:N → 第 N 块代码进剪贴板。"""
        text = url.toString()
        if text.startswith("copy-code:"):
            try:
                idx = int(text.split(":", 1)[1])
                self._on_copy(self._code_texts[idx])
            except (ValueError, IndexError):
                pass


class ToolCardWidget(QWidget):
    """一张工具卡片：标题行是摘要 + 状态 + 耗时，点开看参数和输出。

    折叠默认收起；标题按钮切换。风险分级画边框：danger 红、write 琥珀。
    """

    def __init__(self, name: str, args: dict, risk: str, parent=None):
        super().__init__(parent)
        self.tool_name = name
        self._args_text = ""
        try:
            import json
            self._args_text = json.dumps(args, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            self._args_text = str(args)
        self.setObjectName("cardDanger" if risk == "danger"
                           else "cardWarn" if risk == "write" else "card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(4)
        self.header = QPushButton("▸ …")
        self.header.setFlat(True)
        self.header.setStyleSheet("text-align: left; padding: 2px;")
        self.header.clicked.connect(self._toggle)
        layout.addWidget(self.header)
        self.body = QTextBrowser()
        self.body.setObjectName("body")
        self.body.setReadOnly(True)
        self.body.setOpenLinks(False)
        self.body.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.body.setVisible(False)
        layout.addWidget(self.body)
        self._collapsed = True
        self._summary = ""
        self._header_text = "…"

    def set_started(self, summary: str):
        self._summary = summary
        self._header_text = f"{summary} · 进行中…"
        self._refresh_header()

    def set_finished(self, status: str, duration_ms: int, result: str):
        mark = "✓" if status == "ok" else "✗"
        self._header_text = f"{mark} {self._summary} · {duration_ms}ms"
        self._refresh_header()
        self.body.setPlainText(
            f"参数：\n{self._args_text}\n\n返回：\n{result}")
        doc_height = self.body.document().size().height()
        self.body.setFixedHeight(max(24, int(doc_height) + 4))

    def _refresh_header(self):
        arrow = "▸ " if self._collapsed else "▾ "
        self.header.setText(arrow + self._header_text)

    def _toggle(self):
        self._collapsed = not self._collapsed
        self.body.setVisible(not self._collapsed)
        self._refresh_header()


class ThinkingWidget(QWidget):
    """思考 disclosure：标题“💭 思考过程”，默认折叠，点开看推理原文。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(4)
        self.header = QPushButton("▸ 💭 思考过程")
        self.header.setFlat(True)
        self.header.setStyleSheet("text-align: left; padding: 2px;")
        self.header.clicked.connect(self._toggle)
        layout.addWidget(self.header)
        self.body = QTextBrowser()
        self.body.setObjectName("body")
        self.body.setReadOnly(True)
        self.body.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.body.setVisible(False)
        layout.addWidget(self.body)
        self._collapsed = True
        self._text = ""

    def append_text(self, text: str):
        self._text += text
        cursor = self.body.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(text)
        doc_height = self.body.document().size().height()
        self.body.setFixedHeight(max(24, int(doc_height) + 4))

    def _toggle(self):
        self._collapsed = not self._collapsed
        self.body.setVisible(not self._collapsed)
        self.header.setText(("▾ 💭 思考过程" if not self._collapsed
                             else "▸ 💭 思考过程"))


class ConfirmCardWidget(QWidget):
    """确认卡片：长在聊天流里，不弹窗。

    四个按钮：允许一次 / 总是允许（本会话记住这一种操作）/ 拒绝 /
    编辑（拒绝并把命令填回输入框）。点完回调 on_answer(kind)。
    """

    def __init__(self, action: dict, on_answer, parent=None):
        super().__init__(parent)
        self.setObjectName("cardWarn")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        kind = action.get("kind", "")
        target = action.get("target", "")
        detail = action.get("detail", "")
        text = f"Jarvis 想执行：{kind} {target}"
        if detail:
            text += f"\n{detail[:500]}"
        label = QLabel(text)
        label.setWordWrap(True)
        layout.addWidget(label)
        row = QHBoxLayout()
        for caption, value in (("允许一次", "once"), ("总是允许", "always"),
                               ("拒绝", "deny"), ("编辑", "edit")):
            btn = QPushButton(caption)
            btn.clicked.connect(
                lambda _checked=False, v=value: on_answer(v))
            row.addWidget(btn)
        row.addStretch()
        layout.addLayout(row)


class PlanCardWidget(QWidget):
    """计划卡片（U4-1）：目标 + 步骤 + 风险；计划文本可改；批准/拒绝。

    批准时把编辑框里的文本（标准计划格式）交给 on_approve，
    拒绝调 on_reject。步骤行是只读预览，改内容走下面的文本框。
    """

    def __init__(self, plan: dict, on_approve, on_reject, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        goal = plan.get("goal", "")
        head = QLabel(f"📋 计划：{goal}" if goal else "📋 计划")
        head.setWordWrap(True)
        layout.addWidget(head)
        steps = plan.get("steps") or []
        if steps:
            lines = []
            for i, step in enumerate(steps, 1):
                acc = step.get("acceptance", "")
                line = f"☐ {i}. {step.get('title', '')}"
                if acc:
                    line += f"（验收：{acc}）"
                lines.append(line)
            steps_label = QLabel("\n".join(lines))
            steps_label.setWordWrap(True)
            layout.addWidget(steps_label)
        risks = plan.get("risks") or []
        if risks:
            risk_label = QLabel("风险：\n" + "\n".join(f"- {r}" for r in risks))
            risk_label.setWordWrap(True)
            layout.addWidget(risk_label)
        self.edit = QTextEdit()
        self.edit.setPlainText(plan.get("markdown", ""))
        self.edit.setMaximumHeight(180)
        layout.addWidget(self.edit)
        row = QHBoxLayout()
        ok_btn = QPushButton("批准执行")
        ok_btn.clicked.connect(
            lambda _checked=False: on_approve(self.edit.toPlainText()))
        row.addWidget(ok_btn)
        no_btn = QPushButton("拒绝")
        no_btn.clicked.connect(lambda _checked=False: on_reject())
        row.addWidget(no_btn)
        row.addStretch()
        layout.addLayout(row)


class ErrorCardWidget(QWidget):
    """错误卡片：就地显示原因 + 恢复按钮。

    on_retry 给了就显示“重试”；extra_button=(文案, 回调) 给了就
    再加一个按钮（比如“复制问题，开新会话”）。U6-4：重试只给一次，
    第二次出错就换成复制问题开新会话。
    """

    def __init__(self, message: str, on_retry=None, extra_button=None,
                 parent=None):
        super().__init__(parent)
        self.setObjectName("cardDanger")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        label = QLabel(f"✗ 出错了：{message}")
        label.setWordWrap(True)
        layout.addWidget(label)
        row = QHBoxLayout()
        if on_retry is not None:
            retry = QPushButton("重试")
            retry.clicked.connect(on_retry)
            row.addWidget(retry)
        if extra_button is not None:
            text, callback = extra_button
            extra = QPushButton(text)
            extra.clicked.connect(callback)
            row.addWidget(extra)
        row.addStretch()
        layout.addLayout(row)


class NoteWidget(QWidget):
    """小字系统提示（欢迎语、检查点回滚提示），居中灰字。"""

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 4)
        label = QLabel(text)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)


class ShortcutsDialog(QDialog):
    """快捷键总览（U6-2）：和 CLI 的 ? / 网页版的 ⌨ 快捷键同一张表，
    只列这个界面上真的能用的。"""

    TEXT = (
        "发送：Enter 发送 / Shift+Enter 换行；输入框自动长高（1–6 行）\n"
        "忙时：发送按钮变“停止”，点一下掐断本轮；回车把追问排进队列\n"
        "消息：悬停气泡可复制 / 重新生成；代码块右上角复制\n"
        "工具卡片：点标题展开 / 折叠，看参数与输出预览\n"
        "确认：危险操作出内嵌卡片——允许一次 / 总是允许 / 拒绝 / 编辑\n"
        "计划：顶栏“📋 计划”开着发消息，先出计划卡片，批准后才执行\n"
        "待办：右侧“待办”页看清单（☐ 待做 / ◐ 进行中 / ☑ 已完成）\n"
        "Diff：右侧“Diff”页按文件分组看改动，接受全部 / 拒绝全部\n"
        "检查点：右侧“检查点”页点一行恢复文件；悬停消息可“恢复检查点”\n"
        "主题：设置里切深色 / 浅色")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("快捷键")
        self.setMinimumWidth(560)  # 太窄会把每行断成碎句
        layout = QVBoxLayout(self)
        label = QLabel(self.TEXT)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(label)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        layout.addWidget(close)


class SettingsDialog(QDialog):
    """设置对话框：key、模型、权限档位、最大步数、主题。

    主题从 jarvis/ui/*.json 里读（dark + light）。保存后：key 写进
    项目 .env（只存在本机），其余写回 config.json + ui.json。
    first_run=True 是首次启动向导：标题与说明换成欢迎语。
    """

    def __init__(self, config, parent=None, first_run=False):
        super().__init__(parent)
        self.setWindowTitle("欢迎使用 Jarvis" if first_run else "设置")
        self._config = config
        self.key_saved = None  # 本次新填的 key（没填就是 None）
        layout = QFormLayout(self)
        if first_run:
            intro = QLabel(
                "第一次启动：填入 DeepSeek API key 就能开始。\n"
                "key 只写进本机项目 .env，不上传、不记日志。")
            intro.setWordWrap(True)
            layout.addRow(intro)
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_edit.setPlaceholderText("sk-…（留空表示不改）")
        layout.addRow("DeepSeek key：", self.key_edit)
        self.model_edit = QLineEdit(config.model)
        layout.addRow("模型：", self.model_edit)
        self.permission_box = QComboBox()
        self.permission_box.addItems(["readonly", "edit", "auto"])
        self.permission_box.setCurrentText(config.permission)
        layout.addRow("权限档位：", self.permission_box)
        self.steps_spin = QSpinBox()
        self.steps_spin.setRange(1, 50)
        self.steps_spin.setValue(config.max_steps)
        layout.addRow("最大步数：", self.steps_spin)
        self.theme_box = QComboBox()
        self._themes = self._available_themes()
        self.theme_box.addItems(list(self._themes))
        current = load_ui_prefs().get("theme", "dark")
        self.theme_box.setCurrentText(current if current in self._themes
                                      else "dark")
        self.theme_box.setEnabled(len(self._themes) > 1)
        layout.addRow("主题：", self.theme_box)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _available_themes() -> list:
        """jarvis/ui/*.json → 主题名（theme.json 算 dark）。"""
        names = []
        here = os.path.dirname(os.path.abspath(__file__))
        for path in sorted(glob.glob(os.path.join(here, "*.json"))):
            base = os.path.basename(path)
            name = "dark" if base == "theme.json" else base[:-5]
            if name not in names:
                names.append(name)
        return names or ["dark"]

    def apply(self) -> None:
        from jarvis import PROJECT_ROOT
        from jarvis.core.config import save_key_to_dotenv

        key = self.key_edit.text().strip()
        if key:
            save_key_to_dotenv(key, os.path.join(PROJECT_ROOT, ".env"))
            os.environ["DEEPSEEK_API_KEY"] = key
            self.key_saved = key
        self._config.model = self.model_edit.text().strip() or MODEL
        self._config.permission = self.permission_box.currentText()
        self._config.max_steps = self.steps_spin.value()
        save_config(self._config)
        prefs = load_ui_prefs()
        prefs["theme"] = self.theme_box.currentText()
        save_ui_prefs(prefs)
        self.theme_name = self.theme_box.currentText()


class MainWindow(QMainWindow):
    todo_changed = Signal()  # U4-2：待办清单变了（工作线程里也可能发）

    def __init__(self, client, session_store=None, store=None):
        super().__init__()
        self._client = client
        self._config = load_config()
        self._store = store if store is not None else FactStore()
        self._memory = ConversationMemory(self._system_prompt())
        self._schemas, self._funcs = memory_tools(self._store)
        self._sessions = session_store if session_store is not None \
            else SessionStore()
        self._session_id = None
        self._checkpoints = None
        self._gate = ApprovalGate(self._config.permission)
        self._usage = UsageTracker()
        self._todo = None
        self._set_todo(TodoList())
        self.todo_changed.connect(self._refresh_todos)
        self._plan_task = None
        self._pending_plan = None
        self._thread = None
        self._worker = None
        self._busy = False
        self._bubble = None          # 本轮正在流式写的气泡（工具一来就断开）
        self._turn_bubbles = []      # 本轮产生的所有 Jarvis 气泡（收尾时渲染）
        self._thinking = None
        self._tool_card = None
        self._last_user_text = ""
        self._error_streak = 0  # 连续出错次数（U6-4：重试只给一次）
        self._queue = []             # 忙时排队的追问（FIFO）
        self._always_allowed = set()  # 本会话记住的确认：(kind, target)
        self._pending_confirm_action = None
        self._confirm_card = None
        self._following = True       # transcript 是否跟随到底部
        self._folded = 0
        self._build_ui()
        self._refresh_memory_list()
        self._refresh_session_list()
        self._restore_geometry()
        self.setWindowTitle("Jarvis")
        self._add_transcript_widget(NoteWidget(
            "Jarvis 已启动。能记住事情、读写文件、跑命令。"
            "直接说话，或者先记住点什么："
            "“记住，我最喜欢蓝色”。"))
        if client is None:
            # 没 key：窗口照常打开，首次启动向导随后自动弹（U6-3）。
            # 用挂在窗口下的 QTimer 而不用 QTimer.singleShot：singleShot
            # 的回调是窗口的 bound method，会拖住窗口不释放；200ms 内
            # 关窗退出时，挂起的回调在解释器收尾的 GC 里撞上已析构的
            # QApplication，Windows 上段错误（0xC0000005，2026-11-22
            # 真机 test_gui.py）。closeEvent 里停掉并断开它。
            self._wizard_timer = QTimer(self)
            self._wizard_timer.setSingleShot(True)
            self._wizard_timer.timeout.connect(self._open_first_run_wizard)
            self._wizard_timer.start(200)

    # -- UI construction ------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)

        # 菜单栏：帮助（快捷键总览 / 关于）
        help_menu = self.menuBar().addMenu("帮助")
        shortcuts_action = help_menu.addAction("快捷键…")
        shortcuts_action.triggered.connect(self.show_shortcuts)
        about_action = help_menu.addAction("关于 Jarvis")
        about_action.triggered.connect(self.show_about)

        # 顶栏：模型芯片 + 档位芯片（点一下换档）+ 设置 / 轨迹
        topbar = QWidget()
        top = QHBoxLayout(topbar)
        top.setContentsMargins(0, 0, 0, 0)
        self.model_label = QLabel(f"模型：{self._config.model}")
        self.model_label.setObjectName("chip")
        top.addWidget(self.model_label)
        self.permission_label = QPushButton(
            f"权限：{self._config.permission}")
        self.permission_label.setObjectName("chipBtn")
        self.permission_label.setToolTip("点一下换档位：readonly → edit → auto")
        self.permission_label.clicked.connect(self._cycle_permission)
        top.addWidget(self.permission_label)
        self.plan_button = QPushButton("📋 计划")
        self.plan_button.setObjectName("chipBtn")
        self.plan_button.setCheckable(True)
        self.plan_button.setToolTip(
            "计划模式：开着发消息，先出计划卡片，批准后才执行")
        top.addWidget(self.plan_button)
        top.addStretch()
        self.trajectory_button = QPushButton("轨迹")
        self.trajectory_button.clicked.connect(self.show_trajectory)
        top.addWidget(self.trajectory_button)
        self.settings_button = QPushButton("设置")
        self.settings_button.clicked.connect(self.open_settings)
        top.addWidget(self.settings_button)
        root.addWidget(topbar)

        splitter = QSplitter(Qt.Horizontal)
        self._splitter = splitter
        root.addWidget(splitter, 1)

        # 左：聊天滚动区
        self.chat_scroll = QScrollArea()
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setFrameShape(QScrollArea.NoFrame)
        container = QWidget()
        self.chat_layout = QVBoxLayout(container)
        self.chat_layout.setContentsMargins(4, 4, 4, 4)
        self.chat_layout.setSpacing(8)
        self.chat_layout.addStretch()
        self.chat_scroll.setWidget(container)
        self.chat_scroll.verticalScrollBar().valueChanged.connect(
            self._on_scroll)
        splitter.addWidget(self.chat_scroll)

        # 右：Tab 页（会话 / Diff / 检查点）
        self.tabs = QTabWidget()
        self.tabs.currentChanged.connect(self._on_tab_changed)
        splitter.addWidget(self.tabs)

        sessions_tab = QWidget()
        s_layout = QVBoxLayout(sessions_tab)
        s_layout.addWidget(QLabel("会话"))
        self.session_list = QListWidget()
        self.session_list.itemClicked.connect(self._on_session_clicked)
        s_layout.addWidget(self.session_list)
        self.new_session_button = QPushButton("新会话")
        self.new_session_button.clicked.connect(self.new_session)
        s_layout.addWidget(self.new_session_button)
        s_layout.addWidget(QLabel("长期记忆"))
        self.memory_list = QListWidget()
        s_layout.addWidget(self.memory_list)
        self.clear_button = QPushButton("清空对话")
        self.clear_button.clicked.connect(self.clear_chat)
        s_layout.addWidget(self.clear_button)
        s_layout.addStretch()
        self.tabs.addTab(sessions_tab, "会话")

        todo_tab = QWidget()
        t_layout = QVBoxLayout(todo_tab)
        t_layout.addWidget(QLabel("待办（☐ 待做 / ◐ 进行中 / ☑ 已完成）"))
        self.todo_list = QListWidget()
        t_layout.addWidget(self.todo_list)
        self.tabs.addTab(todo_tab, "待办")

        diff_tab = QWidget()
        d_layout = QVBoxLayout(diff_tab)
        self.diff_view = QTextBrowser()
        self.diff_view.setReadOnly(True)
        self.diff_view.setOpenLinks(False)
        d_layout.addWidget(self.diff_view)
        d_btns = QHBoxLayout()
        self.diff_refresh_button = QPushButton("刷新")
        self.diff_refresh_button.clicked.connect(self._refresh_diff)
        d_btns.addWidget(self.diff_refresh_button)
        self.diff_accept_button = QPushButton("接受全部")
        self.diff_accept_button.clicked.connect(self._accept_diff)
        d_btns.addWidget(self.diff_accept_button)
        self.diff_reject_button = QPushButton("拒绝全部")
        self.diff_reject_button.clicked.connect(self._reject_diff)
        d_btns.addWidget(self.diff_reject_button)
        d_btns.addStretch()
        d_layout.addLayout(d_btns)
        self.tabs.addTab(diff_tab, "Diff")

        ckpt_tab = QWidget()
        c_layout = QVBoxLayout(ckpt_tab)
        c_layout.addWidget(QLabel("检查点（点一条恢复到那一轮，只回滚文件）"))
        self.ckpt_list = QListWidget()
        self.ckpt_list.itemClicked.connect(self._on_ckpt_clicked)
        c_layout.addWidget(self.ckpt_list)
        self.tabs.addTab(ckpt_tab, "检查点")

        splitter.setSizes([750, 350])

        # 排队提示（忙时才出现）
        self.queue_label = QLabel()
        self.queue_label.setVisible(False)
        root.addWidget(self.queue_label)

        # 输入行：多行自增高 + 发送/停止
        input_row = QWidget()
        row = QHBoxLayout(input_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.input_box = ComposerEdit()
        self.input_box.send_requested.connect(self.send)
        row.addWidget(self.input_box, 1)
        self.send_button = QPushButton("发送")
        self.send_button.clicked.connect(self._on_send_button)
        row.addWidget(self.send_button)
        root.addWidget(input_row)

        # 底部状态栏：状态文本 + context 条 + 用量
        self.status_label = QLabel("就绪")
        self.statusBar().addWidget(self.status_label, 1)
        self.context_label = QLabel()
        self.statusBar().addPermanentWidget(self.context_label)
        self.usage_label = QLabel("用量：本轮 0 token")
        self.statusBar().addPermanentWidget(self.usage_label)
        self._refresh_statusbar()

    def _restore_geometry(self):
        cfg = self._config
        self.resize(cfg.window_width or 1100, cfg.window_height or 650)
        if cfg.window_x is not None and cfg.window_y is not None:
            self.move(cfg.window_x, cfg.window_y)
        if cfg.splitter_sizes:
            try:
                self._splitter.setSizes([int(x) for x in cfg.splitter_sizes])
            except (TypeError, ValueError):
                pass

    # -- transcript -----------------------------------------------------

    def _add_transcript_widget(self, widget):
        """往聊天流末尾（stretch 前面）挂一个组件，超 200 个就折叠老的。"""
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, widget)
        self._trim_transcript()
        if self._following:
            bar = self.chat_scroll.verticalScrollBar()
            bar.setValue(bar.maximum())

    def _trim_transcript(self):
        """长 transcript 折叠：超 200 个组件时把最老的 50 个换成一条提示。
        消息还在会话 JSONL 里，轨迹能看（和 TUI 的 400 上限同一做法）。"""
        count = self.chat_layout.count() - 1  # 减掉末尾的 stretch
        if count <= 200:
            return
        removed = 0
        while self.chat_layout.count() - 1 > 150:
            item = self.chat_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
                removed += 1
        if removed:
            self._folded += removed
            note = NoteWidget(
                f"↑ 更早的 {self._folded} 条消息已折叠（轨迹里能看完整记录）")
            self.chat_layout.insertWidget(0, note)

    def transcript_text(self) -> str:
        """聊天流里的全部文字（测试用；气泡取纯文本，提示取 label）。"""
        parts = []
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            widget = item.widget() if item is not None else None
            if isinstance(widget, MessageBubble):
                parts.append(widget.plain_text())
            elif isinstance(widget, NoteWidget):
                label = widget.findChild(QLabel)
                if label is not None:
                    parts.append(label.text())
            elif isinstance(widget, ToolCardWidget):
                parts.append(widget.header.text())
        return "\n".join(parts)

    def _clear_transcript(self):
        while self.chat_layout.count() > 1:
            item = self.chat_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._folded = 0
        self._bubble = None
        self._turn_bubbles = []
        self._thinking = None
        self._tool_card = None

    def _on_scroll(self, value: int):
        bar = self.chat_scroll.verticalScrollBar()
        self._following = value >= bar.maximum() - 20

    def _note(self, text: str):
        self._add_transcript_widget(NoteWidget(text))

    # -- memory / sessions -----------------------------------------------

    def _system_prompt(self) -> str:
        facts = self._store.render_for_prompt()
        return f"{SYSTEM_PROMPT}\n{facts}" if facts else SYSTEM_PROMPT

    def _refresh_memory_list(self):
        self.memory_list.clear()
        self.memory_list.addItems(self._store.all_texts())

    def _refresh_session_list(self):
        self.session_list.clear()
        for s in self._sessions.list_sessions():
            item = QListWidgetItem(
                f"{s.get('title') or '(无标题)'}  ({s.get('turns', 0)} 轮)")
            item.setData(Qt.UserRole, s["id"])
            self.session_list.addItem(item)

    def _on_session_clicked(self, item):
        session_id = item.data(Qt.UserRole)
        if self._busy or not session_id:
            return
        self.resume_session(session_id)

    def resume_session(self, session_id: str):
        messages = self._sessions.load_messages(session_id)
        if not messages and not self._sessions.load_events(session_id):
            return
        self._session_id = session_id
        self._memory.messages = (
            [{"role": "system", "content": self._system_prompt()}]
            + [m for m in messages if m.get("role") != "system"])
        self._memory.trim()
        self._checkpoints = CheckpointStore(session_id)
        set_current_store(self._checkpoints)
        register_before_write_hook(snapshot_if_active)
        self._set_todo(TodoList.load(
            todos_path(self._sessions.root, session_id)))
        self._refresh_todos()
        self._clear_transcript()
        for msg in messages:
            if msg.get("role") == "user":
                bubble = MessageBubble(
                    "user", self._copy_text,
                    on_restore=self._on_bubble_restore)
                bubble.set_plain(str(msg.get("content", "")))
                self._add_transcript_widget(bubble)
            elif msg.get("role") == "assistant" and msg.get("content"):
                bubble = MessageBubble(
                    "jarvis", self._copy_text, self._on_regenerate,
                    on_restore=self._on_bubble_restore)
                content = str(msg["content"])
                if _looks_like_markdown(content):
                    bubble.set_markdown(content)
                else:
                    bubble.set_plain(content)
                self._add_transcript_widget(bubble)
        self.status_label.setText(f"已恢复会话 {session_id[:12]}")
        self._refresh_ckpts()

    def new_session(self):
        if self._busy:
            return
        self._error_streak = 0
        self._session_id = None
        self._checkpoints = None
        set_current_store(None)
        self._memory.clear()
        self._clear_transcript()
        self._note("新会话。")
        self.status_label.setText("就绪")

    def _cycle_permission(self):
        """档位芯片点一下换档：readonly → edit → auto。"""
        order = ["readonly", "edit", "auto"]
        nxt = order[(order.index(self._config.permission) + 1) % len(order)]
        self._config.permission = nxt
        self._gate.mode = nxt
        self.permission_label.setText(f"权限：{nxt}")
        save_config(self._config)

    def show_shortcuts(self):
        """帮助菜单 → 快捷键总览（U6-2）。"""
        dialog = ShortcutsDialog(self)
        dialog.exec()

    def show_about(self):
        """帮助菜单 → 关于：版本与四个入口。"""
        from jarvis.core.config import __version__
        QMessageBox.about(
            self, "关于 Jarvis",
            f"Jarvis {__version__}：多轮对话 + 长期记忆 + 工具调用。\n\n"
            "入口：jarvis（命令行）/ python -m jarvis.ui.gui（桌面）/\n"
            "python -m jarvis.ui.tui（全屏终端）/\n"
            "uvicorn jarvis.ui.server:app（网页）。")

    def open_settings(self):
        dialog = SettingsDialog(self._config, self)
        if dialog.exec():
            dialog.apply()
            self._after_settings(dialog)

    def _open_first_run_wizard(self):
        """首次启动向导（U6-3）：没 key 时自动弹设置，填好就能用。

        取消也不拦：窗口照常能用，发消息时会再给“去填 key”卡片。"""
        if self._client is not None or not self.isVisible():
            # 窗口已关还收到回调（比如定时器在关窗前一刻已触发）：
            # 不弹模态框，直接返回。
            return
        dialog = SettingsDialog(self._config, self, first_run=True)
        if dialog.exec():
            dialog.apply()
            self._after_settings(dialog)
        else:
            self._note("还没填 key：发消息时会再提醒，填好 key 才能对话。")

    def _after_settings(self, dialog):
        """设置保存后：key 变了就重建 client，模型/档位/主题刷新。"""
        if getattr(dialog, "key_saved", None):
            try:
                self._client = build_client()
                self._note("key 已保存，可以开始了。")
            except SystemExit as exc:
                self._note(f"key 已保存，但建 client 失败：{exc}")
        self._gate.mode = self._config.permission
        self.model_label.setText(f"模型：{self._config.model}")
        self.permission_label.setText(f"权限：{self._config.permission}")
        self._apply_theme(dialog.theme_name)
        self.status_label.setText("设置已保存")

    def _apply_theme(self, name: str):
        """换主题：重算整份 QSS（基础 + 组件）并铺到整个应用上，立即生效。"""
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(to_qss(load_theme(name)) + _component_style())

    def show_trajectory(self):
        """轨迹对话框：当前会话的时间线（谁说了什么、调了什么工具）。"""
        if not self._session_id:
            QMessageBox.information(self, "Jarvis", "当前还没有会话，先聊一句。")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("会话轨迹")
        dialog.resize(700, 500)
        layout = QVBoxLayout(dialog)
        view = QTextEdit()
        view.setReadOnly(True)
        view.setPlainText(render_trajectory(self._session_id, self._sessions))
        layout.addWidget(view)
        dialog.exec()

    # -- Diff / 检查点 Tab ------------------------------------------------

    def _on_tab_changed(self, index: int):
        if self.tabs.tabText(index) == "Diff":
            self._refresh_diff()
        elif self.tabs.tabText(index) == "检查点":
            self._refresh_ckpts()

    def _refresh_diff(self):
        """Diff Tab：工作区当前改动按文件分组（U5-1），每文件小节头
        + +/- 行数汇总，+/- 行绿/红，@@ 行弱化。含未跟踪文件。"""
        diff = working_tree_diff()
        if "不是一个 git 仓库" in diff:
            self.diff_view.setPlainText(
                "当前目录不是一个 git 仓库，看不到改动。")
            return
        files = parse_diff(diff)
        if not files:
            self.diff_view.setPlainText("还没有改动。")
            return
        colors = _theme_colors()
        out = []
        for f in files:
            out.append(
                f'<b>{html.escape(f["path"])}'
                f'（{html.escape(f["summary"])}）</b>')
            for line in f["lines"]:
                esc = html.escape(line)
                if line.startswith("+") and not line.startswith("+++"):
                    out.append(f'<font color="{colors.get("diffAdd", "#a6e3a1")}">'
                               f"{esc}</font>")
                elif line.startswith("-") and not line.startswith("---"):
                    out.append(f'<font color="{colors.get("diffDel", "#f38ba8")}">'
                               f"{esc}</font>")
                elif line.startswith("@@"):
                    out.append(f'<font color="{colors.get("textMuted", "#a6adc8")}">'
                               f"{esc}</font>")
                else:
                    out.append(esc)
        self.diff_view.setHtml("<br>".join(out))

    def _accept_diff(self):
        """接受全部：改动留在工作区，只记一条已审阅（U5 再做逐文件卡片）。"""
        self._note("已接受当前改动（改动保留在工作区）。")

    def _reject_diff(self):
        """拒绝全部：用检查点把经 Jarvis 写入的文件恢复到写入前。
        没经 Jarvis 写过的改动回滚不了，按钮会直说。"""
        if not self._checkpoints:
            self._note("没有检查点：拒绝全部只能回滚经 Jarvis 写入的改动，"
                       "当前会话还没有这类改动。")
            return
        rows = self._checkpoints.list_checkpoints()
        if not rows:
            self._note("没有可回滚的检查点。")
            return
        restored = self._checkpoints.rewind(len(rows))
        if restored:
            self._note("已拒绝全部改动，恢复了："
                       + "、".join(os.path.basename(p) for p in restored))
        else:
            self._note("没有可恢复的文件。")
        self._refresh_diff()
        self._refresh_ckpts()

    def _refresh_ckpts(self):
        self.ckpt_list.clear()
        if not self._checkpoints:
            return
        for row in self._checkpoints.list_checkpoints():
            ts = row.get("ts", "")[:19].replace("T", " ")
            path = row.get("path", "")
            item = QListWidgetItem(f"{ts}  {os.path.basename(path)}")
            item.setData(Qt.UserRole, row.get("id", ""))
            self.ckpt_list.addItem(item)

    def _on_bubble_restore(self, bubble):
        """悬停“恢复检查点”：文件回到这条消息挂载之前，对话内容不动。"""
        if self._busy:
            self._note("正忙，等这轮跑完再恢复检查点。")
            return
        if not self._checkpoints:
            self._note("没有检查点：还没有经 Jarvis 写入的改动。")
            return
        restored = self._checkpoints.rewind_to(bubble.mounted_at)
        if restored:
            self._note("已恢复检查点："
                       + "、".join(os.path.basename(p) for p in restored)
                       + "（只回滚了文件，对话内容不动）")
        else:
            self._note("没有可恢复的检查点："
                       "这条消息之后没有经 Jarvis 写入的改动。")
        self._refresh_ckpts()
        self._refresh_diff()

    def _on_ckpt_clicked(self, item):
        """点一条检查点：恢复到那一轮（只回滚文件，消息不动）。"""
        if self._busy or not self._checkpoints:
            return
        rows = self._checkpoints.list_checkpoints()
        steps = len(rows) - self.ckpt_list.row(item)
        restored = self._checkpoints.rewind(max(1, steps))
        if restored:
            self._note("已恢复检查点："
                       + "、".join(os.path.basename(p) for p in restored)
                       + "（只回滚了文件，对话内容不动）")
        else:
            self._note("没有可恢复的文件。")
        self._refresh_ckpts()
        self._refresh_diff()

    # -- chat flow ---------------------------------------------------------

    def send(self):
        text = self.input_box.toPlainText().strip()
        if not text:
            return
        self.input_box.clear()
        if self._busy:
            # 忙时回车：排队成追问，轮次结束自动发
            self._queue.append(text)
            self._refresh_queue_label()
            return
        self._start_turn(text)

    def _on_send_button(self):
        if self._busy:
            if self._worker is not None:
                self._worker.request_stop()
                self.send_button.setText("停止中…")
                self.send_button.setEnabled(False)
            return
        self.send()

    def _refresh_queue_label(self):
        if not self._queue:
            self.queue_label.setVisible(False)
            return
        first = self._queue[0]
        short = first[:20] + ("…" if len(first) > 20 else "")
        self.queue_label.setText(
            f"排队中（{len(self._queue)}）：{short}")
        self.queue_label.setVisible(True)

    def _start_turn(self, text: str, regenerate: bool = False,
                    mount_user: bool = True):
        """开一轮：挂用户气泡（重新生成时不挂，已有）→ 起 worker。

        计划开关开着时走 plan_mode：worker 只出计划（只读），
        plan_ready 后挂计划卡片等批准，不直接执行。
        mount_user=False 是计划获批后的执行轮：提示词很长，不挂气泡。
        """
        if text != self._last_user_text:
            # 新问题：连续出错次数清零（重试/重新生成是同一问题，不清）
            self._error_streak = 0
        self._last_user_text = text
        if not regenerate and mount_user:
            bubble = MessageBubble(
                "user", self._copy_text,
                on_restore=self._on_bubble_restore)
            bubble.set_plain(text)
            self._add_transcript_widget(bubble)
        if self._client is None:
            # 没 key：不发请求，就地给卡片 + 去填 key（U6-3/U6-4）
            card = ErrorCardWidget(
                "还没填 DeepSeek key，对话发不出去。",
                extra_button=("去填 key", self.open_settings))
            self._add_transcript_widget(card)
            self.status_label.setText("就绪")
            return
        self._set_busy(True)
        self._bubble = None
        self._turn_bubbles = []
        self._thinking = None
        self._tool_card = None
        self.status_label.setText("思考中…")
        if not self._session_id:
            self._session_id = self._sessions.create(
                title=text[:24], model=self._config.model)
            self._checkpoints = CheckpointStore(self._session_id)
            set_current_store(self._checkpoints)
            register_before_write_hook(snapshot_if_active)
            self._refresh_session_list()
        # 长期记忆可能刚变过，刷新 system prompt 再跑循环
        self._memory.messages[0] = {
            "role": "system",
            "content": self._system_prompt(),
        }
        plan_mode = self.plan_button.isChecked() and not regenerate
        if plan_mode:
            self._plan_task = text
            self.status_label.setText("正在调研并制定计划…")
        self._thread = QThread()
        self._worker = AgentWorker(
            self._client, text, self._memory.messages,
            self._schemas, self._funcs, gate=self._gate,
            session_store=self._sessions, session_id=self._session_id,
            model=self._config.model, max_steps=self._config.max_steps,
            max_budget_usd=self._config.max_budget_usd,
            usage=self._usage, plan_mode=plan_mode)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.token.connect(self._on_token)
        self._worker.thinking.connect(self._on_thinking)
        self._worker.tool_started.connect(self._on_tool_started)
        self._worker.tool_finished.connect(self._on_tool_finished)
        self._worker.status.connect(self._on_status)
        self._worker.confirm_requested.connect(self._on_confirm_requested)
        self._worker.plan_ready.connect(self._on_plan_ready)
        self._worker.finished.connect(self._on_finished)
        self._worker.stopped.connect(self._on_stopped)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.stopped.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        # 注意：不要接 worker/thread 的 deleteLater。Qt 删了 C++ 对象后，
        # MainWindow 手里的 Python 包装再被释放时 Shiboken 会二次删除，
        # 曾导致段错误（野指针）与“QThread destroyed while running” abort。
        # 轮次收尾由 _stop_turn_thread 显式 quit()+wait() 再丢引用，单边删。
        self._thread.start()

    def _ensure_bubble(self) -> MessageBubble:
        """当前轮正在写的 Jarvis 气泡；工具卡片打断后开一个新气泡。"""
        if self._bubble is None:
            self._bubble = MessageBubble(
                "jarvis", self._copy_text, self._on_regenerate,
                on_restore=self._on_bubble_restore)
            self._turn_bubbles.append(self._bubble)
            self._add_transcript_widget(self._bubble)
        return self._bubble

    def _on_token(self, text: str):
        bubble = self._ensure_bubble()
        bubble.append_text(text)
        if self._following:
            bar = self.chat_scroll.verticalScrollBar()
            bar.setValue(bar.maximum())
        self._refresh_statusbar()

    def _on_thinking(self, text: str):
        if self._thinking is None:
            self._thinking = ThinkingWidget()
            # 思考先于正文：插到当前气泡前面
            index = self.chat_layout.count() - 1
            if self._bubble is not None:
                index = self.chat_layout.indexOf(self._bubble)
            self.chat_layout.insertWidget(index, self._thinking)
        self._thinking.append_text(text)

    def _on_tool_started(self, payload: dict):
        from jarvis.ui.events import summarise_tool_call
        # 工具一来，当前气泡断开（下一段文字开新气泡，顺序不乱）
        self._bubble = None
        card = ToolCardWidget(payload["name"], payload["args"],
                              payload["risk"])
        card.set_started(summarise_tool_call(
            payload["name"], payload["args"]))
        self._tool_card = card
        self._add_transcript_widget(card)

    def _on_tool_finished(self, payload: dict):
        if self._tool_card is not None:
            self._tool_card.set_finished(
                payload["status"], payload["duration_ms"],
                payload["result"])
            self._tool_card = None

    def _on_status(self, text: str):
        self.status_label.setText(text)

    def _on_confirm_requested(self, action: dict):
        """写操作要确认：卡片长在聊天流里，不弹窗。"""
        key = (action.get("kind", ""), action.get("target", ""))
        if key in self._always_allowed:
            if self._worker is not None:
                self._worker.answer_confirm(True)
            return
        self._pending_confirm_action = action
        card = ConfirmCardWidget(action, self._answer_confirm)
        self._confirm_card = card
        self._add_transcript_widget(card)

    def _answer_confirm(self, kind: str):
        action = self._pending_confirm_action or {}
        key = (action.get("kind", ""), action.get("target", ""))
        ok = False
        if kind == "once":
            ok = True
        elif kind == "always":
            ok = True
            self._always_allowed.add(key)
        elif kind == "edit":
            ok = False
            detail = action.get("detail", "") or \
                f"{action.get('kind', '')} {action.get('target', '')}"
            self.input_box.setPlainText(detail)
            self.input_box.setFocus()
        if self._confirm_card is not None:
            self._confirm_card.deleteLater()
            self._confirm_card = None
        self._pending_confirm_action = None
        if self._worker is not None:
            self._worker.answer_confirm(ok)

    def _finalize_bubbles(self):
        """收尾：本轮每个气泡里含 Markdown 构造的，换渲染版；纯文本不动。"""
        for bubble in self._turn_bubbles:
            text = bubble.plain_text()
            if text.strip() and _looks_like_markdown(text):
                bubble.set_markdown(text)

    def _stop_turn_thread(self):
        """停掉本轮的工作线程并丢引用（主线程里同步做）。

        必须等线程真正停下再丢引用：包装一死，Shiboken 就删 C++ 的
        QThread，线程还活着就会 abort。quit() 已在 finished/stopped/
        error 信号里排队，这里再叫一次无害；wait() 兜底。
        """
        thread = self._thread
        self._thread = None
        self._worker = None
        if thread is not None:
            thread.quit()
            thread.wait(3000)

    def _on_finished(self, answer: str):
        self._error_streak = 0
        self._finalize_bubbles()
        self._set_busy(False)
        self.status_label.setText("就绪")
        self._refresh_statusbar()
        self._refresh_memory_list()
        self._refresh_session_list()
        self._refresh_ckpts()
        self._stop_turn_thread()
        self._drain_queue()

    def _on_stopped(self):
        self._error_streak = 0
        if self._turn_bubbles:
            self._turn_bubbles[-1].append_text("\n■ 已停止")
        self._set_busy(False)
        self.status_label.setText("已停止")
        self._refresh_statusbar()
        self._stop_turn_thread()
        self._drain_queue()

    def _on_error(self, message: str):
        """出错：原文 + 一句恢复提示（U6-4）。

        第一次出错给“重试”；重试过一次还失败，就不再给重试，改给
        “复制问题，开新会话”（换个干净上下文再问一次，比原地死磕强）。"""
        self._error_streak += 1
        hint = friendly_error(message)
        text = message + (f"（{hint}）" if hint else "")
        if self._error_streak == 1:
            card = ErrorCardWidget(text, self._retry_last)
        else:
            card = ErrorCardWidget(
                text, extra_button=("复制问题，开新会话",
                                    self._copy_question_new_session))
        self._add_transcript_widget(card)
        self._set_busy(False)
        self.status_label.setText("出错")
        self._refresh_statusbar()
        self._stop_turn_thread()
        self.plan_button.setChecked(False)

    def _copy_question_new_session(self):
        """重试一次还失败：问题复制到剪贴板，开新会话，填回输入框。"""
        text = self._last_user_text
        if text:
            QApplication.clipboard().setText(text)
        self.new_session()
        if text:
            self.input_box.setPlainText(text)
            self.input_box.setFocus()

    def _on_plan_ready(self, payload: dict):
        """计划已出（U4-1）：停掉规划 worker，挂计划卡片等批准。"""
        self._stop_turn_thread()
        self.status_label.setText("计划已出，等批准")
        card = PlanCardWidget(payload, self._approve_plan, self._reject_plan)
        self._pending_plan = card
        self._add_transcript_widget(card)

    def _approve_plan(self, markdown: str):
        """批准：解析卡片里的计划文本 → 挂清单 → 按计划执行。"""
        plan = parse_plan(markdown)
        self._pending_plan = None
        self.plan_button.setChecked(False)
        self._set_todo(plan.to_todos())
        self._refresh_todos()
        self.tabs.setCurrentIndex(1)  # 切到待办 Tab，看清单实时打勾
        self._add_transcript_widget(NoteWidget("已批准，开始按计划执行。"))
        task = self._plan_task or ""
        prompt = (f"按顺序执行下面的计划，每完成一步调 update_todo 更新进度。"
                  f"\n\n{plan.render()}\n\n任务：{task}")
        self._start_turn(prompt, mount_user=False)

    def _reject_plan(self):
        """拒绝：计划不执行，恢复空闲，排队的下一条继续。"""
        self._pending_plan = None
        self.plan_button.setChecked(False)
        self._add_transcript_widget(NoteWidget("已拒绝，计划没有执行。"))
        self._set_busy(False)
        self.status_label.setText("就绪")
        self._refresh_statusbar()
        self._drain_queue()

    def _retry_last(self):
        if self._last_user_text:
            self._start_turn(self._last_user_text)

    def _drain_queue(self):
        self._refresh_queue_label()
        if self._queue and not self._busy:
            self._start_turn(self._queue.pop(0))
            self._refresh_queue_label()

    def _on_regenerate(self, bubble: MessageBubble):
        """重新生成：删掉本轮 trailing 的 assistant/tool 消息，重发上一条用户消息。
        界面上也把最后一个用户气泡之后的气泡/卡片拿掉。"""
        if self._busy:
            return
        msgs = self._memory.messages
        while msgs and msgs[-1].get("role") in ("assistant", "tool"):
            msgs.pop()
        last_user = next((m for m in reversed(msgs)
                          if m.get("role") == "user"), None)
        if not last_user:
            return
        self._remove_widgets_after_last_user_bubble()
        self._start_turn(str(last_user.get("content", "")), regenerate=True)

    def _remove_widgets_after_last_user_bubble(self):
        last_user_index = -1
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            widget = item.widget() if item is not None else None
            if isinstance(widget, MessageBubble) and widget.role == "user":
                last_user_index = i
        if last_user_index < 0:
            return
        while self.chat_layout.count() - 1 > last_user_index + 1:
            item = self.chat_layout.takeAt(last_user_index + 1)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._bubble = None
        self._turn_bubbles = []

    def _copy_text(self, text: str):
        QApplication.clipboard().setText(text)

    def _set_todo(self, todo: TodoList) -> None:
        """换一张清单：挂 on_change（落盘 + 发 todo_changed），重挂工具。"""
        self._todo = todo
        todo.on_change = self._on_todo_change
        attach_todo_tools(todo, self._schemas, self._funcs)

    def _on_todo_change(self) -> None:
        """清单一变：落盘到会话文件，并发信号让主线程重渲染待办 Tab。"""
        if self._session_id:
            self._todo.save(todos_path(
                self._sessions.root, self._session_id))
        self.todo_changed.emit()

    def _refresh_todos(self) -> None:
        """待办 Tab 全量重渲染（U4-2：todo_set 的界面侧）。"""
        marks = {"done": "☑", "active": "◐", "pending": "☐"}
        self.todo_list.clear()
        for item in self._todo.to_event():
            self.todo_list.addItem(
                f"{marks[item['status']]} {item['content']}")
        if not self._todo.items:
            self.todo_list.addItem("(清单是空的，打开计划开关发一轮就有)")

    def _refresh_statusbar(self):
        """只刷新 context 条和用量（状态文本是 worker 的，另起一行，不抢）。"""
        usage = self._usage
        pct = usage.context_pct()
        self.context_label.setText(
            f"context {_context_bar(pct)}")
        text = f"用量：本轮 {usage.total_tokens} token · ¥{usage.cost_cny:.4f}"
        if usage.unknown_calls:
            text += f" · 未返回用量 {usage.unknown_calls} 次"
        self.usage_label.setText(text)

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.send_button.setText("■ 停止" if busy else "发送")
        self.send_button.setEnabled(True)
        if not busy:
            self.input_box.setFocus()

    def clear_chat(self):
        if self._busy:
            return
        self._memory.clear()
        self._clear_transcript()
        self._note("对话已清空，长期记忆还在。")

    def closeEvent(self, event):
        cfg = self._config
        cfg.window_width = self.width()
        cfg.window_height = self.height()
        cfg.window_x = self.x()
        cfg.window_y = self.y()
        try:
            cfg.splitter_sizes = [int(x) for x in self._splitter.sizes()]
        except (TypeError, ValueError):
            pass
        save_config(cfg)
        timer = getattr(self, "_wizard_timer", None)
        if timer is not None:
            # 关窗时向导还没弹：停掉并断开回调，放掉它对窗口的引用。
            # 不然挂起的回调会拖住窗口，退出收尾时在 GC 里段错误。
            timer.stop()
            try:
                timer.timeout.disconnect(self._open_first_run_wizard)
            except (TypeError, RuntimeError):
                pass
        self._stop_turn_thread()
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    prefs = load_ui_prefs()
    theme_name = prefs.get("theme", "dark")
    app.setStyleSheet(to_qss(load_theme(theme_name)) + _component_style())
    try:
        client = build_client()
    except SystemExit:
        # 没 key：窗口照常打开，首次启动向导随后自动弹（U6-3），
        # 不再像以前那样弹致命框直接退出。
        client = None
    window = MainWindow(client)
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
