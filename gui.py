"""Jarvis GUI (PySide6, P7 升级)：聊天窗口 + 会话列表 + 设置 + 检查点。

布局：
    +-------------------------------+------------------+
    | chat view (scrollable)        | 会话列表         |
    |                               | 长期记忆         |
    |                               | 模型/状态/用量   |
    +-------------------------------+------------------+
    | [input box            ] [发送] | [设置][恢复检查点]|
    +-------------------------------+------------------+

Markdown 渲染：流式输出时先按纯文本往里插（实时感）；一轮结束时，
如果本轮没调工具，把刚插进去的纯文本整段换成 md.to_html() 渲染后的
版本（有工具调用的轮次保持原样，避免工具行被误删）。
确认：写操作在 edit 档位要弹确认框——worker 发信号，主线程弹窗，
结果经 worker.answer_confirm() 传回 worker 线程。

Threading: sending a message starts an AgentWorker in a QThread.
The worker runs run_agent (blocking network calls) and emits signals;
slots in this file update the widgets. Nothing in worker.py touches UI.

Run:  python gui.py
"""

import html
import sys

from PySide6.QtCore import Qt, QThread
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QSpinBox,
    QSplitter, QTextEdit, QVBoxLayout, QWidget,
)

from agent import MODEL, SYSTEM_PROMPT, build_client
from checkpoint import CheckpointStore, set_current_store
from config import load_config, save_config
from md import to_html
from memory import ConversationMemory, FactStore, memory_tools
from permissions import ApprovalGate
from session_store import SessionStore
from trajectory import render_trajectory
from usage import UsageTracker
from worker import AgentWorker

DARK_STYLE = """
QMainWindow { background: #1e1e2e; }
QTextEdit { background: #181825; color: #cdd6f4; border: 1px solid #313244;
            border-radius: 6px; font-size: 14px; }
QLineEdit { background: #181825; color: #cdd6f4; border: 1px solid #313244;
            border-radius: 6px; padding: 6px; font-size: 14px; }
QPushButton { background: #89b4fa; color: #1e1e2e; border: none;
              border-radius: 6px; padding: 6px 16px; font-size: 14px; }
QPushButton:disabled { background: #45475a; color: #a6adc8; }
QListWidget { background: #181825; color: #cdd6f4; border: 1px solid #313244;
              border-radius: 6px; font-size: 13px; }
QLabel { color: #a6adc8; font-size: 13px; }
QComboBox, QSpinBox, QDoubleSpinBox { background: #181825; color: #cdd6f4;
            border: 1px solid #313244; border-radius: 6px; padding: 4px; }
"""


class SettingsDialog(QDialog):
    """设置对话框：模型、权限档位、最大步数。保存后写回 config.json。"""

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self._config = config
        layout = QFormLayout(self)
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
        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def apply(self) -> None:
        self._config.model = self.model_edit.text().strip() or MODEL
        self._config.permission = self.permission_box.currentText()
        self._config.max_steps = self.steps_spin.value()
        save_config(self._config)


