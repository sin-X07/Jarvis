"""PySide6 worker: the agent loop runs in a QThread, results come back as signals.

Qt rule: never touch widgets from a worker thread. The worker only emits
signals; slots in the main thread update the UI (Qt queues cross-thread
signals automatically, so this is thread-safe by construction).
"""

from PySide6.QtCore import QObject, Signal

from agent import run_agent


class AgentWorker(QObject):
    token = Signal(str)      # 流式吐出的每段文字
    tool_call = Signal(str)  # 调了哪个工具（界面上显示一行小灰字）
    status = Signal(str)     # 进度，如“正在调用 calculator…”
    finished = Signal(str)   # 整轮结束，带最终回答
    error = Signal(str)      # 出错，带错误信息

    def __init__(self, client, user_message, history,
                 extra_tools=None, extra_funcs=None):
        super().__init__()
        self._client = client
        self._user_message = user_message
        self._history = history
        self._extra_tools = extra_tools
        self._extra_funcs = extra_funcs

    def run(self):
        """QThread 启动后调这个。"""
        try:
            answer = run_agent(
                self._client, self._user_message,
                history=self._history,
                extra_tools=self._extra_tools,
                extra_funcs=self._extra_funcs,
                on_event=self._on_event, stream=True, verbose=False,
            )
            self.finished.emit(answer)
        except Exception as exc:  # noqa: BLE001 - report to the UI, don't crash the thread
            self.error.emit(str(exc))

    def _on_event(self, kind, payload):
        if kind == "token":
            self.token.emit(payload)
        elif kind == "tool_call":
            args = str(payload["args"])
            if len(args) > 80:
                args = args[:80] + "…"
            self.tool_call.emit(f"▸ 调用 {payload['name']}({args})")
            self.status.emit(f"正在调用 {payload['name']}…")
        elif kind == "tool_result":
            self.status.emit(f"{payload['name']} 已返回")
