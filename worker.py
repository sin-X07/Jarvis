"""PySide6 worker: the agent loop runs in a QThread, results come back as signals.

Qt rule: never touch widgets from a worker thread. The worker only emits
signals; slots in the main thread update the UI (Qt queues cross-thread
signals automatically, so this is thread-safe by construction).

Confirm flow (P7): a write action in edit mode needs the user's yes/no.
The agent loop (running in this thread) calls gate.confirm; the worker
turns that into a signal, then blocks on a threading.Event until the
main thread's slot answers. The dialog itself lives in gui.py.
"""

import threading

from PySide6.QtCore import QObject, Signal

from agent import run_agent
from usage import UsageTracker


class AgentWorker(QObject):
    token = Signal(str)      # 流式吐出的每段文字
    tool_call = Signal(str)  # 调了哪个工具（界面上显示一行小灰字）
    status = Signal(str)     # 进度，如“正在调用 calculator…”
    finished = Signal(str)   # 整轮结束，带最终回答
    error = Signal(str)      # 出错，带错误信息
    confirm_requested = Signal(dict)  # 需要用户确认：{"kind","target","detail"}

    def __init__(self, client, user_message, history,
                 extra_tools=None, extra_funcs=None, gate=None,
                 session_store=None, session_id=None, model=None,
                 max_steps=None, max_budget_usd=None, usage=None):
        super().__init__()
        self._client = client
        self._user_message = user_message
        self._history = history
        self._extra_tools = extra_tools
        self._extra_funcs = extra_funcs
        self._gate = gate
        self._session_store = session_store
        self._session_id = session_id
        self._model = model
        self._max_steps = max_steps
        self._max_budget_usd = max_budget_usd
        self.usage = usage if usage is not None else UsageTracker()
        self._confirm_event = threading.Event()
        self._confirm_result = False

    def run(self):
        """QThread 启动后调这个。"""
        try:
            gate = self._gate
            if gate is not None:
                gate.confirm = self._gui_confirm
            kwargs = {}
            if self._model is not None:
                kwargs["model"] = self._model
            if self._max_steps is not None:
                kwargs["max_steps"] = self._max_steps
            if self._max_budget_usd is not None:
                kwargs["max_budget_usd"] = self._max_budget_usd
            answer = run_agent(
                self._client, self._user_message,
                history=self._history,
                extra_tools=self._extra_tools,
                extra_funcs=self._extra_funcs,
                on_event=self._on_event, stream=True, verbose=False,
                usage=self.usage, gate=gate,
                session_store=self._session_store,
                session_id=self._session_id, **kwargs)
            self.finished.emit(answer)
        except Exception as exc:  # noqa: BLE001 - report to the UI, don't crash the thread
            self.error.emit(str(exc))

    def _gui_confirm(self, action: dict) -> bool:
        """在 worker 线程里被调：发信号问主线程，等用户点确定/取消。"""
        self._confirm_event.clear()
        self.confirm_requested.emit(dict(action))
        self._confirm_event.wait()
        return self._confirm_result

    def answer_confirm(self, ok: bool) -> None:
        """主线程的槽：用户点了确认框后调这个，把 worker 线程放行。"""
        self._confirm_result = bool(ok)
        self._confirm_event.set()

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
