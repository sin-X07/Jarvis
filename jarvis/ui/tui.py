"""Jarvis 全屏 TUI（U2，Textual）：命令行界面的成品形态。

布局（四层 + 侧栏，抄 DSH 社区 TUI 与 Codex TUI 的共识）：
    顶栏（模型芯片 · 权限档位芯片 · 会话标题）
    transcript（滚动：气泡 + 思考 disclosure + 工具卡片 + 确认卡片）
    composer（多行自增高输入框，/ 补全、@ 文件、! shell）
    状态栏（context 条 · 本轮真 token · 费用 · git 分支）
    侧栏（Ctrl+B 开关，Tab 页：会话 / 待办 / Diff / 检查点）

agent 循环跑在工作线程里，事件经 on_event 回到 UI 线程渲染；
确认（写文件/跑命令）在 transcript 里嵌卡片，单键 a/A/d/e 回答，
工作线程在 threading.Event 上等结果。
Stop（Esc）：从事件回调里抛 _StopRequested 掐断流式调用，
已出的文字保留并标记"■ 已停止"。

非 TTY 时（管道、重定向、CI）不启动 TUI，退化成纯文本流式输出；
--version / --sessions 等一次性命令也不进 TUI，直接交给 chat.main。

Run:  python -m jarvis.ui.tui
"""

import glob
from functools import partial
import json
import os
import subprocess
import sys
import threading
import time

from jarvis.core.agent import MODEL, SYSTEM_PROMPT, build_client, run_agent
from jarvis.core.config import (DEFAULT_CONTEXT_LIMIT, __version__,
                                default_config_dir, load_config, save_config,
                                save_key_to_dotenv)
from jarvis.core.usage import UsageTracker
from jarvis.memory.checkpoint import (CheckpointStore, set_current_store,
                                      snapshot_if_active)
from jarvis.memory.compaction import maybe_compact, summarize_with_model
from jarvis.memory.memory import ConversationMemory, FactStore, memory_tools
from jarvis.memory.planner import make_plan, parse_plan, verify_plan
from jarvis.memory.session_store import SessionStore
from jarvis.memory.todo import (TodoList, attach_todo_tools, todo_tools,
                                todos_path)
from jarvis.runtime.jobs import JobManager, jobs_tools
from jarvis.runtime.subagent import subagent_tools
from jarvis.safety.permissions import ApprovalGate
from jarvis.tools.project import project_context_text
from jarvis.tools.tools import run_shell, working_tree_diff
from jarvis.extensions.skills import skill_tools, skills_prompt_fragment
from jarvis.safety.diffutil import parse_diff, render_diff_grouped
from jarvis.ui.events import (friendly_error, summarise_tool_call,
                              tool_risk)
from jarvis.ui.theme import (load_theme, load_ui_prefs,
                           save_ui_prefs, to_textual_css)
from jarvis.ui.viewstate import (EMPTY_HINTS, EMPTY_TAGLINE,
                                 build_execute_prompt,
                                 checkpoint_cutoff_for_row, diff_note)

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen
    from textual.widgets import (Button, Collapsible, Input, Label, ListItem,
                                 ListView, Select, Static, TabbedContent,
                                 TabPane, TextArea)
except ImportError:  # pragma: no cover - 没装 textual 时给一句人话
    raise SystemExit("没装 textual：先 pip install -r requirements.txt")

# TUI 里实现的命令（补全弹窗的候选）。
COMMANDS = [
    ("help", "显示这份帮助"),
    ("model", "看当前模型 / 换模型"),
    ("status", "模型、档位、token/费用、上下文用量"),
    ("resume", "恢复一个会话（不带 id 列出会话）"),
    ("fork", "把当前会话另存一份"),
    ("sessions", "列出全部会话"),
    ("todo", "看待办清单"),
    ("plan", "规划模式：先出计划卡片，批准后才执行"),
    ("rewind", "恢复最近 n 个文件检查点（默认 1）"),
    ("clear", "清空本轮对话历史（长期记忆不受影响）"),
    ("jobs", "列出后台任务"),
    ("usage", "最近 7 天用量小结"),
    ("trajectory", "看当前会话的轨迹"),
    ("remember", "存一条长期记忆"),
    ("memory", "列出长期记忆"),
    ("forget", "删一条长期记忆"),
    ("permission", "看权限档位 / 切换 readonly|edit|auto"),
    ("setup", "首次运行向导（填 key、选模型、选档位），可重跑"),
    ("theme", "切换主题（dark / light）"),
    ("context", "用量明细：按调用拆开的 token/费用 + 优化建议"),
    ("diff", "显示当前改动（按文件分组，含未跟踪文件）"),
    ("regenerate", "重新生成上一条回答"),
    ("compact", "手动压缩上下文"),
    ("quit", "退出"),
]

HELP_TEXT = """快捷键（只列当前可用的）：
  Enter          发送 / 选中补全项
  Shift+Enter    输入框内换行
  Tab            焦点循环；忙时把追问排进队列
  Esc            忙时停止本轮；否则关闭弹窗
  Ctrl+B         开关侧栏
  Ctrl+O         展开/折叠最近一张工具卡片
  ?              这份总览
  End            回到底部
  a / A / d / e  确认卡片：允许一次 / 总是允许 / 拒绝 / 编辑
  a / e / r      计划卡片：批准执行 / 编辑 / 拒绝
  r              错误卡片：重试

命令（输入框里以 / 开头，Tab 补全）：
""" + "\n".join(f"  /{name:<12} {desc}" for name, desc in COMMANDS)


class _StopRequested(Exception):
    """用户按了停止：从事件回调里抛出，掐断流式调用。"""




def context_bar(pct: float | None, width: int = 10) -> str:
    """context 占比画成 [██████░░░░] 60% 这种文本条；没数时显示 —。"""
    if pct is None:
        return "[----------] —"
    filled = max(0, min(width, round(pct * width)))
    return f"[{'█' * filled}{'░' * (width - filled)}] {pct * 100:.0f}%"


def git_branch() -> str:
    """当前 git 分支（失败返回空串，状态栏直接不显示）。"""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5)
        branch = (proc.stdout or "").strip()
        return branch if proc.returncode == 0 and branch else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


# ---------------------------------------------------------------- widgets


