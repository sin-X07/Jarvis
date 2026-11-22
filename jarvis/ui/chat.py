"""Jarvis CLI（P7 重写）：完整命令行入口。

三个界面（CLI / GUI / 网页）共用同一个 agent 核心，CLI 只是薄壳：
本文件只管"读输入、拼 system prompt、调 run_agent、打结果"，
不自己实现任何智能。

用法：
    python -m jarvis.ui.chat                 # 交互式 REPL
    python -m jarvis.ui.chat "帮我写个脚本"   # 一次问答（--print 效果：只打答案）
    python -m jarvis.ui.chat --print "问题"   # 同上，显式
    python -m jarvis.ui.chat --resume <会话id前缀>
    python -m jarvis.ui.chat --sessions      # 列出会话
    python -m jarvis.ui.chat --version

交互命令（以 / 开头）：/help /model /status /resume /fork /sessions
    /todo /plan /diff /rewind /clear /jobs /usage /remember /memory /forget
    /permission /quit
"""

import argparse
import sys

from jarvis.core.agent import MODEL, SYSTEM_PROMPT, build_client, run_agent
from jarvis.memory.checkpoint import (
    CheckpointStore, set_current_store, snapshot_if_active)
from jarvis.memory.compaction import maybe_compact, summarize_with_model
from jarvis.core.config import __version__, load_config
from jarvis.extensions.hooks import load_hooks, run_hooks
from jarvis.ui.md import to_ansi
from jarvis.ui.events import friendly_error, summarise_tool_call
from jarvis.memory.memory import ConversationMemory, FactStore, memory_tools
from jarvis.safety.permissions import ApprovalGate, cli_confirm
from jarvis.memory.planner import make_plan, verify_plan
from jarvis.tools.project import project_context_text
from jarvis.extensions.skills import skill_tools, skills_prompt_fragment
from jarvis.memory.session_store import SessionStore
from jarvis.runtime.subagent import subagent_tools
from jarvis.memory.todo import TodoList, attach_todo_tools, todos_path
from jarvis.safety.diffutil import render_diff_grouped
from jarvis.tools.tools import register_before_write_hook, working_tree_diff
from jarvis.ui.trajectory import render_trajectory
from jarvis.ui.update import check_update, format_update
from jarvis.core.usage import UsageTracker, daily_summary
from jarvis.runtime.jobs import JobManager, jobs_tools

HELP = """命令：
  /help              显示这份帮助
  /model [名字]      看当前模型 / 换模型（只改本轮）
  /status            模型、权限档位、token/费用、本轮上下文用量
  /resume [id]       恢复一个会话（不带 id 列出会话）
  /fork              把当前会话另存一份（继续聊，分叉）
  /sessions          列出全部会话
  /todo              看待办清单
  /plan <任务>       规划模式：先出计划，确认后再执行，结束时验验收标准
  /diff              显示工作区当前改动（按文件分组，含未跟踪文件）
  /rewind [n]        恢复最近 n 个文件检查点（默认 1）
  /clear             清空本轮对话历史（长期记忆不受影响）
  /jobs              列出后台任务
  /usage             最近 7 天用量小结
  /trajectory        看当前会话的轨迹（谁说了什么、调了什么工具）
  /remember <内容>   存一条长期记忆
  /memory            列出长期记忆
  /forget <内容>     删一条长期记忆（按内容匹配）
  /permission [档位] 看权限档位 / 切换 readonly|edit|auto
  /quit              退出（exit / quit 也行）
"""

SHORTCUTS = """快捷键（只列常用的，全部命令看 /help）：
  ?              显示这份快捷键总览
  /plan <任务>   先出计划卡片，批准后才执行
  /diff          显示当前改动（按文件分组）
  /rewind [n]    恢复最近 n 个文件检查点（默认 1）
  /resume [id]   恢复一个会话
  /fork          把当前会话另存一份
  /clear         清空本轮对话历史（长期记忆不受影响）
  Ctrl+C         退出
"""


def build_system_prompt(store: FactStore, query: str | None = None) -> str:
    """system prompt = 基础人设 + 长期记忆 + 项目规则 + 技能列表。

    每次调用前都重新拼一遍：本轮新记住的事实、项目里的 JARVIS.md
    改动，下一轮模型立刻就能看到。
    """
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


