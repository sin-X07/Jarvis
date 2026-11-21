"""P8 测试：轨迹回放 + 自更新检查 + CLI 用量/轨迹命令（不用 key、不联网）。

验证：
  1. trajectory：会话事件画成时间线（user/assistant/工具/摘要都有）
  2. update：有新版 / 已是最新 / 联网失败 / tag 读不懂四种情况
  3. CLI：--usage、--trajectory（不存在的 id）、/trajectory 命令
"""

import io
import json
import os
import tempfile
import urllib.error
from contextlib import redirect_stdout
from types import SimpleNamespace

from jarvis.ui.chat import CliSession, handle_command, main
from jarvis.core.config import load_config
from jarvis.memory.memory import FactStore
from jarvis.memory.session_store import SessionStore
from jarvis.ui.trajectory import render_trajectory
from jarvis.ui.update import check_update, format_update


def _session(tmp):
    store = FactStore(path=os.path.join(tmp, "memory.json"))
    sessions = SessionStore(root=os.path.join(tmp, "sessions"))
    args = SimpleNamespace(model=None, max_steps=None,
                           max_budget_usd=None, permission=None)
    return CliSession(object(), load_config(), args,
                      session_store=sessions, store=store)


def test_trajectory_render():
    tmp = tempfile.mkdtemp()
    store = SessionStore(root=os.path.join(tmp, "sessions"))
    sid = store.create(title="测试会话")
    store.append_event(sid, "user", {"content": "帮我写个脚本"})
    store.append_event(sid, "assistant", {
        "content": None,
        "tool_calls": [{"id": "c1", "function": {"name": "write_text_file"}}]})
    store.append_event(sid, "tool_call", {
        "name": "write_text_file", "args": {"path": "a.py"}, "tool_call_id": "c1"})
    store.append_event(sid, "tool_result", {
        "name": "write_text_file", "result": "已写入 a.py", "tool_call_id": "c1"})
    store.append_event(sid, "assistant", {"content": "写好了"})
    store.append_event(sid, "summary", {"content": "之前聊了天气"})
    out = render_trajectory(sid, store)
    assert "会话开始：测试会话" in out, out
    assert "你：帮我写个脚本" in out, out
    assert "调工具 [write_text_file]" in out, out
    assert "▸ 调用 write_text_file" in out, out
    assert "✓ write_text_file 返回" in out, out
    assert "Jarvis：写好了" in out, out
    assert "已压缩成摘要" in out, out
    assert "没有找到会话" in render_trajectory("不存在的id", store)
    print("[OK] 轨迹：user/assistant/工具调用/返回/摘要都画出来了")


def _fake_fetch(payload):
    return lambda url: json.dumps(payload)


def test_update_newer_available():
    result = check_update("0.2.0", fetch=_fake_fetch({
        "tag_name": "v0.3.0",
        "html_url": "https://github.com/sin-X07/Jarvis/releases/tag/v0.3.0",
        "body": "新功能"}))
    assert result["update_available"] is True, result
    assert result["latest"] == "v0.3.0", result
    text = format_update(result)
    assert "有新版：v0.3.0" in text and "releases/tag/v0.3.0" in text, text
    print("[OK] 自更新：有新版时报告版本号和下载地址")


def test_update_already_latest():
    result = check_update("0.2.0", fetch=_fake_fetch({
        "tag_name": "v0.2.0", "html_url": "x", "body": ""}))
    assert result["update_available"] is False, result
    assert "已是最新版" in format_update(result), result
    result = check_update("0.2.0", fetch=_fake_fetch({
        "tag_name": "v0.1.0", "html_url": "x", "body": ""}))
    assert result["update_available"] is False, result
    print("[OK] 自更新：已是最新或本地更新时不报新版")


def test_update_failure():
    def _boom(url):
        raise urllib.error.URLError("没联网")
    result = check_update("0.2.0", fetch=_boom)
    assert result["update_available"] is False, result
    assert "检查失败" in format_update(result), result
    result = check_update("0.2.0", fetch=lambda url: "这不是 JSON")
    assert "检查失败" in format_update(result), result
    result = check_update("0.2.0", fetch=_fake_fetch(
        {"tag_name": "garbage-version", "html_url": "x", "body": ""}))
    assert result["update_available"] is False, result
    print("[OK] 自更新：联网失败、坏 JSON、读不懂的 tag 都不崩")


def test_cli_usage_flag():
    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["--usage"])
    out = buf.getvalue()
    assert "用量记录" in out or "tokens" in out, out
    print("[OK] CLI：--usage 打印用量小结（没记录时也不崩）")


def test_cli_trajectory_flag_missing():
    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["--trajectory", "zzzz-not-exist"])
    assert "没有" in buf.getvalue(), buf.getvalue()
    print("[OK] CLI：--trajectory 查不到会话时明说，不崩")


def test_trajectory_command():
    tmp = tempfile.mkdtemp()
    session = _session(tmp)
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert handle_command(session, "/trajectory") is True
    assert "还没有会话" in buf.getvalue(), buf.getvalue()
    sid = session.sessions.create(title="t")
    session.sessions.append_event(sid, "user", {"content": "hi"})
    session.session_id = sid
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert handle_command(session, "/trajectory") is True
    assert "你：hi" in buf.getvalue(), buf.getvalue()
    print("[OK] CLI：/trajectory 没会话时提示，有会话时画轨迹")


if __name__ == "__main__":
    test_trajectory_render()
    test_update_newer_available()
    test_update_already_latest()
    test_update_failure()
    test_cli_usage_flag()
    test_cli_trajectory_flag_missing()
    test_trajectory_command()
    print("全部通过。")