class MainWindow(QMainWindow):
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
        self._thread = None
        self._worker = None
        self._busy = False
        self._streaming = False  # 本轮是否已开始流式输出
        self._streamed = False   # 本轮是否收到过 token
        self._stream_start = 0   # 流式答案在文档里的起始位置
        self._turn_had_tool_call = False
        self._build_ui()
        self._refresh_memory_list()
        self._refresh_session_list()
        self.setWindowTitle("Jarvis")
        self.resize(1100, 650)

    # -- UI construction ------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter)

        # Left: chat view + input row
        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.chat_view = QTextEdit()
        self.chat_view.setReadOnly(True)
        left_layout.addWidget(self.chat_view)
        input_row = QWidget()
        row = QHBoxLayout(input_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.input_box = QLineEdit()
        self.input_box.setPlaceholderText("和 Jarvis 说点什么…（回车发送）")
        self.input_box.returnPressed.connect(self.send)
        self.send_button = QPushButton("发送")
        self.send_button.clicked.connect(self.send)
        row.addWidget(self.input_box)
        row.addWidget(self.send_button)
        left_layout.addWidget(input_row)
        splitter.addWidget(left)

        # Right: sessions + memory + status + buttons
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(QLabel("会话"))
        self.session_list = QListWidget()
        self.session_list.itemClicked.connect(self._on_session_clicked)
        right_layout.addWidget(self.session_list)
        self.new_session_button = QPushButton("新会话")
        self.new_session_button.clicked.connect(self.new_session)
        right_layout.addWidget(self.new_session_button)
        right_layout.addWidget(QLabel("长期记忆"))
        self.memory_list = QListWidget()
        right_layout.addWidget(self.memory_list)
        self.model_label = QLabel(f"模型：{self._config.model}")
        right_layout.addWidget(self.model_label)
        self.permission_label = QLabel(f"权限：{self._config.permission}")
        right_layout.addWidget(self.permission_label)
        self.status_label = QLabel("就绪")
        right_layout.addWidget(self.status_label)
        self.usage_label = QLabel("用量：本轮 0 token")
        right_layout.addWidget(self.usage_label)
        btn_row = QWidget()
        btn_layout = QHBoxLayout(btn_row)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        self.settings_button = QPushButton("设置")
        self.settings_button.clicked.connect(self.open_settings)
        btn_layout.addWidget(self.settings_button)
        self.rewind_button = QPushButton("恢复检查点")
        self.rewind_button.clicked.connect(self.rewind_checkpoint)
        btn_layout.addWidget(self.rewind_button)
        self.trajectory_button = QPushButton("轨迹")
        self.trajectory_button.clicked.connect(self.show_trajectory)
        btn_layout.addWidget(self.trajectory_button)
        right_layout.addWidget(btn_row)
        self.clear_button = QPushButton("清空对话")
        self.clear_button.clicked.connect(self.clear_chat)
        right_layout.addWidget(self.clear_button)
        right_layout.addStretch()
        splitter.addWidget(right)
        splitter.setSizes([750, 350])

        self.chat_view.append("Jarvis 已启动。直接说话，或者先记住点什么："
                              "“记住，我最喜欢蓝色”。")

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
        self.chat_view.clear()
        for msg in messages:
            if msg.get("role") == "user":
                self.chat_view.append(
                    f'<p align="right"><font color="#89b4fa">你：'
                    f"{html.escape(str(msg.get('content', '')))}</font></p>")
            elif msg.get("role") == "assistant" and msg.get("content"):
                self.chat_view.append(
                    f"<b>Jarvis：</b>{to_html(str(msg['content']))}")
        self.status_label.setText(f"已恢复会话 {session_id[:12]}")

    def new_session(self):
        if self._busy:
            return
        self._session_id = None
        self._checkpoints = None
        set_current_store(None)
        self._memory.clear()
        self.chat_view.clear()
        self.chat_view.append("新会话。")
        self.status_label.setText("就绪")

    def open_settings(self):
        dialog = SettingsDialog(self._config, self)
        if dialog.exec():
            dialog.apply()
            self._gate.mode = self._config.permission
            self.model_label.setText(f"模型：{self._config.model}")
            self.permission_label.setText(f"权限：{self._config.permission}")
            self.status_label.setText("设置已保存")

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

    def rewind_checkpoint(self):
        if self._busy:
            return
        if not self._checkpoints:
            QMessageBox.information(self, "Jarvis", "当前会话还没有检查点。")
            return
        restored = self._checkpoints.rewind(1)
        if restored:
            self.chat_view.append(
                '<p><i><font color="#6c7086">已恢复检查点：'
                + html.escape("、".join(restored)) + "</font></i></p>")
        else:
            QMessageBox.information(self, "Jarvis", "没有可恢复的检查点。")

    # -- chat flow ---------------------------------------------------------

    def send(self):
        if self._busy:
            return
        text = self.input_box.text().strip()
        if not text:
            return
        self.input_box.clear()
        self.chat_view.append(
            f'<p align="right"><font color="#89b4fa">你：'
            f"{html.escape(text)}</font></p>")
        self._set_busy(True)
        self._streaming = False
        self._streamed = False
        self._turn_had_tool_call = False
        self.status_label.setText("思考中…")
        if not self._session_id:
            self._session_id = self._sessions.create(
                title=text[:24], model=self._config.model)
            self._checkpoints = CheckpointStore(self._session_id)
            set_current_store(self._checkpoints)
            self._refresh_session_list()
        # 长期记忆可能刚变过，刷新 system prompt 再跑循环
        self._memory.messages[0] = {
            "role": "system",
            "content": self._system_prompt(),
        }
        self._thread = QThread()
        self._worker = AgentWorker(
            self._client, text, self._memory.messages,
            self._schemas, self._funcs, gate=self._gate,
            session_store=self._sessions, session_id=self._session_id,
            model=self._config.model, max_steps=self._config.max_steps,
            max_budget_usd=self._config.max_budget_usd,
            usage=self._usage)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.token.connect(self._on_token)
        self._worker.tool_call.connect(self._on_tool_call)
        self._worker.status.connect(self.status_label.setText)
        self._worker.confirm_requested.connect(self._on_confirm_requested)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.error.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _on_token(self, text: str):
        if not self._streaming:
            # 第一片 token：先起一个“Jarvis：”段落，记下答案起始位置
            self._streaming = True
            self._streamed = True
            self.chat_view.append("<b>Jarvis：</b>")
            cursor = self.chat_view.textCursor()
            cursor.movePosition(QTextCursor.End)
            self._stream_start = cursor.position()
            fmt = QTextCharFormat()
            fmt.setForeground(QColor("#cdd6f4"))
            fmt.setFontWeight(400)
            cursor.setCharFormat(fmt)
            self.chat_view.setTextCursor(cursor)
        cursor = self.chat_view.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(text)
        self.chat_view.ensureCursorVisible()

    def _on_tool_call(self, line: str):
        self._turn_had_tool_call = True
        self.chat_view.append(
            f'<p><i><font color="#6c7086">{html.escape(line)}</font></i></p>')

    def _on_confirm_requested(self, action: dict):
        """主线程槽：写操作要确认，弹框问用户，结果传回 worker。"""
        kind = action.get("kind", "")
        target = action.get("target", "")
        detail = action.get("detail", "")
        question = f"Jarvis 想执行：{kind} {target}"
        if detail:
            question += f"\n\n{detail[:500]}"
        answer = QMessageBox.question(
            self, "需要确认", question,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if self._worker is not None:
            self._worker.answer_confirm(answer == QMessageBox.Yes)

    def _on_finished(self, answer: str):
        if self._streamed and not self._turn_had_tool_call:
            # 没调工具：把刚流式插进去的纯文本换成 Markdown 渲染版
            cursor = self.chat_view.textCursor()
            cursor.setPosition(self._stream_start)
            cursor.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
            cursor.removeSelectedText()
            cursor.insertHtml(to_html(answer))
        elif not self._streamed:
            # 没收到流式 token（比如模型一次答完没走流式），整段补上
            self.chat_view.append(
                f"<b>Jarvis：</b>{to_html(answer)}")
        self._streaming = False
        self._set_busy(False)
        self.status_label.setText("就绪")
        self.usage_label.setText("用量：" + self._usage.summary())
        self._refresh_memory_list()
        self._refresh_session_list()

    def _on_error(self, message: str):
        self.chat_view.append(
            f'<p><font color="#f38ba8">出错了：{html.escape(message)}</font></p>')
        self._streaming = False
        self._set_busy(False)
        self.status_label.setText("出错")

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.input_box.setEnabled(not busy)
        self.send_button.setEnabled(not busy)
        if not busy:
            self.input_box.setFocus()

    def clear_chat(self):
        if self._busy:
            return
        self._memory.clear()
        self.chat_view.clear()
        self.chat_view.append("对话已清空，长期记忆还在。")

    def closeEvent(self, event):
        if self._thread is not None and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(3000)
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_STYLE)
    try:
        client = build_client()
    except SystemExit as exc:
        QMessageBox.critical(None, "Jarvis", str(exc))
        raise SystemExit(1)
    window = MainWindow(client)
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