class CliSession:
    """一次 CLI 会话的状态：核心对象都攥在这里，命令函数只改它。"""

    def __init__(self, client, config, args, session_store=None, store=None):
        self.client = client
        self.config = config
        self.model = args.model or config.model
        self.max_steps = args.max_steps or config.max_steps
        self.max_budget = (args.max_budget_usd if args.max_budget_usd is not None
                           else config.max_budget_usd)
        self.store = store if store is not None else FactStore()
        self.memory = ConversationMemory(
            build_system_prompt(self.store))
        self.usage = UsageTracker()
        self.sessions = session_store if session_store is not None \
            else SessionStore()
        self.session_id = None
        self.jobs = JobManager()
        self.gate = ApprovalGate(
            args.permission or config.permission, confirm=cli_confirm)
        self.checkpoints = None
        self.hooks = load_hooks()
        self.use_color = sys.stdout.isatty()

        # 工具：记忆 + 技能 + 委派 + 后台任务 + 待办
        mem_schemas, mem_funcs = memory_tools(self.store)
        sk_schemas, sk_funcs = skill_tools()
        sub_schemas, sub_funcs = subagent_tools(client, self.model)
        job_schemas, job_funcs = jobs_tools(self.jobs)
        self.extra_tools = (mem_schemas + sk_schemas + sub_schemas
                            + job_schemas)
        self.extra_funcs = {**mem_funcs, **sk_funcs, **sub_funcs,
                            **job_funcs}
        self.todo = None
        self._set_todo(TodoList())

    def _set_todo(self, todo: TodoList) -> None:
        """换一张清单：挂 on_change（打清单 + 落盘），重挂 update_todo。

        U4-2：清单变动 → 打出整表（CLI 的 todo_set 渲染）并存到
        会话的 todos 文件；执行时模型每调一次 update_todo 就打一次。
        """
        self.todo = todo
        todo.on_change = self._on_todo_change
        attach_todo_tools(todo, self.extra_tools, self.extra_funcs)

    def _on_todo_change(self) -> None:
        print("\n待办：\n" + self.todo.render(), flush=True)
        if self.session_id:
            self.todo.save(todos_path(self.sessions.root, self.session_id))

    # -- 会话管理 -----------------------------------------------------
    def ensure_session(self, first_message: str) -> None:
        """第一条用户消息进来时建会话，并装上文件检查点。"""
        if self.session_id:
            return
        title = first_message[:24]
        self.session_id = self.sessions.create(title=title, model=self.model)
        self.checkpoints = CheckpointStore(self.session_id)
        set_current_store(self.checkpoints)
        # 写文件前自动备份（U5：之前只建库不注册钩子，备份从没跑过；
        # TUI 注册了但钩子返回 None 会把写文件拦掉，两个缺口一起补）
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
        register_before_write_hook(snapshot_if_active)
        self._set_todo(TodoList.load(
            todos_path(self.sessions.root, session_id)))
        return True

    # -- 一轮对话 -----------------------------------------------------
    def ask(self, text: str, stream: bool = True) -> str:
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
            print("（上下文太长，旧对话已压缩成摘要）")

        def _on_event(kind, payload):
            if kind == "text_delta" and stream:
                sys.stdout.write(payload["text"])
                sys.stdout.flush()
            elif kind == "tool_start":
                print(f"\n▸ {summarise_tool_call(payload['name'], payload['args'])}",
                      flush=True)
            elif kind == "tool_end":
                mark = "✓" if payload["status"] == "ok" else "✗"
                print(f"{mark} {payload['name']} 已返回"
                      f"（{payload['duration_ms']}ms）", flush=True)

        answer = run_agent(
            self.client, text, model=self.model, max_steps=self.max_steps,
            history=self.memory.messages, extra_tools=self.extra_tools,
            extra_funcs=self.extra_funcs, on_event=_on_event, stream=stream,
            usage=self.usage, max_budget_usd=self.max_budget,
            gate=self.gate, session_store=self.sessions,
            session_id=self.session_id, verbose=False,
        )
        self.memory.trim()
        return answer

    def usage_line(self) -> str:
        """本轮用量一行小结。"""
        return "本轮用量：" + self.usage.summary()


