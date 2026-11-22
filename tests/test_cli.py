"""P7 测试：Markdown 渲染 + CLI 命令（不用 key、不联网）。

验证：
  1. md.to_html：代码块、粗体、行内代码、标题、列表
  2. md.to_ansi：粗体变 ANSI，use_color=False 时原样返回
  3. CLI：--version、--sessions、未知命令、/remember /memory /forget、
     /model /permission /status、/todo、/sessions、/resume 恢复会话
"""

import io
import os
import sys
import tempfile
from contextlib import redirect_stdout
from types import SimpleNamespace

from jarvis.ui import chat
from jarvis.ui.chat import CliSession, handle_command, main
from jarvis.core.config import load_config
from jarvis.ui.md import to_ansi, to_html
from jarvis.memory.memory import FactStore
from jarvis.memory.session_store import SessionStore


def _args(**overrides):
    base = {"model": None, "max_steps": None, "max_budget_usd": None,
            "permission": None}
    base.update(overrides)
    return SimpleNamespace(**base)


def _session(tmp):
    store = FactStore(path=os.path.join(tmp, "memory.json"))
    sessions = SessionStore(root=os.path.join(tmp, "sessions"))
    return CliSession(object(), load_config(), _args(),
                      session_store=sessions, store=store)


def test_md_html():
    html = to_html("# 标题\n\n一段 **粗体** 和 `代码`。\n\n- 一\n- 二\n")
    assert "<h3>标题</h3>" in html, html
    assert "<b>粗体</b>" in html, html
    assert "<code>代码</code>" in html, html
    assert "<ul><li>一</li><li>二</li></ul>" in html, html
    html = to_html("```python\nprint(1)\n```")
    assert "<pre><code" in html and "print(1)" in html, html
    html = to_html("<script>alert(1)</script>")
    assert "<script>" not in html and "&lt;script&gt;" in html, html
    print("[OK] Markdown→HTML：标题、粗体、代码、列表、转义都正常")


def test_md_ansi():
    out = to_ansi("一段 **粗体** 和 `代码`。")
    assert "\x1b[1m粗体\x1b[0m" in out, repr(out)
    assert "\x1b[7m代码\x1b[0m" in out, repr(out)
    assert to_ansi("**粗体**", use_color=False) == "**粗体**"
    assert to_ansi("# 标题").startswith("\x1b[1m")
    print("[OK] Markdown→ANSI：粗体/代码变样式，非 tty 时原样返回")


def test_cli_version():
    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["--version"])
    assert buf.getvalue().strip().startswith("jarvis "), buf.getvalue()
    print("[OK] CLI：--version 打印版本号")


def test_cli_sessions_empty():
    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["--sessions"])
    out = buf.getvalue()
    # 列表读的是真实 ~/.jarvis/sessions：为空打印“还没有会话。”，
    # 有会话时每行一条“id 标题 N 轮 时间”，行数等于会话数。
    # 注意：print_sessions 有会话时不打印标题行，所以不能断言“会话”二字。
    rows = SessionStore().list_sessions()
    if not rows:
        assert "还没有会话" in out, out
    else:
        assert len(out.strip().splitlines()) == len(rows), out
    print("[OK] CLI：--sessions 能列出会话（不崩）")


def test_commands_memory():
    tmp = tempfile.mkdtemp()
    session = _session(tmp)
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert handle_command(session, "/remember 我最喜欢蓝色") is True
        assert handle_command(session, "/memory") is True
        assert handle_command(session, "/forget 我最喜欢蓝色") is True
        assert handle_command(session, "/memory") is True
    out = buf.getvalue()
    assert "我最喜欢蓝色" in out, out
    assert "已忘记" in out, out
    assert session.store.all_texts() == []
    print("[OK] CLI：/remember /memory /forget 走通")


def test_commands_misc():
    tmp = tempfile.mkdtemp()
    session = _session(tmp)
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert handle_command(session, "/help") is True
        assert handle_command(session, "/model") is True
        assert handle_command(session, "/model deepseek-chat") is True
        assert session.model == "deepseek-chat"
        assert handle_command(session, "/permission") is True
        assert handle_command(session, "/permission readonly") is True
        assert session.gate.mode == "readonly"
        assert handle_command(session, "/permission 胡说") is True
        assert session.gate.mode == "readonly"
        assert handle_command(session, "/status") is True
        assert handle_command(session, "/todo") is True
        assert handle_command(session, "/sessions") is True
        assert handle_command(session, "/jobs") is True
        assert handle_command(session, "/usage") is True
        assert handle_command(session, "/不存在的命令") is True
        assert handle_command(session, "/quit") is False
    out = buf.getvalue()
    assert "当前模型" in out and "deepseek-chat" in out, out
    assert "readonly" in out, out
    assert "未知命令" in out, out
    assert "清单是空的" in out, out
    print("[OK] CLI：/help /model /permission /status /todo /jobs /usage /quit 都正常")