class ComposerTextArea(TextArea):
    """多行输入框：Enter 发送、Shift+Enter 换行、Tab 忙时排队。

    Textual 的 TextArea 默认把 Enter 当换行；这里在 on_key 里先截住。
    补全弹窗打开时，Enter/上下键先交给弹窗选词。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.show_line_numbers = False

    def on_key(self, event) -> None:
        app = self.app
        if app.try_handle_global_key(event.key):
            event.prevent_default()
            event.stop()
            return
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            if app.popup_open() and not app.composer_is_complete_command():
                app.popup_select()
            elif app.busy:
                app.queue_current_input(steer=True)
            else:
                app.send_current_input()
        elif event.key == "shift+enter":
            event.prevent_default()
            event.stop()
            self.insert("\n")
            app.on_composer_changed()
        elif event.key == "tab" and app.busy:
            event.prevent_default()
            event.stop()
            app.queue_current_input(steer=False)
        elif event.key in ("up", "down") and app.popup_open():
            event.prevent_default()
            event.stop()
            app.popup_move(1 if event.key == "down" else -1)
        elif event.key == "escape" and app.popup_open():
            event.prevent_default()
            event.stop()
            app.popup_close()
        else:
            # 其余按键（打字、删字、光标移动）交给 TextArea 内部的
            # _on_key 处理；这里只跟进刷新补全弹窗与高度。
            app.on_composer_changed()

    def on_text_area_changed(self, event) -> None:
        # TextArea 内容变化（粘贴、删字）时也刷新补全弹窗与高度。
        self.app.on_composer_changed()


class ToolCard(Collapsible):
    """一张工具卡片：标题行是摘要 + 状态 + 耗时，点开看参数和输出。

    折叠是 Collapsible 自带的；Ctrl+O 展开/折叠最近一张。
    """

    def __init__(self, name: str, args: dict, risk: str):
        self.tool_name = name
        self._args_text = json.dumps(args, ensure_ascii=False, indent=2)
        self._body = Static(self._args_text, classes="tool-body")
        super().__init__(self._body, title="▸ …", collapsed=True)
        self.add_class("toolcard")
        self.add_class({"read": "risk-read", "write": "risk-write",
                        "danger": "risk-danger"}.get(risk, "risk-write"))

    def set_started(self, summary: str) -> None:
        self.title = f"▸ {summary}"
        self._status = "进行中…"

    def set_finished(self, status: str, duration_ms: int,
                     result_preview: str) -> None:
        mark = "✓" if status == "ok" else "✗"
        self.title = f"{mark} {self.title[2:]}"
        self._status = "完成" if status == "ok" else "失败"
        self._body.update(self._args_text + "\n── 输出 ──\n"
                          + (result_preview or "(空)"))
        self._duration = f"{duration_ms}ms"


class ThinkingBox(Collapsible):
    """思考 disclosure：默认折叠，思考流往里追加，不和正文混在一起。"""

    def __init__(self):
        self._body = Static("", classes="think-body")
        super().__init__(self._body, title="思考中…", collapsed=True)
        self.add_class("thinking")
        self._text = ""

    def append(self, text: str) -> None:
        self._text += text
        self._body.update(self._text)

    def finish(self) -> None:
        self.title = "思考过程（已折叠）"


class ConfirmCard(Static):
    """transcript 内嵌的确认卡片：显示确切命令和文件，等单键回答。

    工作线程在 self.event 上等结果；a/A/d/e 由 App 的 on_key 路由过来。
    A（总是允许）只记本会话：App 在 _always_allowed 里记 (kind, target)。
    """

    def __init__(self, action: dict):
        self.action = action
        self.event = threading.Event()
        self.result: bool | None = None
        kind = action.get("kind", "")
        target = action.get("target", "")
        detail = action.get("detail", "")
        danger = kind == "shell" or "rm " in str(target) or "删除" in str(detail)
        text = (f"[确认] {kind}：{target}\n"
                + (detail[:2000] if detail else "")
                + "\n[a] 允许一次  [A] 本会话总是允许  [d] 拒绝  [e] 编辑（拒绝并填入输入框）")
        self._text = text
        super().__init__(text, classes="confirm danger" if danger else "confirm")
        self._danger = danger

    def resolve(self, value: str) -> None:
        """value: once / always / deny / edit。"""
        if value == "once":
            self.result = True
        elif value == "always":
            self.result = True
            self.app.note_always_allowed(self.action)
        elif value == "deny":
            self.result = False
        elif value == "edit":
            self.result = False
            self.app.prefill_composer(str(self.action.get("detail") or
                                          self.action.get("target") or ""))
        self.update(self._text + f"\n→ 已选：{value}")
        self.event.set()


class ErrorCard(Static):
    """就地错误卡片 + 重试：保留已出文字，r 重发上一问。

    U6-4：重试只给一次。retry=False 时不给 r，改给 n（开新会话，
    把问题填回输入框再问一次——终端没有剪贴板，不走复制路线）。"""

    def __init__(self, message: str, retry: bool = True):
        self.retry = retry
        hint = ("[r] 重试  [Esc] 关闭" if retry
                else "[n] 开新会话再问一次  [Esc] 关闭")
        super().__init__(f"✗ 出错了：{message}\n{hint}",
                         classes="errorcard")


class PlanCard(Static):
    """计划卡片（U4-1）：目标 + 步骤 + 风险，等单键回答。

    工作线程在 self.event 上等结果；a/e/r 由 App 的 on_key 路由过来。
    result 是 approve / edit / reject；edit 时 plan 是编辑屏解析出的
    新计划，approve 时 plan 就是原计划。
    """

    def __init__(self, plan):
        self.plan = plan
        self.event = threading.Event()
        self.result: str | None = None
        text = ("[计划] " + (plan.goal or "") + "\n" + plan.render()
                + "\n[a] 批准执行  [e] 编辑  [r] 拒绝")
        self._text = text
        super().__init__(text, classes="confirm")

    def resolve(self, value: str, plan=None) -> None:
        if value == "approve" and plan is not None:
            self.plan = plan
        self.result = value
        mark = {"approve": "已批准", "edit": "已编辑",
                "reject": "已拒绝"}.get(value, value)
        self.update(self._text + f"\n→ {mark}")
        self.event.set()


class PlanEditScreen(ModalScreen):
    """编辑计划：把 plan.render() 放进 TextArea，改完保存即按此执行。"""

    def __init__(self, plan_text: str):
        super().__init__()
        self._plan_text = plan_text

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("编辑计划（改完保存即按此执行，Esc 取消回卡片）：",
                   id="plan-edit-title"),
            TextArea(self._plan_text, id="plan-edit-area"),
            Horizontal(
                Button("保存并执行", id="plan-edit-save"),
                Button("取消", id="plan-edit-cancel"),
            ),
            id="plan-edit-box",
        )

    def on_button_pressed(self, event) -> None:
        if event.button.id == "plan-edit-save":
            area = self.query_one("#plan-edit-area", TextArea)
            self.app.resolve_plan_edit(area.text)
        self.app.pop_screen()

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.app.pop_screen()
            event.prevent_default()
            event.stop()


class Divider(Static):
    """侧栏和 transcript 之间的可拖分隔条：按住拖动改侧栏宽。

    Textual 没有内置 Splitter，这里自己接鼠标事件：按下时捕获鼠标，
    移动时按横向位移改侧栏宽度（20–60 列），松开释放。
    """

    def __init__(self):
        super().__init__("│", id="divider")
        self._dragging = False
        self._last_x = 0

    def on_mouse_down(self, event) -> None:
        self._dragging = True
        self._last_x = event.screen_x
        self.capture_mouse()
        event.stop()

    def on_mouse_move(self, event) -> None:
        if not self._dragging:
            return
        dx = event.screen_x - self._last_x
        self._last_x = event.screen_x
        if dx:
            self.app.adjust_sidebar_width(-dx)
        event.stop()

    def on_mouse_up(self, event) -> None:
        self._dragging = False
        self.release_mouse()
        event.stop()


class HelpScreen(ModalScreen):
    """`?` 快捷键总览：只列当前可用的键。"""

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(HELP_TEXT, id="help-text"),
            Button("关闭 (Esc)", id="help-close"),
            id="help-box",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss()


class SetupScreen(ModalScreen):
    """`/setup` 首次运行向导：填 key、选模型、选档位，可重跑。

    key 写进项目 .env（DEEPSEEK_API_KEY 行）；模型和档位写
    ~/.jarvis/config.json。保存后 TUI 重建 client。
    """

    def __init__(self, app_ref, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._app = app_ref

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("首次运行向导（可重跑 /setup）", classes="modal-title"),
            Label("DeepSeek API key（只写进项目 .env，不上传）："),
            Input(placeholder="sk-…", password=True, id="setup-key"),
            Label("模型："),
            Input(value=self._app.session.model, id="setup-model"),
            Label("权限档位："),
            Select([("readonly（只读）", "readonly"),
                    ("edit（写操作需确认）", "edit"),
                    ("auto（全放行）", "auto")],
                   value=self._app.session.gate.mode, id="setup-permission"),
            Button("保存", id="setup-save"),
            Button("取消", id="setup-cancel"),
            id="setup-box",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "setup-cancel":
            self.dismiss()
            return
        key = self.query_one("#setup-key", Input).value.strip()
        model = self.query_one("#setup-model", Input).value.strip()
        permission = self.query_one("#setup-permission", Select).value
        self._app.apply_setup(key, model, permission)
        self.dismiss()

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss()


class ContextScreen(ModalScreen):
    """`/context` 用量可视化：按调用拆开的明细 + 优化建议（简版）。"""

    def __init__(self, text: str, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._text = text

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(self._text, id="context-text"),
            Button("关闭 (Esc)", id="context-close"),
            id="context-box",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss()


# ---------------------------------------------------------------- session


class TuiSession:
    """一次 TUI 会话的状态：和 chat.py 的 CliSession 同一套接线，
    只是确认回调与事件处理换成 TUI 版（卡片 + 线程事件），不打印。"""

    def __init__(self, client, config, session_store=None, store=None,
                 todo=None, confirm=None, on_todo_change=None):
        self.client = client
        self.config = config
        self.model = config.model
        self.max_steps = config.max_steps
        self.max_budget = config.max_budget_usd
        self.store = store if store is not None else FactStore()
        self.memory = ConversationMemory(
            build_system_prompt(self.store))
        self.usage = UsageTracker()
        self.sessions = session_store if session_store is not None \
            else SessionStore()
        self.session_id = None
        self.jobs = JobManager()
        self.gate = ApprovalGate(config.permission, confirm=confirm)
        self.checkpoints = None
        self.on_todo_change = on_todo_change

        mem_schemas, mem_funcs = memory_tools(self.store)
        sk_schemas, sk_funcs = skill_tools()
        sub_schemas, sub_funcs = subagent_tools(client, self.model)
        job_schemas, job_funcs = jobs_tools(self.jobs)
        self.extra_tools = (mem_schemas + sk_schemas + sub_schemas
                            + job_schemas)
        self.extra_funcs = {**mem_funcs, **sk_funcs, **sub_funcs,
                            **job_funcs}
        self.todo = None
        self._set_todo(todo if todo is not None else TodoList())

    def _set_todo(self, todo: TodoList) -> None:
        """换一张清单：挂 on_change（落盘 + 通知界面），重挂 update_todo。"""
        self.todo = todo
        todo.on_change = self._on_todo_change
        attach_todo_tools(todo, self.extra_tools, self.extra_funcs)

    def _on_todo_change(self) -> None:
        if self.session_id:
            self.todo.save(todos_path(self.sessions.root, self.session_id))
        if self.on_todo_change is not None:
            self.on_todo_change()

    def ensure_session(self, first_message: str) -> None:
        """第一条用户消息进来时建会话，并装上文件检查点。"""
        if self.session_id:
            return
        title = first_message[:24]
        self.session_id = self.sessions.create(title=title, model=self.model)
        self.checkpoints = CheckpointStore(self.session_id)
        set_current_store(self.checkpoints)
        # 写文件前自动备份（U2-6 检查点 tab 的数据来源；U5 再补 git 版）
        from jarvis.tools.tools import register_before_write_hook
        register_before_write_hook(snapshot_if_active)

    def resume(self, session_id: str) -> bool:
        """把一个已存会话恢复成当前会话（消息回填进内存）。"""
        messages = self.sessions.load_messages(session_id)
        if not messages and not self.sessions.load_events(session_id):
            return False
        self.session_id = session_id
        self.memory.messages = (
            [{"role": "system", "content": build_system_prompt(self.store)}]
            + [m for m in messages if m.get("role") != "system"])
        self.memory.trim()
        self.checkpoints = CheckpointStore(session_id)
        set_current_store(self.checkpoints)
        self._set_todo(TodoList.load(
            todos_path(self.sessions.root, session_id)))
        return True

    def ask(self, text: str, on_event) -> str:
        """跑一轮：拼 system prompt → 可能压缩 → 调 run_agent → 修剪。"""
        self.ensure_session(text)
        self.memory.messages[0] = {
            "role": "system",
            "content": build_system_prompt(self.store, query=text),
        }
        new_messages, did_compact, summary = maybe_compact(
            self.memory.messages,
            summarizer=lambda msgs: summarize_with_model(
                self.client, self.model, msgs),
            limit_tokens=self.config.context_limit)
        if did_compact:
            self.memory.messages = new_messages
            self.sessions.append_event(
                self.session_id, "summary", {"content": summary})
        answer = run_agent(
            self.client, text, model=self.model, max_steps=self.max_steps,
            history=self.memory.messages, extra_tools=self.extra_tools,
            extra_funcs=self.extra_funcs, on_event=on_event, stream=True,
            usage=self.usage, max_budget_usd=self.max_budget,
            gate=self.gate, session_store=self.sessions,
            session_id=self.session_id, verbose=False,
        )
        self.memory.trim()
        return answer


def build_system_prompt(store: FactStore, query: str | None = None) -> str:
    """system prompt = 基础人设 + 长期记忆 + 项目规则 + 技能列表。"""
    parts = [SYSTEM_PROMPT]
    facts = store.render_for_prompt(query)
    if facts:
        parts.append(facts)
    project = project_context_text(".")
    if project:
        parts.append(project)
    skills_fragment = skills_prompt_fragment()
    if skills_fragment:
        parts.append(skills_fragment)
    return "\n\n".join(parts)


SUGGESTIONS = [
    "记住，我最喜欢蓝色",
    "帮我写一个 Python 快速排序",
    "今天北京天气怎么样？",
    "总结一下这个项目是干什么的",
]


def _theme_css(theme_name: str | None = None) -> str:
    """主题变量 + 布局样式（全部用 $变量，不写死颜色）。

    theme_name 不给就读 ~/.jarvis/ui.json 里的偏好。
    """
    if theme_name is None:
        prefs = load_ui_prefs()
        theme_name = prefs.get("theme", "dark")
    try:
        theme = load_theme(theme_name)
    except Exception:  # noqa: BLE001 - 主题读坏了就回退 dark
        theme = load_theme("dark")
    return to_textual_css(theme) + """
