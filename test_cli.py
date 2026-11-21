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

import chat
from chat import CliSession, handle_command, main
from config import load_config
from md import to_ansi, to_html
from memory import FactStore
from session_store import SessionStore


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
    tmp = tempfile.mkdtemp()
    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["--sessions"])
    # 默认 ~/.jarvis/sessions 里可能有会话，只不断言为空，断言不崩
    assert "会话" in buf.getvalue() or "还没有会话" in buf.getvalue()
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


if __name__ == "__main__":
    test_md_html()
    test_md_ansi()
    test_cli_version()
    test_cli_sessions_empty()
    test_commands_memory()
    test_commands_misc()
    test_resume_session()
    test_build_system_prompt_includes_skills()
    print("全部通过。")
