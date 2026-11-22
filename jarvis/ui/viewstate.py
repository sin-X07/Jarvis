"""四界面共用的视图状态（UI 统一，U7）。

events.py 是“事件契约”（核心 → 界面，一次性的流）；这里是“状态机”
（界面当前在展示什么，可反复读、可重渲染）。四个界面（CLI / TUI /
GUI / 网页）收敛成这份状态的渲染器：状态怎么变只在这里定一次，
各界面只决定“用什么控件/按键把它画出来”。

统一的是功能和状态，不是操作方式：TUI 保持键盘驱动、GUI 保持鼠标
驱动、网页保持浏览器交互、CLI 保持打字命令。

内容：
  - ViewState：会话 / 计划 / 待办 / Diff / 检查点 / 用量 / 忙闲的
    同一份状态；apply_event(kind, payload) 按 events.py 的 11 种
    事件更新它（status 事件里 token 全是 API 真实 usage，无 usage
    时记 unknown，渲染成“—”，绝不估算）。
  - 统一文案：HELP_TEXT（快捷键/帮助总览）、EMPTY_HINTS（统一空态）、
    MODE_LABELS（顶栏模型/模式）、TODO_MARKS（☐/◐/☑）。
  - 小助手：format_usage、build_execute_prompt（计划获批后的执行
    提示词，四方同一份）、diff_note（Diff 接受/拒绝的统一文案）、
    checkpoint_steps_for_row / checkpoint_cutoff_for_row（检查点
    Tab 点一条 → 回滚到那一轮的换算，GUI/TUI/CLI 共用）。
"""

from jarvis.ui.events import EVENT_KINDS

# -- 统一文案 -------------------------------------------------------------
# 四个界面此前各写一份，内容基本相同；这里是唯一事实源，界面只引用。

MODE_LABELS = {
    "chat": "💬 对话",
    "plan": "📋 规划（只读）",
}

TODO_MARKS = {"pending": "☐", "active": "◐", "done": "☑"}
TODO_LABELS = {"pending": "待做", "active": "进行中", "done": "已完成"}

HELP_TEXT = """快捷键（只列当前可用的）：
  Enter        发送（TUI/GUI/网页；网页 Shift+Enter 换行）
  Esc          停止本轮 / 关闭弹窗（TUI/GUI）
  Ctrl+B       开关侧栏（TUI）
  Tab          忙时把追问排进队列（TUI/GUI/网页）
  ?            显示这份快捷键总览（CLI/TUI）
  /plan <任务>  先出计划卡片，批准后才执行
  /todo        看待办清单（☐ 待做 / ◐ 进行中 / ☑ 已完成）
  /diff        显示当前改动（按文件分组）
  /rewind [n]  恢复最近 n 个文件检查点（默认 1）
  /usage       最近 7 天用量小结
  /help        显示全部命令"""

EMPTY_TAGLINE = "能记住事情、读写文件、跑命令。"
EMPTY_HINTS = [
    "直接说话，或者先记住点什么：比如“记住，我最喜欢蓝色”。",
    "输入 / 看命令，? 看快捷键，/plan 先出计划再执行。",
]

SECTION_TITLES = {
    "sessions": "会话",
    "todos": "待办",
    "diff": "Diff",
    "checkpoints": "检查点",
    "jobs": "任务",
    "usage": "用量",
    "trajectory": "轨迹",
}


def build_execute_prompt(plan_render: str, task: str) -> str:
    """计划获批后的执行提示词：四方同一份（此前 CLI/TUI/GUI/网页各拼一份）。"""
    return ("按顺序执行下面的计划，每完成一步调 update_todo 更新进度。"
            f"\n\n{plan_render}\n\n任务：{task}")


def diff_note(accepted: bool) -> str:
    """Diff 接受/拒绝的统一文案。

    “接受”只是确认语义：改动本来就在工作区，不动文件。
    “拒绝”只能回滚经 Jarvis 写入的改动（靠检查点）；网页版没有检查点，
    调用方（网页 JS）用自己的文案直说去桌面端/TUI。
    """
    if accepted:
        return "已接受当前改动（改动保留在工作区）。"
    return ("已拒绝全部改动：经 Jarvis 写入的文件已用检查点恢复；"
            "没经 Jarvis 写过的改动回滚不了。")


def format_usage(total_tokens: int, cost_cny: float,
                 unknown_calls: int = 0) -> str:
    """用量一行：无真实用量时显示“—”，绝不估算顶替。"""
    if total_tokens:
        text = f"本轮 {total_tokens} token · ¥{cost_cny:.4f}"
    else:
        text = "用量：—"
    if unknown_calls:
        text += f" · 未返回用量 {unknown_calls} 次"
    return text


def checkpoint_steps_for_row(rows: list, index: int) -> int:
    """检查点列表点第 index 行 → 要 rewind 的步数。

    rows 按时间从旧到新；点第 index 行 = 恢复到那一轮（含）之后的所有
    写入之前，即弹出 index 之后（含 index）的全部行。
    """
    if not rows or not 0 <= index < len(rows):
        return 0
    return len(rows) - index