Screen { background: $bg; color: $text; }
#topbar { height: 1; background: $surface; color: $accent; padding: 0 1; }
#middle { height: 1fr; }
#transcript { width: 1fr; padding: 0 1; }
#transcript:focus { border: none; }
#divider { width: 1; color: $border; }
#sidebar { width: 32; background: $surface; border-left: solid $border; }
#sidebar.hidden { display: none; }
#divider.hidden { display: none; }
#suggest { height: auto; max-height: 8; display: none;
           background: $surface; border: solid $border; }
#suggest.open { display: block; }
#queuebar { height: 1; color: $warning; display: none; padding: 0 1; }
#queuebar.show { display: block; }
#input { height: 3; border: solid $border; background: $surface; }
#input:focus { border: solid $accent; }
#statusbar { height: 1; background: $surface; color: $textMuted; padding: 0 1; }
#launchpad { align: center middle; height: 100%; }
#launchpad-title { color: $accent; text-align: center; }
#launchpad-hint { color: $textMuted; text-align: center; }
.sugg { margin: 0 1; }
.msg-user { background: $surfaceRaised; text-align: right; margin: 1 0; }
.msg-jarvis { background: $surface; margin: 1 0; }
.msg-shell { color: $textMuted; }
.toolcard { margin: 1 0; border: solid $border; }
.toolcard.risk-read { border-left: solid $info; }
.toolcard.risk-write { border-left: solid $warning; }
.toolcard.risk-danger { border-left: solid $danger; }
.thinking { margin: 1 0; color: $textMuted; border: dashed $border; }
.confirm { margin: 1 0; border: solid $warning; background: $surface; }
.confirm.danger { border: solid $danger; }
.errorcard { margin: 1 0; border: solid $danger; background: $surface; color: $danger; }
#back-to-bottom { display: none; width: 100%; background: $surface;
                  color: $accent; border: none; text-align: center; }