def print_sessions(store: SessionStore) -> None:
    sessions = store.list_sessions()
    if not sessions:
        print("还没有会话。")
        return
    for s in sessions:
        print(f"{s['id'][:12]}  {s.get('title') or '(无标题)'}  "
              f"{s.get('turns', 0)} 轮  {s.get('updated_at', '')}")


def print_usage(config) -> None:
    """打印最近 7 天用量小结 + 日预算提醒（/usage 和 --usage 共用）。"""
    rows = daily_summary(7)
    if not rows:
        print("最近 7 天还没有用量记录。")
    for row in rows:
        print(f"{row['date']}  {row['calls']} 次  {row['tokens']} tokens  "
              f"约 ¥{row['cost_cny']:.2f}")
    budget = config.daily_budget_usd
    if budget and rows:
        spent = rows[-1]["cost_cny"]
        limit = budget * 7.2
        if spent >= limit:
            print(f"提醒：今天已花约 ¥{spent:.2f}，超过日预算 "
                  f"${budget:.2f}（约 ¥{limit:.2f}）。")
        elif spent >= limit * 0.8:
            print(f"提醒：今天已花约 ¥{spent:.2f}，接近日预算 "
                  f"${budget:.2f}（约 ¥{limit:.2f}）。")


def find_session(store: SessionStore, prefix: str):
    """按 id 前缀找会话：唯一匹配返回 id，多/零匹配返回 None。"""
    matches = [s for s in store.list_sessions()
               if s["id"].startswith(prefix)]
    if len(matches) == 1:
        return matches[0]["id"]
    if len(matches) > 1:
        print(f"前缀 {prefix!r} 匹配到 {len(matches)} 个会话，请写长一点。")
    else:
        print(f"没有 id 以 {prefix!r} 开头的会话。")
    return None


def handle_command(session: CliSession, text: str) -> bool:
    """处理一条 / 命令。返回 False 表示退出 REPL。"""
    parts = text[1:].split(None, 1)
    cmd = parts[0] if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("quit", "退出", "exit"):
        return False
    if cmd == "help":
        print(HELP)
    elif cmd == "model":
        if arg:
            session.model = arg
            print(f"模型已换成 {arg}（只改本轮）。")
        else:
            print(f"当前模型：{session.model}")
    elif cmd == "status":
        from jarvis.memory.compaction import estimate_messages_tokens
        tokens = estimate_messages_tokens(session.memory.messages)
        print(f"模型：{session.model}")
        print(f"权限档位：{session.gate.mode}")
        print(f"上下文：约 {tokens} / {session.config.context_limit} tokens")
        print(session.usage_line())
        if session.session_id:
            print(f"会话：{session.session_id}")
    elif cmd == "sessions":
        print_sessions(session.sessions)
    elif cmd == "resume":
        if not arg:
            print_sessions(session.sessions)
            return True
        target = find_session(session.sessions, arg)
        if target and session.resume(target):
            print(f"已恢复会话 {target[:12]}。")
        else:
            print("恢复失败。")
    elif cmd == "fork":
        if not session.session_id:
            print("当前还没有会话，先聊一句再分叉。")
            return True
        new_id = session.sessions.fork(session.session_id)
        session.session_id = new_id
        session.checkpoints = CheckpointStore(new_id)
        set_current_store(session.checkpoints)
        print(f"已分叉到新会话 {new_id[:12]}。")
    elif cmd == "todo":
        print(session.todo.render())
    elif cmd == "plan":
        if not arg:
            print("用法：/plan <任务>，比如 /plan 给项目加一个登录页")
            return True
        run_plan(session, arg)
    elif cmd == "diff":
        # U5-3：工作区当前改动，按文件分组，含未跟踪文件
        print(render_diff_grouped(working_tree_diff()))
        print("接受改动 = 什么都不用做（改动已在工作区）；"
              "拒绝改动 = /rewind 回滚经 Jarvis 写入的文件。")
    elif cmd == "rewind":
        if not session.checkpoints:
            print("当前会话还没有检查点。")
            return True
        try:
            steps = int(arg) if arg else 1
        except ValueError:
            print("用法：/rewind [n]，n 是数字。")
            return True
        restored = session.checkpoints.rewind(steps)
        if restored:
            print("已恢复：")
            for path in restored:
                print(f"  {path}")
        else:
            print("没有可恢复的检查点。")
    elif cmd == "clear":
        session.memory.clear()
        print("本轮对话已清空，长期记忆还在。")
    elif cmd == "jobs":
        print(session.jobs.list_jobs())
    elif cmd == "usage":
        print_usage(session.config)
    elif cmd == "trajectory":
        if not session.session_id:
            print("当前还没有会话，先聊一句再看轨迹。")
        else:
            print(render_trajectory(session.session_id, session.sessions))
    elif cmd == "remember":
        if not arg:
            print("用法：/remember <内容>")
        else:
            print(session.store.add(arg))
    elif cmd == "memory":
        facts = session.store.all_texts()
        print("长期记忆：" if facts else "长期记忆还是空的。")
        for fact in facts:
            print(f"- {fact}")
    elif cmd == "forget":
        if not arg:
            print("用法：/forget <要删的内容>")
        else:
            print(session.store.delete(arg))
    elif cmd == "permission":
        if not arg:
            print(f"当前权限档位：{session.gate.mode}")
            print("可选：readonly（只读）| edit（写操作需确认）| auto（全放行）")
        elif arg in ("readonly", "edit", "auto"):
            session.gate.mode = arg
            print(f"权限档位已换成 {arg}。")
        else:
            print(f"没有 {arg!r} 这个档位，可选 readonly|edit|auto。")
    else:
        print(f"未知命令：{text}，输入 /help 看全部命令。")
    return True