def checkpoint_cutoff_for_row(rows: list, index: int) -> str | None:
    """检查点列表点第 index 行 → 传给 rewind_to 的 cutoff（该行的 ts）。

    rewind_to(cutoff) 弹出所有 ts >= cutoff 的检查点并逐个恢复，
    文件回到该检查点备份的那一刻之前；与 checkpoint_steps_for_row
    是同一恢复动作的两种说法（步数版 / 时刻版）。
    """
    if not rows or not 0 <= index < len(rows):
        return None
    return rows[index].get("ts", "") or None


class ViewState:
    """四界面共用的视图状态机。

    只存“界面正在展示什么”，不存业务数据本身（消息历史、文件内容
    仍在各自的 SessionStore / ConversationMemory 里）。事件进来就
    更新，界面随时按它重渲染；重渲染是幂等的（同一份状态画两次
    是一样的）。
    """

    def __init__(self, model: str = "", theme: str = "dark"):
        self.model = model
        self.theme = theme
        self.mode = "chat"           # chat | plan
        self.busy = False
        self.session_id: str | None = None
        self.todos: list = []        # todo_set payload：[{content, status, acceptance}]
        self.plan: dict | None = None  # plan_proposed / plan_ready：{markdown, goal, steps, risks}
        self.diff_files: list = []   # diff_proposed：[{path, summary, lines}]
        self.checkpoints: list = []  # [{id, path, ts}]
        self.queued_count = 0
        self.error: str | None = None
        self.error_retryable = False
        # 用量快照：全部来自 API 真实 usage；unknown_calls > 0 说明有调用没返回用量。
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0,
                      "total_tokens": 0, "cost_cny": 0.0, "calls": 0,
                      "unknown_calls": 0, "context_pct": None}
        self.current_text = ""       # 本轮已流出的正文（text_delta 累积）
        self.thinking_text = ""      # 本轮思考流（thinking_delta 累积）
        self.turns = 0

    # -- 事件入口 ---------------------------------------------------------
    def apply_event(self, kind: str, payload: dict | None = None) -> None:
        """按 events.py 的事件表更新状态；未知 kind 直接报错（契约）。"""
        if kind not in EVENT_KINDS:
            raise ValueError(f"未知的事件种类：{kind}，见 EVENT_KINDS")
        payload = dict(payload or {})
        if kind == "text_delta":
            self.busy = True
            self.current_text += payload.get("text", "")
        elif kind == "thinking_delta":
            self.busy = True
            self.thinking_text += payload.get("text", "")
        elif kind == "tool_start":
            self.busy = True
        elif kind == "tool_end":
            pass
        elif kind == "todo_set":
            self.todos = list(payload.get("todos") or [])
        elif kind == "plan_proposed":
            self.plan = {"markdown": payload.get("markdown", ""),
                         "goal": payload.get("goal", ""),
                         "steps": list(payload.get("steps") or []),
                         "risks": list(payload.get("risks") or [])}
        elif kind == "diff_proposed":
            self.diff_files = [{"file": payload.get("file", ""),
                                "diff": payload.get("diff", "")}]
        elif kind == "approval_request":
            self.busy = True
        elif kind == "status":
            self.usage = {
                "prompt_tokens": payload.get("prompt_tokens") or 0,
                "completion_tokens": payload.get("completion_tokens") or 0,
                "total_tokens": payload.get("total_tokens") or 0,
                "cost_cny": payload.get("cost_cny") or 0.0,
                "calls": payload.get("calls") or 0,
                "unknown_calls": payload.get("unknown_calls") or 0,
                "context_pct": payload.get("context_pct"),
            }
        elif kind == "error":
            self.error = payload.get("message", "")
            self.error_retryable = bool(payload.get("retryable"))
            self.busy = False
        elif kind == "turn_end":
            self.busy = False
            self.turns += 1
            self.current_text = ""
            self.thinking_text = ""

    # -- 派生视图 ---------------------------------------------------------
    def todo_lines(self) -> list:
        """待办渲染行：☐/◐/☑ + 内容（四方同一份画法）。"""
        lines = []
        for i, item in enumerate(self.todos, 1):
            mark = TODO_MARKS.get(item.get("status", "pending"), "☐")
            line = f"{mark} {i}. {item.get('content', '')}"
            if item.get("acceptance"):
                line += f"（验收：{item['acceptance']}）"
            lines.append(line)
        return lines

    def usage_line(self) -> str:
        return format_usage(self.usage["total_tokens"],
                            self.usage["cost_cny"],
                            self.usage["unknown_calls"])

    def is_empty(self) -> bool:
        """空态判定：没有待办、没有计划、本轮没文字、没走过一轮。"""
        return not self.todos and self.plan is None \
            and not self.current_text and self.turns == 0