#back-to-bottom.show { display: block; }
#diff-btns { height: 3; }
#diff-btns Button { width: 50%; }
.modal-title { color: $accent; }
"""


# ---------------------------------------------------------------- app


class JarvisTUI(App):
    """全屏 TUI 应用。"""

    CSS = _theme_css()
    BINDINGS = [
        Binding("ctrl+b", "toggle_sidebar", "侧栏"),
        Binding("ctrl+o", "toggle_last_tool_card", "展开工具卡片"),
    ]

    def __init__(self, client=None, config=None, session_store=None,
                 store=None, todo=None):
        super().__init__()
        self.config = config or load_config()
        if client is None:
            try:
                client = build_client()
            except SystemExit:
                client = None  # 没 key 也能进界面，launchpad 会提示 /setup
        self.client = client
        self.session = TuiSession(
            client, self.config, session_store=session_store, store=store,
            todo=todo, confirm=self._confirm,
            on_todo_change=self._on_todo_change)
        self.busy = False
        self._stop_requested = False
        self._queue: list = []  # [(kind, text)] kind: ask 督导? 追问
        self._always_allowed: set = set()
        self._pending_confirm: ConfirmCard | None = None
        self._pending_plan: PlanCard | None = None
        self._pending_error: ErrorCard | None = None
        self._error_shown_this_turn = False
        self._error_streak = 0  # 连续出错次数（U6-4：重试只给一次）
        self._call_log: list = []  # 每次 status 事件的用量（/context 用）
        self._bubble: Static | None = None
        self._bubble_text = ""
        self._thinking: ThinkingBox | None = None
        self._tool_card: ToolCard | None = None
        self._last_tool_card: ToolCard | None = None
        self._last_user_text = ""
        self._follow = True
        self._folded = 0
        self._sidebar_auto_hidden = False
        self._popup_kind: str | None = None  # None | "cmd" | "file"
        self._popup_items: list = []
        self._popup_index = 0
        self._branch = ""
        self._branch_at = 0.0

    # -- 界面骨架 ------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            yield Static("", id="topbar")
            with Horizontal(id="middle"):
                yield VerticalScroll(id="transcript")
                yield Divider()
                with Vertical(id="sidebar"):
                    with TabbedContent(id="sidetabs"):
                        with TabPane("会话", id="tab-sessions"):
                            yield ListView(id="session-list")
                        with TabPane("待办", id="tab-todos"):
                            yield ListView(id="todo-list")
                        with TabPane("Diff", id="tab-diff"):
                            with Horizontal(id="diff-btns"):
                                yield Button("接受全部", id="diff-accept")
                                yield Button("拒绝全部", id="diff-reject")
                            with VerticalScroll(id="diff-scroll"):
                                yield Static("还没有改动。", id="diff-view")
                        with TabPane("检查点", id="tab-ckpt"):
                            yield ListView(id="ckpt-list")
            yield ListView(id="suggest")
            yield Button("↓ 回到底部 (End)", id="back-to-bottom")
            yield Static("", id="queuebar")
            yield ComposerTextArea(id="input")
            yield Static("", id="statusbar")

    def on_mount(self) -> None:
        self.title = "Jarvis"
        self._refresh_topbar()
        self._refresh_statusbar()
        self._refresh_sessions()
        self._refresh_todos()
        if self.size.width < 80:
            self._set_sidebar_visible(False)
            self._sidebar_auto_hidden = True
        else:
            self._sidebar_auto_hidden = False
        if not self.client:
            self._mount_launchpad(no_key=True)
        elif not self.session.sessions.list_sessions():
            self._mount_launchpad(no_key=False)
        self._refresh_ckpts()
        self._refresh_diff()
        self.set_interval(0.5, self._check_follow)
        self.query_one("#input", ComposerTextArea).focus()

    def on_resize(self) -> None:
        # 窄终端自动收起侧栏（只留一个提示），宽回来再恢复
        if self.size.width < 80 and not self._sidebar_auto_hidden:
            self._set_sidebar_visible(False)
            self._sidebar_auto_hidden = True
        elif self.size.width >= 80 and self._sidebar_auto_hidden:
            self._set_sidebar_visible(True)
            self._sidebar_auto_hidden = False

    # -- 顶栏 / 状态栏 --------------------------------------------------
    def _refresh_topbar(self) -> None:
        """顶栏：模型 · 档位 · 会话名。没挂载时（比如测试里直接调
        apply_setup）跳过，不崩。"""
        try:
            topbar = self.query_one("#topbar", Static)
        except Exception:  # noqa: BLE001 - 挂载前跳过
            return
        title = self.session.session_id or "新会话"
        if self.session.session_id:
            title = title[:12]
        topbar.update(f"Jarvis  ·  {self.session.model}  ·  "
                      f"[{self.session.gate.mode}]  ·  {title}")

    def _refresh_statusbar(self) -> None:
        try:
            statusbar = self.query_one("#statusbar", Static)
        except Exception:  # noqa: BLE001 - 挂载前跳过
            return
        usage = self.session.usage
        pct = usage.context_pct()
        if time.monotonic() - self._branch_at > 30:
            self._branch = git_branch()
            self._branch_at = time.monotonic()
        bar = context_bar(pct)
        statusbar.update(
            f"{self.session.model} · context {bar} · "
            f"本轮 {usage.total_tokens} tok · ¥{usage.cost_cny:.4f}"
            + (f" · {self._branch}" if self._branch else "")
            + (f" · 未返回用量 {usage.unknown_calls} 次" if usage.unknown_calls else ""))

    # -- transcript -----------------------------------------------------
    def _transcript(self) -> VerticalScroll:
        return self.query_one("#transcript", VerticalScroll)

    def _mount_to_transcript(self, widget) -> None:
        """往 transcript 里挂一个组件；没挂载时（测试里直接调命令）
        静默跳过，不崩。"""
        try:
            transcript = self._transcript()
        except Exception:  # noqa: BLE001 - 挂载前跳过
            return
        transcript.mount(widget)
        self._trim_transcript()
        if self._follow:
            transcript.scroll_end(animate=False)

    def _trim_transcript(self) -> None:
        """长 transcript 按需折叠：超过 400 个 widget 时，把最老的 50 个
        换成一条折叠提示（不是删掉——消息还在会话 JSONL 里，/trajectory 能看）。"""
        transcript = self._transcript()
        kids = [w for w in transcript.children
                if w.id != "launchpad" and w.id != "back-to-bottom"]
        if len(kids) <= 400:
            return
        drop = kids[:50]
        self._folded += len(drop)
        for w in drop:
            w.remove()
        old = transcript.query("#folded-note")
        if old:
            old.first().update(f"↑ 更早的 {self._folded} 条消息已折叠（/trajectory 查看完整轨迹）")
        else:
            transcript.mount(
                Static(f"↑ 更早的 {self._folded} 条消息已折叠（/trajectory 查看完整轨迹）",
                       id="folded-note"), before=0)

    def _mount_launchpad(self, no_key: bool) -> None:
        """空状态：居中建议问题 + 常用命令提示，不是空白屏。"""
        transcript = self._transcript()
        if transcript.query("#launchpad"):
            return
        box = Vertical(
            Static("Jarvis 已启动", id="launchpad-title"),
            Static(EMPTY_TAGLINE + " " + " ".join(EMPTY_HINTS),
                   id="launchpad-hint"),
            *([Static("没填 key 时先跑 /setup（填 key、选模型、选档位）。",
                      id="launchpad-hint")] if no_key else []),
            *[Button(text, classes="sugg", id=f"sugg-{i}")
              for i, text in enumerate(SUGGESTIONS)],
            id="launchpad",
        )
        transcript.mount(box)

    def _remove_launchpad(self) -> None:
        for box in self._transcript().query("#launchpad"):
            box.remove()

    # -- 发送 / 工作线程 -------------------------------------------------
    @property
    def _composer(self) -> ComposerTextArea:
        return self.query_one("#input", ComposerTextArea)

    def send_current_input(self) -> None:
        text = self._composer.text.strip()
        if text:
            self._composer.clear()
            self.on_composer_changed()
            self.send_text(text)

    def send_text(self, text: str) -> None:
        text = text.strip()
        if not text or self.busy:
            return
        if text.startswith("/"):
            self.handle_command(text)
            return
        if text.startswith("!"):
            self._run_shell_escape(text[1:])
            return
        if text != self._last_user_text:
            # 新问题：连续出错次数清零（重试是同一问题，不清）
            self._error_streak = 0
        self._last_user_text = text
        if self.client is None:
            try:
                self.client = build_client()
                self.session.client = self.client
            except SystemExit:
                self._show_error("没找到 DeepSeek API key，先跑 /setup 填 key。")
                return
        self.busy = True
        self._stop_requested = False
        self._error_shown_this_turn = False
        self._bubble = None
        self._bubble_text = ""
        self._thinking = None
        self._tool_card = None
        self._remove_launchpad()
        self._mount_to_transcript(
            Static(text, classes="msg-user"))
        self._refresh_topbar()
        self.run_worker(partial(self._ask_worker, text), thread=True)

    def _start_plan(self, task: str) -> None:
        """/plan：只读调研出计划 → 卡片等批准 → 批准后执行。"""
        if self.client is None:
            try:
                self.client = build_client()
                self.session.client = self.client
            except SystemExit:
                self._show_error("没找到 DeepSeek API key，先跑 /setup 填 key。")
                return
        if self.busy:
            self._note("先等这轮跑完，再 /plan。")
            return
        self.session.ensure_session(task)
        self.busy = True
        self._stop_requested = False
        self._remove_launchpad()
        self._mount_to_transcript(
            Static(f"📋 规划：{task}", classes="msg-user"))
        self._refresh_topbar()
        self.run_worker(partial(self._plan_worker, task), thread=True)

    def _plan_worker(self, task: str) -> None:
        """工作线程：出计划 → 等卡片回答 → 批准就执行（同一 worker 里跑完）。"""
        try:
            plan = make_plan(self.session.client, self.session.model, task,
                             extra_tools=self.session.extra_tools,
                             extra_funcs=self.session.extra_funcs,
                             max_steps=self.session.max_steps,
                             on_event=self._agent_event,
                             usage=self.session.usage)
        except Exception as exc:  # noqa: BLE001 - 报错进卡片，不崩界面
            self.call_from_thread(self._turn_done, None, str(exc))
            return
        card = PlanCard(plan)
        self.call_from_thread(self._show_plan_card, card)
        card.event.wait()
        if card.result == "approve":
            self.session._set_todo(card.plan.to_todos())
            self.call_from_thread(self._refresh_todos)
            prompt = build_execute_prompt(card.plan.render(), task)
            try:
                answer = self.session.ask(prompt, self._agent_event)
                # U7：执行完对照验收标准检查（以前 TUI 只 import 了
                # verify_plan，从没调过）。验收轮的 token 也进用量。
                try:
                    report = verify_plan(
                        self.session.client, self.session.model,
                        card.plan, answer, usage=self.session.usage)
                    self.call_from_thread(
                        self._note, "验收检查：\n" + report)
                except Exception as exc:  # noqa: BLE001 - 验收失败不拦主流程
                    self.call_from_thread(
                        self._note, "验收检查失败：" + str(exc))
                self.call_from_thread(self._turn_done, answer, None)
            except _StopRequested:
                self.call_from_thread(self._turn_done, None, "stopped")
            except Exception as exc:  # noqa: BLE001
                self.call_from_thread(self._turn_done, None, str(exc))
        else:
            self.call_from_thread(self._note, "已拒绝，计划没有执行。")
            self.call_from_thread(self._turn_done, None, None)

    def _show_plan_card(self, card: PlanCard) -> None:
        self._pending_plan = card
        self._mount_to_transcript(card)
        self._scroll_if_following()

    def resolve_plan_edit(self, text: str) -> None:
        """编辑屏保存：按改后的文本执行（解析不到步骤就按原计划）。"""
        if self._pending_plan is None:
            return
        card = self._pending_plan
        self._pending_plan = None
        plan = parse_plan(text)
        if not plan.steps:
            plan = card.plan
        card.resolve("approve", plan)

    def _ask_worker(self, text: str) -> None:
        try:
            answer = self.session.ask(text, self._agent_event)
            self.call_from_thread(self._turn_done, answer, None)
        except _StopRequested:
            self.call_from_thread(self._turn_done, None, "stopped")
        except Exception as exc:  # noqa: BLE001 - 报错进卡片，不崩界面
            self.call_from_thread(self._turn_done, None, str(exc))

    def _agent_event(self, kind, payload) -> None:
        """工作线程里的事件回调：Stop 在这里掐断，其余转发到 UI 线程。"""
        if self._stop_requested and kind in (
                "text_delta", "thinking_delta", "tool_start"):
            raise _StopRequested()
        self.call_from_thread(self._handle_event, kind, payload)

    def _turn_done(self, answer, error) -> None:
        self.busy = False
        if error == "stopped":
            if self._bubble is not None:
                self._bubble.update(self._bubble_text + "\n■ 已停止")
        elif error:
            if not self._error_shown_this_turn:
                self._show_error(error)
        else:
            self._error_streak = 0  # 本轮成功：连续出错清零
            if self._thinking is not None:
                self._thinking.finish()
            if self._bubble is not None and self._bubble_text.strip():
                from rich.markdown import Markdown
                self._bubble.update(Markdown(self._bubble_text))
        self._refresh_statusbar()
        self._refresh_sessions()
        self._refresh_todos()
        self._refresh_ckpts()
        self._refresh_topbar()
        if self._queue:
            _kind, text = self._queue.pop(0)
            self._refresh_queuebar()
            self.send_text(text)

    # -- 事件渲染 ---------------------------------------------------------
    def _handle_event(self, kind, payload) -> None:
        if kind == "text_delta":
            if self._bubble is None:
                self._bubble = Static("", classes="msg-jarvis")
                self._mount_to_transcript(self._bubble)
            self._bubble_text += payload["text"]
            self._bubble.update(self._bubble_text + " ▊")
            self._scroll_if_following()
        elif kind == "thinking_delta":
            if self._thinking is None:
                self._thinking = ThinkingBox()
                self._mount_to_transcript(self._thinking)
            self._thinking.append(payload["text"])
        elif kind == "tool_start":
            card = ToolCard(payload["name"], payload["args"],
                            tool_risk(payload["name"]))
            card.set_started(summarise_tool_call(
                payload["name"], payload["args"]))
            self._tool_card = card
            self._last_tool_card = card
            self._mount_to_transcript(card)
        elif kind == "tool_end":
            if self._tool_card is not None:
                self._tool_card.set_finished(
                    payload["status"], payload["duration_ms"],
                    str(payload["result"])[:500])
                self._tool_card = None
        elif kind == "status":
            self._call_log.append(dict(payload))
            self._refresh_statusbar()
        elif kind == "todo_set":
            self._refresh_todos()
        elif kind == "turn_end":
            pass  # 收尾在 _turn_done 里做（Markdown 渲染 + 状态栏）
        elif kind == "error":
            if self._stop_requested:
                return  # 停止时 run_agent 也会发 error 事件，忽略
            self._error_shown_this_turn = True
            self._show_error(payload["message"])

    def _show_error(self, message: str) -> None:
        """挂错误卡片：原文 + 一句恢复提示；连续出错只给一次重试。"""
        self._error_streak += 1
        hint = friendly_error(message)
        text = message + (f"（{hint}）" if hint else "")
        card = ErrorCard(text, retry=self._error_streak == 1)
        self._pending_error = card
        self._mount_to_transcript(card)

    def _new_session_reask(self) -> None:
        """U6-4：重试一次还失败——清空本轮（/clear 同款），把问题填回
        输入框，等用户按 Enter 再问一次（终端没有剪贴板）。"""
        self._error_streak = 0
        self.session.memory.clear()
        self._transcript().remove_children()
        self._folded = 0
        self._mount_launchpad(no_key=self.client is None)
        self._note("已开新会话，问题填回输入框了，按 Enter 再问一次。")
        if self._last_user_text:
            composer = self.query_one("#input", ComposerTextArea)
            composer.text = self._last_user_text
            composer.focus()

    def _check_follow(self) -> None:
        """每 0.5 秒看一次滚动位置：用户往上翻了就停跟随、露“回到底部”；
        回到底部了就恢复跟随。"""
        try:
            transcript = self._transcript()
            bar = self.query_one("#back-to-bottom", Button)
        except Exception:  # noqa: BLE001 - 挂载前跳过
            return
        at_bottom = transcript.scroll_y >= transcript.max_scroll_y - 2
        if at_bottom:
            self._follow = True
            bar.remove_class("show")
        else:
            self._follow = False
            bar.add_class("show")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back-to-bottom":
            self.action_scroll_to_bottom()
            return
        if event.button.id == "diff-accept":
            self._accept_diff()
            return
        if event.button.id == "diff-reject":
            self._reject_diff()
            return
        if event.button.id and event.button.id.startswith("sugg-"):
            idx = int(event.button.id.split("-")[1])
            self.send_text(SUGGESTIONS[idx])

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """侧栏列表回车选中：会话切换 / 检查点恢复。"""
        lst = event.list_view
        if lst.id == "session-list":
            item = lst.highlighted_child
            if item is not None and getattr(item, "data", None):
                self._switch_session(item.data)
        elif lst.id == "ckpt-list":
            self.restore_checkpoint_row(lst.index or 0)

    def _switch_session(self, session_id: str) -> None:
        if self.busy or session_id == self.session.session_id:
            return
        if self.session.resume(session_id):
            self._replay_transcript()
            self._refresh_topbar()
            self._refresh_sessions()
            self._refresh_ckpts()

    def _replay_transcript(self) -> None:
        """从 JSONL 日志重放 transcript（恢复会话 / 断线重连用）。"""
        from jarvis.ui.trajectory import replay_transcript
        transcript = self._transcript()
        transcript.remove_children()
        self._folded = 0
        for item in replay_transcript(self.session.session_id,
                                      self.session.sessions):
            kind = item["kind"]
            if kind == "user":
                transcript.mount(Static(item["content"], classes="msg-user"))
            elif kind == "assistant" and item.get("content"):
                transcript.mount(Static(item["content"], classes="msg-jarvis"))
            elif kind == "error":
                transcript.mount(ErrorCard(item["content"]))
        transcript.scroll_end(animate=False)

    def on_tabbed_content_tab_activated(
            self, event: TabbedContent.TabActivated) -> None:
        pane = event.pane
        if pane.id == "tab-diff":
            self._refresh_diff()
        elif pane.id == "tab-ckpt":
            self._refresh_ckpts()
        elif pane.id == "tab-todos":
            self._refresh_todos()
        elif pane.id == "tab-sessions":
            self._refresh_sessions()

    def _scroll_if_following(self) -> None:
        transcript = self._transcript()
        if self._follow:
            transcript.scroll_end(animate=False)

    # -- Stop / 排队 ------------------------------------------------------
    def request_stop(self) -> None:
        """Esc：掐断本轮（下一次事件回调里抛 _StopRequested）。"""
        if self.busy:
            self._stop_requested = True

    def queue_current_input(self, steer: bool) -> None:
        """忙时排队：Enter 督导当前轮次 / Tab 排队追问，队列可见。"""
        text = self._composer.text.strip()
        if not text:
            return
        kind = "steer" if steer else "ask"
        self._queue.append((kind, text))
        self._composer.clear()
        self.on_composer_changed()
        self._refresh_queuebar()

    def _refresh_queuebar(self) -> None:
        bar = self.query_one("#queuebar", Static)
        if not self._queue:
            bar.remove_class("show")
            bar.update("")
            return
        parts = [f"{i + 1}. {'督导' if k == 'steer' else '追问'}：{t[:30]}"
                 for i, (k, t) in enumerate(self._queue)]
        bar.update("排队中：" + " | ".join(parts))
        bar.add_class("show")

    # -- 确认 --------------------------------------------------------------
    def _confirm(self, action: dict) -> bool:
        """ApprovalGate 的 confirm 回调（工作线程里调）：先查本会话
        总是允许，再挂卡片等单键回答。"""
        key = (action.get("kind"), action.get("target"))
        if key in self._always_allowed:
            return True
        holder: dict = {}
        mounted = threading.Event()

        def _show():
            card = ConfirmCard(action)
            holder["card"] = card
            self._pending_confirm = card
            self._mount_to_transcript(card)
            mounted.set()

        self.call_from_thread(_show)
        mounted.wait(timeout=30)
        card = holder.get("card")
        if card is None:
            return False
        card.event.wait()
        self._pending_confirm = None
        return bool(card.result)

    def note_always_allowed(self, action: dict) -> None:
        self._always_allowed.add((action.get("kind"), action.get("target")))

    def prefill_composer(self, text: str) -> None:
        self.call_from_thread(self._prefill_composer_ui, text)

    def _prefill_composer_ui(self, text: str) -> None:
        self._composer.text = text
        self._composer.focus()

    # -- 侧栏 ---------------------------------------------------------------
    def _set_sidebar_visible(self, visible: bool) -> None:
        sidebar = self.query_one("#sidebar")
        divider = self.query_one("#divider")
        sidebar.set_class(not visible, "hidden")
        divider.set_class(not visible, "hidden")

    def action_toggle_sidebar(self) -> None:
        sidebar = self.query_one("#sidebar")
        visible = sidebar.has_class("hidden")
        self._set_sidebar_visible(visible)
        if visible:
            self._sidebar_auto_hidden = False

    def adjust_sidebar_width(self, dx: int) -> None:
        """分隔条拖动：侧栏宽 20–60 列。"""
        sidebar = self.query_one("#sidebar")
        cur = sidebar.styles.width
        cur_cells = cur.value if hasattr(cur, "value") else 32
        new = max(20, min(60, int(cur_cells) + dx))
        sidebar.styles.width = new

    def _refresh_sessions(self) -> None:
        try:
            lst = self.query_one("#session-list", ListView)
        except Exception:  # noqa: BLE001 - 挂载前刷新就跳过
            return
        lst.clear()
        for s in self.session.sessions.list_sessions():
            label = f"{(s.get('title') or '新会话')[:20]}  {s.get('turns', 0)} 轮"
            item = ListItem(Label(label))
            item.data = s["id"]
            lst.append(item)

    def _on_todo_change(self) -> None:
        """清单变了（U4-2）：工作线程里调过来时 hop 到 UI 线程再重渲染。"""
        import threading
        if threading.current_thread() is threading.main_thread():
            self._refresh_todos()
        else:
            self.call_from_thread(self._refresh_todos)

    def _refresh_todos(self) -> None:
        try:
            lst = self.query_one("#todo-list", ListView)
        except Exception:  # noqa: BLE001
            return
        lst.clear()
        marks = {"done": "☑", "active": "◐", "pending": "☐"}
        for item in self.session.todo.to_event():
            li = ListItem(Label(
                f"{marks[item['status']]} {item['content']}"))
            lst.append(li)
        if not self.session.todo.items:
            lst.append(ListItem(Label("(清单是空的，/plan 出计划后出现在这里)")))

    def _refresh_diff(self) -> None:
        """Diff 页：工作区当前改动按文件分组（U5-1），小节头 + +/- 行
        绿/红、@@ 行弱化。含未跟踪文件。"""
        try:
            view = self.query_one("#diff-view", Static)
        except Exception:  # noqa: BLE001
            return
        from rich.text import Text
        diff = working_tree_diff()
        if "不是一个 git 仓库" in diff:
            view.update("当前目录不是一个 git 仓库，看不到改动。")
            return
        files = parse_diff(diff)
        if not files:
            view.update("还没有改动。")
            return
        text = Text()
        for f in files:
            text.append(f"{f['path']}（{f['summary']}）\n", style="bold")
            for line in f["lines"]:
                if line.startswith("+") and not line.startswith("+++"):
                    text.append(line + "\n", style="green")
                elif line.startswith("-") and not line.startswith("---"):
                    text.append(line + "\n", style="red")
                elif line.startswith("@@"):
                    text.append(line + "\n", style="dim")
                else:
                    text.append(line + "\n")
        view.update(text)

    def _accept_diff(self) -> None:
        """接受全部：改动留在工作区，只记一条已审阅（U5-1）。"""
        self._note(diff_note(True))

    def _reject_diff(self) -> None:
        """拒绝全部：用检查点把经 Jarvis 写入的文件恢复到写入前。
        没经 Jarvis 写过的改动回滚不了，直说。"""
        ckpts = self.session.checkpoints
        if not ckpts or not ckpts.list_checkpoints():
            self._note("没有可回滚的检查点：拒绝全部只能回滚经 Jarvis "
                       "写入的改动，当前会话还没有这类改动。")
            return
        rows = ckpts.list_checkpoints()
        restored = ckpts.rewind(len(rows))
        if restored:
            self._note("已拒绝全部改动，恢复了："
                       + "、".join(os.path.basename(p) for p in restored))
        else:
            self._note("没有可恢复的文件。")
        self._refresh_diff()
        self._refresh_ckpts()

    def _refresh_ckpts(self) -> None:
        try:
            lst = self.query_one("#ckpt-list", ListView)
        except Exception:  # noqa: BLE001
            return
        lst.clear()
        ckpts = self.session.checkpoints
        if not ckpts:
            lst.append(ListItem(Label("(还没有检查点，写文件后自动备份)")))
            return
        for row in reversed(ckpts.list_checkpoints()):
            li = ListItem(Label(f"{row['ts'][11:19]}  {row['path'].split('/')[-1]}"))
            li.data = row
            lst.append(li)

    def restore_checkpoint_row(self, display_index: int) -> None:
        """回车恢复检查点：回到选中的那一条（只回滚文件，消息不动）。

        U7 修正：检查点列表是按时间倒序展示的（最新的在上面），
        display_index 是展示序号；先换算成时间正序的下标，再按该行
        的 ts 调 rewind_to（以前按展示序号直接算步数，选最新一行会
        把全部检查点都回滚掉）。
        """
        ckpts = self.session.checkpoints
        if not ckpts:
            return
        rows = ckpts.list_checkpoints()
        if not rows or not 0 <= display_index < len(rows):
            return
        chrono_index = len(rows) - 1 - display_index
        cutoff = checkpoint_cutoff_for_row(rows, chrono_index)
        if cutoff is None:
            return
        restored = ckpts.rewind_to(cutoff)
        note = (("已恢复检查点：" + "、".join(
            os.path.basename(p) for p in restored)) if restored
            else "没有可恢复的检查点。")
        self._mount_to_transcript(Static(note, classes="msg-shell"))
        self._refresh_ckpts()

    # -- 补全弹窗 -------------------------------------------------------------
    def popup_open(self) -> bool:
        return self._popup_kind is not None

    def on_composer_changed(self) -> None:
        """输入框内容变了：刷新补全弹窗与自增高。"""
        composer = self._composer
        text = composer.text
        # 自增高：1–6 行
        lines = max(1, text.count("\n") + 1)
        composer.styles.height = max(3, min(8, lines + 2))
        if text.startswith("/") and " " not in text.strip():
            query = text[1:].strip().lower()
            items = [(n, d) for n, d in COMMANDS
                     if n.startswith(query) or query in n]
            self._popup_show("cmd", items)
        elif "@" in text:
            tail = text.split("@")[-1].strip()
            if tail:
                items = self._file_matches(tail)
                self._popup_show("file", items)
            else:
                self._popup_close()
        else:
            self._popup_close()

    def _file_matches(self, query: str) -> list:
        from jarvis import PROJECT_ROOT
        try:
            names = glob.glob("**/*", root_dir=PROJECT_ROOT, recursive=True)
        except OSError:
            return []
        out = []
        for name in names:
            if name.startswith(".git/") or "/.git/" in name:
                continue
            if query.lower() in name.lower():
                out.append((name, ""))
            if len(out) >= 20:
                break
        return out

    def _popup_show(self, kind: str, items: list) -> None:
        self._popup_kind = kind
        self._popup_items = items
        self._popup_index = 0
        popup = self.query_one("#suggest", ListView)
        popup.clear()
        for name, desc in items[:8]:
            label = f"/{name}  {desc}" if kind == "cmd" else name
            popup.append(ListItem(Label(label)))
        popup.set_class(bool(items), "open")
        if items:
            popup.index = 0

    def _popup_close(self) -> None:
        self._popup_kind = None
        self._popup_items = []
        popup = self.query_one("#suggest", ListView)
        popup.clear()
        popup.remove_class("open")

    def popup_move(self, delta: int) -> None:
        if not self._popup_items:
            return
        self._popup_index = (self._popup_index + delta) % len(self._popup_items)
        popup = self.query_one("#suggest", ListView)
        popup.index = min(self._popup_index, len(self._popup_items) - 1)

    def composer_is_complete_command(self) -> bool:
        """输入框里的 /命令 是否已经是完整命令名：是的话回车直接执行，
        不用弹窗再补全一次（/help 打完按回车就该打开帮助）。"""
        text = self._composer.text.strip()
        if not text.startswith("/"):
            return False
        name = text[1:].split(None, 1)[0]
        return any(n == name for n, _ in COMMANDS)

    def popup_select(self) -> None:
        if not self._popup_items:
            self._popup_close()
            return
        name = self._popup_items[self._popup_index][0]
        composer = self._composer
        if self._popup_kind == "cmd":
            composer.text = f"/{name} "
        else:
            head, _, _tail = composer.text.rpartition("@")
            composer.text = head + "@" + name + " "
        self._popup_close()
        composer.focus()
        self.on_composer_changed()

    # -- 按键与动作 -------------------------------------------------------------
    def action_toggle_last_tool_card(self) -> None:
        if self._last_tool_card is not None:
            self._last_tool_card.collapsed = not self._last_tool_card.collapsed

    def action_show_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_scroll_to_bottom(self) -> None:
        self._follow = True
        self._transcript().scroll_end(animate=False)

    def try_handle_global_key(self, key: str) -> bool:
        """确认/重试这类全局单键：有待确认的卡片时接管按键（哪怕
        焦点在输入框里——卡片等回答时，输入框里敲 a 本来也发不出去）。"""
        if self._pending_confirm is not None:
            mapping = {"a": "once", "A": "always", "d": "deny", "e": "edit"}
            if key in mapping:
                self._pending_confirm.resolve(mapping[key])
                return True
        if self._pending_plan is not None:
            if key == "a":
                card = self._pending_plan
                self._pending_plan = None
                card.resolve("approve")
                return True
            if key == "e":
                self.push_screen(PlanEditScreen(
                    self._pending_plan.plan.render()))
                return True
            if key in ("r", "escape"):
                card = self._pending_plan
                self._pending_plan = None
                card.resolve("reject")
                return True
        if self._pending_error is not None:
            if key == "r" and self._pending_error.retry:
                card = self._pending_error
                self._pending_error = None
                card.remove()
                if self._last_user_text:
                    self.send_text(self._last_user_text)
                return True
            if key == "n" and not self._pending_error.retry:
                # 重试过一次还失败：开新会话，问题填回输入框（U6-4）
                card = self._pending_error
                self._pending_error = None
                card.remove()
                self._new_session_reask()
                return True
        return False

    def on_key(self, event) -> None:
        if self.try_handle_global_key(event.key):
            event.prevent_default()
            event.stop()
            return
        if event.key == "question_mark" and not isinstance(
                self.focused, ComposerTextArea):
            event.prevent_default()
            event.stop()
            self.action_show_help()
            return
        if event.key == "end" and not isinstance(
                self.focused, ComposerTextArea):
            event.prevent_default()
            event.stop()
            self.action_scroll_to_bottom()
            return
        if event.key == "escape":
            if self.busy:
                event.prevent_default()
                event.stop()
                self.request_stop()


    # -- / 命令 -------------------------------------------------------------
    def _note(self, text: str) -> None:
        self._mount_to_transcript(Static(text, classes="msg-shell"))

    def _find_session(self, prefix: str):
        matches = [s for s in self.session.sessions.list_sessions()
                   if s["id"].startswith(prefix)]
        if len(matches) == 1:
            return matches[0]["id"]
        if len(matches) > 1:
            self._note(f"前缀 {prefix!r} 匹配到 {len(matches)} 个会话，请写长一点。")
        else:
            self._note(f"没有 id 以 {prefix!r} 开头的会话。")
        return None

    def handle_command(self, text: str) -> None:
        parts = text[1:].split(None, 1)
        cmd = parts[0] if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("quit", "退出", "exit"):
            self.exit()
        elif cmd == "help":
            self.push_screen(HelpScreen())
        elif cmd == "model":
            if arg:
                self.session.model = arg
                self._refresh_topbar()
                self._note(f"模型已换成 {arg}（只改本轮）。")
            else:
                self._note(f"当前模型：{self.session.model}")
        elif cmd == "status":
            from jarvis.memory.compaction import estimate_messages_tokens
            tokens = estimate_messages_tokens(self.session.memory.messages)
            self._note(f"模型：{self.session.model}\n"
                       f"权限档位：{self.session.gate.mode}\n"
                       f"上下文：约 {tokens} / {self.config.context_limit} tokens\n"
                       f"{self.session.usage.summary()}"
                       + (f"\n会话：{self.session.session_id}"
                          if self.session.session_id else ""))
        elif cmd == "sessions":
            rows = self.session.sessions.list_sessions()
            if not rows:
                self._note("还没有会话。")
            else:
                self._note("\n".join(
                    f"{s['id'][:12]}  {s.get('title') or '(无标题)'}  "
                    f"{s.get('turns', 0)} 轮  {s.get('updated_at', '')}"
                    for s in rows))
        elif cmd == "resume":
            if not arg:
                self.handle_command("/sessions")
                return
            target = self._find_session(arg)
            if target and self.session.resume(target):
                self._replay_transcript()
                self._refresh_topbar()
                self._refresh_sessions()
                self._refresh_ckpts()
                self._note(f"已恢复会话 {target[:12]}。")
            else:
                self._note("恢复失败。")
        elif cmd == "fork":
            if not self.session.session_id:
                self._note("当前还没有会话，先聊一句再分叉。")
                return
            new_id = self.session.sessions.fork(self.session.session_id)
            self.session.session_id = new_id
            self.session.checkpoints = CheckpointStore(new_id)
            set_current_store(self.session.checkpoints)
            self._refresh_topbar()
            self._refresh_sessions()
            self._note(f"已分叉到新会话 {new_id[:12]}。")
        elif cmd == "todo":
            self._note(self.session.todo.render())
        elif cmd == "plan":
            if not arg:
                self._note("用法：/plan <任务>，比如 /plan 给项目加一个登录页")
                return
            self._start_plan(arg)
        elif cmd == "rewind":
            if not self.session.checkpoints:
                self._note("当前会话还没有检查点。")
                return
            try:
                steps = int(arg) if arg else 1
            except ValueError:
                self._note("用法：/rewind [n]，n 是数字。")
                return
            restored = self.session.checkpoints.rewind(steps)
            self._note("已恢复：\n" + "\n".join(f"  {p}" for p in restored)
                      if restored else "没有可恢复的检查点。")
            self._refresh_ckpts()
        elif cmd == "clear":
            self.session.memory.clear()
            self._transcript().remove_children()
            self._folded = 0
            self._mount_launchpad(no_key=self.client is None)
            self._note("本轮对话已清空，长期记忆还在。")
        elif cmd == "jobs":
            self._note(self.session.jobs.list_jobs())
        elif cmd == "usage":
            from jarvis.core.usage import daily_summary
            rows = daily_summary(7)
            if not rows:
                self._note("最近 7 天还没有用量记录。")
            else:
                self._note("\n".join(
                    f"{r['date']}  {r['calls']} 次  {r['tokens']} tokens  "
                    f"约 ¥{r['cost_cny']:.2f}" for r in rows))
        elif cmd == "trajectory":
            if not self.session.session_id:
                self._note("当前还没有会话，先聊一句再看轨迹。")
            else:
                from jarvis.ui.trajectory import render_trajectory
                self._note(render_trajectory(
                    self.session.session_id, self.session.sessions))
        elif cmd == "remember":
            if not arg:
                self._note("用法：/remember <内容>")
            else:
                self._note(self.session.store.add(arg))
        elif cmd == "memory":
            facts = self.session.store.all_texts()
            self._note("长期记忆：\n" + "\n".join(f"- {f}" for f in facts)
                      if facts else "长期记忆还是空的。")
        elif cmd == "forget":
            if not arg:
                self._note("用法：/forget <要删的内容>")
            else:
                self._note(self.session.store.delete(arg))
        elif cmd == "permission":
            if not arg:
                self._note(f"当前权限档位：{self.session.gate.mode}\n"
                           "可选：readonly（只读）| edit（写操作需确认）| auto（全放行）")
            elif arg in ("readonly", "edit", "auto"):
                self.session.gate.mode = arg
                self.config.permission = arg
                save_config(self.config)
                self._refresh_topbar()
                self._note(f"权限档位已换成 {arg}。")
            else:
                self._note(f"没有 {arg!r} 这个档位，可选 readonly|edit|auto。")
        elif cmd == "setup":
            self.push_screen(SetupScreen(self))
        elif cmd == "theme":
            self._cycle_theme()
        elif cmd == "context":
            self.push_screen(ContextScreen(self._context_text()))
        elif cmd == "diff":
            self._note(render_diff_grouped(working_tree_diff())
                       + "\n接受改动 = 什么都不用做；拒绝改动 = "
                         "Diff 页点“拒绝全部”。")
        elif cmd == "regenerate":
            self._regenerate()
        elif cmd == "compact":
            self._run_compact()
        else:
            self._note(f"未知命令：{text}，输入 /help 看全部命令。")

    def _regenerate(self) -> None:
        """U7：重新生成上一条回答（GUI/网页已有，TUI 补齐）。

        删掉内存里本轮 trailing 的 assistant/tool 消息（与 GUI 的
        _on_regenerate 同款做法），再用上一条用户消息重问一次；
        不重复挂用户气泡（问题已经在 transcript 里）。
        """
        if self.busy:
            self._note("先等这轮跑完，再重新生成。")
            return
        if not self._last_user_text:
            self._note("还没有可重新生成的问题，先问一句。")
            return
        if self.client is None:
            try:
                self.client = build_client()
                self.session.client = self.client
            except SystemExit:
                self._show_error("没找到 DeepSeek API key，先跑 /setup 填 key。")
                return
        msgs = self.session.memory.messages
        while msgs and msgs[-1].get("role") in ("assistant", "tool"):
            msgs.pop()
        self.busy = True
        self._stop_requested = False
        self._error_shown_this_turn = False
        self._bubble = None
        self._bubble_text = ""
        self._thinking = None
        self._tool_card = None
        self._refresh_topbar()
        self.run_worker(partial(self._ask_worker, self._last_user_text),
                        thread=True)

    def _context_text(self) -> str:
        """`/context`：按调用拆开的用量明细 + 优化建议。"""
        usage = self.session.usage
        pct = usage.context_pct()
        lines = ["用量明细（每次模型调用一行，全是 API 返回的真数）："]
        if not self._call_log:
            lines.append("（本轮还没有调用）")
        for i, row in enumerate(self._call_log, 1):
            pt = row.get("prompt_tokens")
            ct = row.get("completion_tokens")
            lines.append(f"  {i}. {row.get('model', '')}  "
                         f"输入 {pt if pt is not None else '—'} / "
                         f"输出 {ct if ct is not None else '—'}  "
                         f"¥{row.get('cost_cny', 0):.4f}")
        lines.append(f"合计：{usage.total_tokens} tok，约 ¥{usage.cost_cny:.4f}")
        lines.append(f"上下文：{context_bar(pct)}（窗口 "
                     f"{self.config.context_limit // 1000}k）")
        if pct is not None and pct >= 0.8:
            lines.append("建议：上下文快满了，跑 /compact 压缩一下。")
        elif pct is not None and pct >= 0.5:
            lines.append("建议：上下文过半了，长对话可适时 /compact。")
        else:
            lines.append("建议：上下文还宽裕，不用压缩。")
        return "\n".join(lines)

    def _run_compact(self) -> None:
        """手动压缩上下文（/compact）。"""
        new_messages, did_compact, summary = maybe_compact(
            self.session.memory.messages,
            summarizer=lambda msgs: summarize_with_model(
                self.client, self.model, msgs),
            limit_tokens=self.config.context_limit)
        if did_compact:
            self.session.memory.messages = new_messages
            if self.session.session_id:
                self.session.sessions.append_event(
                    self.session.session_id, "summary", {"content": summary})
            self._note("已压缩：旧对话浓缩成摘要，见轨迹。")
        else:
            self._note("上下文还不挤，不用压缩。")

    def _cycle_theme(self) -> None:
        """`/theme`：在已有的主题文件里切换（dark/light），并记住选择。"""
        import glob as _glob
        here = os.path.dirname(os.path.abspath(__file__))
        names = []
        for path in sorted(_glob.glob(os.path.join(here, "*.json"))):
            name = os.path.basename(path)[:-5]
            if name == "theme":
                name = "dark"
            try:
                with open(path, encoding="utf-8") as f:
                    if "color" in json.load(f):
                        names.append(name)
            except (OSError, json.JSONDecodeError):
                continue
        if not names:
            names = ["dark"]
        prefs = load_ui_prefs()
        cur = prefs.get("theme", "dark")
        nxt = names[(names.index(cur) + 1) % len(names)] if cur in names \
            else names[0]
        if nxt == cur:
            self._note(f"只有一个主题：{cur}。")
            return
        self._apply_theme(nxt)
        prefs["theme"] = nxt
        save_ui_prefs(prefs)
        self._note(f"主题已换成 {nxt}（已记住，下次启动还用它）。")

    def _apply_theme(self, name: str) -> None:
        """换主题：整份主题 CSS 重贴到同一个源上，再 refresh_css 重算样式。

        为什么不是 set_variables：Textual 的 $变量声明只在同源解析时
        生效（`$bg:` 写在 App.CSS 源里，`app.stylesheet._variables`
        里根本没有它）；`stylesheet.set_variables` 是全量替换——会抹
        掉 Textual 内置变量（`$primary` / `$background`…），且已算好
        的样式不会因此重算。实测过：只有 add_source(整份新 CSS) +
        refresh_css 才能真的换色（见 /tmp/min_theme3.py）。
        """
        css = _theme_css(name)
        for loc, src in self.stylesheet.source.items():
            if loc[1] == "JarvisTUI.CSS":
                self.stylesheet.add_source(
                    css, read_from=loc, is_default_css=src.is_defaults,
                    tie_breaker=src.tie_breaker, scope=src.scope)
                break
        else:  # 兜底：源找不到了就当新源贴上
            self.stylesheet.add_source(css)
        self.refresh_css()

    def apply_setup(self, key: str, model: str, permission: str,
                    dotenv_path: str | None = None) -> None:
        """向导保存：key 进项目 .env，模型和档位进 ~/.jarvis/config.json。

        dotenv_path 只供测试传临时路径；默认写项目根目录的 .env。
        """
        notes = []
        if key:
            from jarvis import PROJECT_ROOT
            path = dotenv_path or os.path.join(PROJECT_ROOT, ".env")
            save_key_to_dotenv(key, path)
            os.environ["DEEPSEEK_API_KEY"] = key
            try:
                self.client = build_client()
                self.session.client = self.client
                notes.append("key 已保存到项目 .env，client 已重建")
            except SystemExit as exc:
                notes.append(f"key 已保存，但建 client 失败：{exc}")
        if model:
            self.session.model = model
            self.config.model = model
            save_config(self.config)
            notes.append(f"模型已换成 {model}")
        if permission in ("readonly", "edit", "auto"):
            self.session.gate.mode = permission
            self.config.permission = permission
            save_config(self.config)
            notes.append(f"权限档位已换成 {permission}")
        self._refresh_topbar()
        self._refresh_statusbar()
        self._note("设置已保存：" + "；".join(notes) if notes else "什么都没改。")

    # -- ! shell 转义 --------------------------------------------------------
    def _run_shell_escape(self, command: str) -> None:
        """`!` 开头的输入：跑 shell，只显示输出，不发给模型。"""
        command = command.strip()
        if not command:
            return
        self._mount_to_transcript(Static(f"❯ {command}", classes="msg-shell"))

        def _run():
            try:
                out = run_shell(command)
            except Exception as exc:  # noqa: BLE001 - 输出进 transcript
                out = f"执行出错：{exc}"
            self.call_from_thread(
                self._mount_to_transcript,
                Static(out[:4000], classes="msg-shell"))

        self.run_worker(_run, thread=True)


# ---------------------------------------------------------------- entry


_ONE_SHOT = {"--version", "--sessions", "--usage", "--trajectory", "--update",
             "--print", "--resume", "--help", "-h"}


def main(argv=None) -> None:
    """入口：TTY 下启动全屏 TUI；非 TTY / 一次性命令交给 chat.main。

    `python -m jarvis.ui.tui`（交互终端）→ TUI；
    管道、重定向、CI 里 → 纯文本流式输出（codex exec 式）；
    `python -m jarvis.ui.tui --version` / `"问题"` → chat.main 的一次性命令。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    from jarvis.ui import chat as chat_mod
    if (not sys.stdin.isatty() or not sys.stdout.isatty()
            or any(a in _ONE_SHOT for a in args)
            or (args and not args[0].startswith("-"))):
        chat_mod.main(argv)
        return
    app = JarvisTUI()
    app.run()


if __name__ == "__main__":
    main()