def run_plan(session: CliSession, task: str) -> None:
    """规划模式：出计划 → 用户确认（批准/编辑/拒绝）→ 执行 → 验验收标准。

    U4-1：计划卡片就是下面打出来的这份（CLI 的 plan_proposed 渲染）；
    批准后清单才挂到会话上（todo_set），执行时每勾一步打一次清单。
    默认拒绝：直接回车 = 不执行。
    """
    def _plan_event(kind, payload):
        # 计划轮的工具事件也打出来：写工具被拦时用户当场看到
        if kind == "tool_start":
            print(f"\n▸ {summarise_tool_call(payload['name'], payload['args'])}",
                  flush=True)
        elif kind == "tool_end":
            mark = "✓" if payload["status"] == "ok" else "✗"
            print(f"{mark} {payload['name']} 已返回"
                  f"（{payload['duration_ms']}ms）", flush=True)

    print("正在调研并制定计划…")
    plan = make_plan(session.client, session.model, task,
                     extra_tools=session.extra_tools,
                     extra_funcs=session.extra_funcs,
                     max_steps=session.max_steps, on_event=_plan_event)
    while True:
        print()
        print("计划卡片：")
        print(plan.render())
        print()
        choice = input("批准执行？[y 批准 / e 编辑 / n 拒绝] ").strip().lower()
        if choice in ("y", "yes"):
            break
        if choice in ("n", "no", ""):
            print("已拒绝，计划没有执行。")
            return
        if choice in ("e", "edit"):
            note = input("怎么改（直接说，比如“第 2 步去掉”）：").strip()
            if not note:
                continue
            print("按修改意见重新规划…")
            plan = make_plan(session.client, session.model,
                             task + "\n修改意见：" + note,
                             extra_tools=session.extra_tools,
                             extra_funcs=session.extra_funcs,
                             max_steps=session.max_steps,
                             on_event=_plan_event)
            continue
        print("只认 y / e / n。")
    session._set_todo(plan.to_todos())
    print("\n待办：\n" + session.todo.render())
    prompt = (f"按顺序执行下面的计划，每完成一步调 update_todo 更新进度。"
              f"\n\n{plan.render()}\n\n任务：{task}")
    answer = session.ask(prompt)
    print()
    print(to_ansi(answer, use_color=session.use_color))
    print()
    print("正在对照验收标准检查…")
    report = verify_plan(session.client, session.model, plan, answer)
    print(report)