def test_cli_diff_and_hook():
    """U5-2/U5-3：/diff 按文件分组（含未跟踪文件）；建会话即注册写前
    钩子，写文件成功且自动备份（之前只建库不注册，备份从没跑过）。"""
    from test_diffutil import GIT_DIFF
    from jarvis.memory.checkpoint import current_store, set_current_store
    from jarvis.tools import tools

    tmp = tempfile.mkdtemp()
    session = _session(tmp)
    old_diff = chat.working_tree_diff
    old_hooks = list(tools._BEFORE_WRITE_HOOKS)
    old_store = current_store()
    old_dir = tools.PROJECT_DIR
    try:
        chat.working_tree_diff = lambda: GIT_DIFF
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert handle_command(session, "/diff") is True
        out = buf.getvalue()
        assert "=== a.py（+1 行 / -1 行）===" in out, out
        assert "=== b.py（+2 行 / -0 行）===" in out, out
        tools.PROJECT_DIR = tmp
        session.ensure_session("写个文件")
        assert tools.write_text_file("a.txt", "hello").startswith("已写入")
        # 新文件没有旧内容可备份；第二次写才产生检查点
        assert len(session.checkpoints.list_checkpoints()) == 0
        assert tools.write_text_file("a.txt", "hello2").startswith("已写入")
        assert len(session.checkpoints.list_checkpoints()) == 1
    finally:
        chat.working_tree_diff = old_diff
        tools._BEFORE_WRITE_HOOKS[:] = old_hooks
        set_current_store(old_store)
        tools.PROJECT_DIR = old_dir
    print("[OK] CLI：/diff 按文件分组；建会话即注册写前钩子，写文件自动备份")


def test_resume_session():
    tmp = tempfile.mkdtemp()
    session = _session(tmp)
    sid = session.sessions.create(title="测试会话")
    session.sessions.append_event(sid, "user", {"content": "你好"})
    session.sessions.append_event(sid, "assistant", {"content": "你好呀"})
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert handle_command(session, f"/resume {sid[:8]}") is True
    assert session.session_id == sid
    roles = [m["role"] for m in session.memory.messages]
    assert roles == ["system", "user", "assistant"], roles
    assert "已恢复会话" in buf.getvalue(), buf.getvalue()
    print("[OK] CLI：/resume 按 id 前缀恢复会话，消息回填进内存")


def test_build_system_prompt_includes_skills():
    tmp = tempfile.mkdtemp()
    store = FactStore(path=os.path.join(tmp, "memory.json"))
    prompt = chat.build_system_prompt(store)
    assert "code-review" in prompt, prompt
    print("[OK] CLI：system prompt 里拼进了技能列表")


def test_cli_question_mark_shows_shortcuts():
    """U6-2：REPL 里裸 ? 显示快捷键总览，不发给模型。"""
    import builtins
    from unittest.mock import patch
    from jarvis.ui.chat import repl

    with tempfile.TemporaryDirectory() as tmp:
        session = _session(tmp)
        calls = []
        session.ask = lambda text: calls.append(text) or "不该被调到"
        buf = io.StringIO()
        with patch.object(builtins, "input", side_effect=["?", "/quit"]), \
                redirect_stdout(buf):
            repl(session)
        out = buf.getvalue()
    assert "快捷键" in out, out
    assert "/plan" in out and "先出计划卡片" in out, out
    assert calls == [], "裸 ? 不该发给模型"
    print("[OK] CLI：裸 ? 显示快捷键总览，不发给模型")


def test_cli_repl_error_does_not_crash():
    """U6-4：REPL 里一轮报错不打死循环，带恢复提示，再发一次能继续。"""
    import builtins
    from unittest.mock import patch
    from jarvis.ui.chat import repl

    with tempfile.TemporaryDirectory() as tmp:
        session = _session(tmp)
        state = {"n": 0}

        def _ask(text):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("RateLimit 429 too many requests")
            return "好的"

        session.ask = _ask
        buf = io.StringIO()
        with patch.object(builtins, "input",
                          side_effect=["你好", "你好", "/quit"]), \
                redirect_stdout(buf):
            repl(session)
        out = buf.getvalue()
    assert "出错了" in out and "请求太频繁" in out, out
    assert "直接再发一次可重试" in out, out
    assert "好的" in out, out
    print("[OK] CLI：REPL 报错不崩，带恢复提示，再发一次能继续")


if __name__ == "__main__":
    test_md_html()
    test_md_ansi()
    test_cli_version()
    test_cli_sessions_empty()
    test_commands_memory()
    test_commands_misc()
    test_cli_diff_and_hook()
    test_resume_session()
    test_build_system_prompt_includes_skills()
    test_cli_question_mark_shows_shortcuts()
    test_cli_repl_error_does_not_crash()
    print("全部通过。")
