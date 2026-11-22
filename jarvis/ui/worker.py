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

from jarvis.core.agent import run_agent
from jarvis.core.usage import UsageTracker
from jarvis.ui.events import summarise_tool_call, tool_risk


class _StopRequested(Exception):
    """GUI 点了 Stop：下一段 token/思考/工具事件处掐断本轮（TUI 同款做法）。"""


class AgentWorker(QObject):
    token = Signal(str)      # 流式吐出的每段文字
    thinking = Signal(str)   # 思考流（reasoning_content）的每段文字
    tool_call = Signal(str)  # 调了哪个工具（界面上显示一行小灰字；U3 起由工具卡片接管）
    tool_started = Signal(dict)   # 工具卡片：{"name","args","risk"}
    tool_finished = Signal(dict)  # 工具卡片结算：{"name","status","duration_ms","result"}
    status = Signal(str)     # 进度，如“正在调用 calculator…”
    finished = Signal(str)   # 整轮结束，带最终回答
    stopped = Signal()       # 用户点了 Stop，本轮已掐断（已出文字保留）
    error = Signal(str)      # 出错，带错误信息
    confirm_requested = Signal(dict)  # 需要用户确认：{"kind","target","detail"}
    plan_ready = Signal(dict)  # U4-1 计划模式：计划已出，等批准
    # {"markdown","goal","steps","risks"}

    def __init__(self, client, user_message, history,
                 extra_tools=None, extra_funcs=None, gate=None,
                 session_store=None, session_id=None, model=None,
                 max_steps=None, max_budget_usd=None, usage=None,
                 plan_mode: bool = False):
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
        self._plan_mode = plan_mode
        self._confirm_event = threading.Event()
        self._confirm_result = False
        self._stop_requested = False

    def request_stop(self) -> None:
        """主线程点 Stop：下一段事件处掐断（线程本身不杀，安全）。"""
        self._stop_requested = True

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
            if self._plan_mode:
                # U4-1：只读调研出计划，不执行；主线程挂计划卡片等批准。
                from jarvis.core.agent import MODEL
                from jarvis.memory.planner import make_plan
                plan = make_plan(
                    self._client,
                    self._model if self._model is not None else MODEL,
                    self._user_message,
                    extra_tools=self._extra_tools,
                    extra_funcs=self._extra_funcs,
                    max_steps=self._max_steps or 8,
                    on_event=self._on_event, usage=self.usage)
                self.plan_ready.emit({
                    "markdown": plan.render(), "goal": plan.goal,
                    "steps": plan.steps, "risks": plan.risks})
                return
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
        except _StopRequested:
            self.stopped.emit()
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
        if self._stop_requested and kind in (
                "text_delta", "thinking_delta", "tool_start"):
            raise _StopRequested()
        if kind == "text_delta":
            self.token.emit(payload["text"])
        elif kind == "thinking_delta":
            self.thinking.emit(payload["text"])
        elif kind == "tool_start":
            self.tool_started.emit({
                "name": payload["name"], "args": payload["args"],
                "risk": tool_risk(payload["name"])})
            self.tool_call.emit("▸ " + summarise_tool_call(
                payload["name"], payload["args"]))
            self.status.emit(f"正在调用 {payload['name']}…")
        elif kind == "tool_end":
            self.tool_finished.emit({
                "name": payload["name"], "status": payload["status"],
                "duration_ms": payload["duration_ms"],
                "result": str(payload["result"])[:500]})
            mark = "✓" if payload["status"] == "ok" else "✗"
            self.status.emit(
                f"{mark} {payload['name']} 已返回"
                f"（{payload['duration_ms']}ms）")


class VerifyWorker(QObject):
    """U7：计划执行完的验收 worker（GUI 专用）。

    计划获批后的执行轮在 AgentWorker 里跑完、finished 信号回主线程后，
    主线程再起这个 worker 在 QThread 里调 verify_plan：对照验收标准
    逐项检查，报告回主线程挂成一张卡片。验收轮的 token 也进 usage。
    """

    verified = Signal(str)   # 验收报告全文
    failed = Signal(str)     # 验收本身出错（不拦主流程，只提示）

    def __init__(self, client, model, plan, answer, usage=None):
        super().__init__()
        self._client = client
        self._model = model
        self._plan = plan
        self._answer = answer
        self._usage = usage

    def run(self) -> None:
        try:
            from jarvis.memory.planner import verify_plan
            report = verify_plan(self._client, self._model, self._plan,
                                 self._answer, usage=self._usage)
            self.verified.emit(report)
        except Exception as exc:  # noqa: BLE001 - 验收失败只提示，不崩线程
            self.failed.emit(str(exc))

