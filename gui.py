"""Jarvis GUI (PySide6): a chat window on top of the agent loop.

Layout:
    +-------------------------------+------------------+
    | chat view (scrollable)        | 长期记忆         |
    |                               | 状态             |
    +-------------------------------+------------------+
    | [input box            ] [发送] | [清空对话]       |
    +-------------------------------+------------------+

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
    QApplication, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMainWindow,
    QMessageBox, QPushButton, QSplitter, QTextEdit, QVBoxLayout,
    QWidget,
)

from agent import MODEL, SYSTEM_PROMPT, build_client
from memory import ConversationMemory, FactStore, memory_tools
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
"""


class MainWindow(QMainWindow):
    def __init__(self, client):
        super().__init__()
        self._client = client
        self._store = FactStore()
        self._memory = ConversationMemory(self._system_prompt())
        self._schemas, self._funcs = memory_tools(self._store)
        self._thread = None
        self._worker = None
        self._busy = False
        self._streaming = False  # 本轮是否已开始流式输出
        self._streamed = False   # 本轮是否收到过 token
        self._build_ui()
        self._refresh_memory_list()
        self.setWindowTitle("Jarvis")
        self.resize(1000, 650)

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

        # Right: memory list + status + clear button
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(QLabel("长期记忆"))
        self.memory_list = QListWidget()
        right_layout.addWidget(self.memory_list)
        self.model_label = QLabel(f"模型：{MODEL}")
        right_layout.addWidget(self.model_label)
        self.status_label = QLabel("就绪")
        right_layout.addWidget(self.status_label)
        self.clear_button = QPushButton("清空对话")
        self.clear_button.clicked.connect(self.clear_chat)
        right_layout.addWidget(self.clear_button)
        right_layout.addStretch()
        splitter.addWidget(right)
        splitter.setSizes([700, 300])

        self.chat_view.append("Jarvis 已启动。直接说话，或者先记住点什么："
                              "“记住，我最喜欢蓝色”。")

    # -- memory ----------------------------------------------------------

    def _system_prompt(self) -> str:
        facts = self._store.render_for_prompt()
        return f"{SYSTEM_PROMPT}\n{facts}" if facts else SYSTEM_PROMPT

    def _refresh_memory_list(self):
        self.memory_list.clear()
        self.memory_list.addItems(self._store.all_texts())

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
        self.status_label.setText("思考中…")
        # 长期记忆可能刚变过，刷新 system prompt 再跑循环
        self._memory.messages[0] = {
            "role": "system",
            "content": self._system_prompt(),
        }
        self._thread = QThread()
        self._worker = AgentWorker(
            self._client, text, self._memory.messages,
            self._schemas, self._funcs)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.token.connect(self._on_token)
        self._worker.tool_call.connect(self._on_tool_call)
        self._worker.status.connect(self.status_label.setText)
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
            # 第一片 token：先起一个“Jarvis：”段落，再把字往里插
            self._streaming = True
            self._streamed = True
            self.chat_view.append("<b>Jarvis：</b>")
            cursor = self.chat_view.textCursor()
            cursor.movePosition(QTextCursor.End)
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
        self.chat_view.append(
            f'<p><i><font color="#6c7086">{html.escape(line)}</font></i></p>')

    def _on_finished(self, answer: str):
        if not self._streamed:
            # 没收到流式 token（比如模型一次答完没走流式），整段补上
            self.chat_view.append(
                f"<b>Jarvis：</b>{html.escape(answer)}")
        self._streaming = False
        self._set_busy(False)
        self.status_label.setText("就绪")
        self._refresh_memory_list()

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