def repl(session: CliSession) -> None:
    """交互式 REPL 主循环。"""
    print("Jarvis 已启动（多轮对话 + 长期记忆 + 会话持久化）。")
    print("能记住事情、读写文件、跑命令。")
    print("输入 /help 看命令，? 看快捷键，/quit 退出。")
    if session.store.all_texts():
        print(f"已载入 {len(session.store.all_texts())} 条长期记忆。")
    while True:
        try:
            text = input("\n你：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break
        if not text:
            continue
        if text == "?":
            # 裸 ? 是快捷键总览，不发给模型（U6-2）
            print(SHORTCUTS)
            continue
        if text.startswith("/"):
            if not handle_command(session, text):
                break
            continue
        try:
            answer = session.ask(text)
        except Exception as exc:  # noqa: BLE001 - REPL 不能被一次报错打死
            hint = friendly_error(str(exc))
            print(f"\n✗ 出错了：{exc}"
                  + (f"（{hint}）" if hint else ""))
            print("直接再发一次可重试；重试一次还失败，换个说法或 /clear 开新会话。")
            continue
        print()
        print(to_ansi(answer, use_color=session.use_color))
        if session.config.daily_budget_usd:
            # 日预算只提醒、不拦：用量文件是按天汇总的，取今天那一行
            rows = daily_summary(1)
            if rows:
                spent = rows[-1]["cost_cny"]
                limit = session.config.daily_budget_usd * 7.2
                if spent >= limit:
                    print(f"提醒：今天已花约 ¥{spent:.2f}，已超过日预算 "
                          f"${session.config.daily_budget_usd:.2f}。")
                elif spent >= limit * 0.8:
                    print(f"提醒：今天已花约 ¥{spent:.2f}，接近日预算 "
                          f"${session.config.daily_budget_usd:.2f}。")
    print(session.usage_line())
    run_hooks("on_session_end", {"session": session.session_id or ""},
              hooks=session.hooks)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="jarvis", description="Jarvis 个人助手")
    parser.add_argument("prompt", nargs="*", help="直接问一个问题（一次问答）")
    parser.add_argument("--print", action="store_true",
                        help="一次问答模式：只打印答案")
    parser.add_argument("--resume", metavar="ID", help="恢复一个会话（id 前缀）")
    parser.add_argument("--sessions", action="store_true", help="列出全部会话")
    parser.add_argument("--model", help="本次用的模型")
    parser.add_argument("--max-steps", type=int, help="本轮最多工具调用步数")
    parser.add_argument("--max-budget-usd", type=float, help="本次费用上限（美元）")
    parser.add_argument("--permission", choices=["readonly", "edit", "auto"],
                        help="权限档位")
    parser.add_argument("--usage", action="store_true",
                        help="打印最近 7 天用量小结")
    parser.add_argument("--trajectory", metavar="ID",
                        help="打印一个会话的轨迹（id 前缀）")
    parser.add_argument("--update", action="store_true",
                        help="检查 GitHub 上有没有新版")
    parser.add_argument("--version", action="store_true", help="打印版本号")
    args = parser.parse_args(argv)

    if args.version:
        print(f"jarvis {__version__}")
        return
    config = load_config()
    if args.sessions:
        # 只列会话，不用调模型，所以不建 client（没 key 也能跑）
        print_sessions(SessionStore())
        return
    if args.usage:
        print_usage(config)
        return
    if args.update:
        print(format_update(check_update()))
        return
    if args.trajectory:
        target = find_session(SessionStore(), args.trajectory)
        if target:
            print(render_trajectory(target))
        return
    client = build_client()
    session = CliSession(client, config, args)

    if args.resume:
        target = find_session(session.sessions, args.resume)
        if target and session.resume(target):
            print(f"已恢复会话 {target[:12]}，继续聊吧。")
            repl(session)
        return
    if args.prompt or args.print:
        question = " ".join(args.prompt)
        if not question:
            print("用法：jarvis --print \"你的问题\"")
            return
        answer = session.ask(question, stream=not args.print)
        if args.print:
            print(to_ansi(answer, use_color=session.use_color))
        print(session.usage_line(), file=sys.stderr)
        return
    repl(session)


if __name__ == "__main__":
    main()
