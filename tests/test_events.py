"""U0 测试：事件模型 + 工具摘要 + 主题生成器 + 轨迹重放。

验证：
  1. EVENT_KINDS 是 11 种事件，每种都有字段/发出方/说明；
     new_event 造事件，未知种类报错
  2. summarise_tool_call 覆盖全部 25 个内置工具 + mcp__ 前缀 +
     未知工具兜底（绝不直接甩原始 JSON）
  3. tool_risk：run_shell 是 danger，只读工具是 read，其余（含
     mcp__、未知工具）保守算 write
  4. 主题生成器：改一个 token，to_qss/to_css_vars 输出跟着变；
     dark 主题生成的 QSS 和旧手写 DARK_STYLE 视觉一致
  5. iter_turns 按 user 事件分轮；replay_transcript 把 JSONL
     重放成界面条目（含 error）

Run:  python test_events.py
"""

import os
import re
import tempfile

from jarvis.ui.events import (EVENT_KINDS, new_event, short,
                              summarise_tool_call, tool_risk)
from jarvis.ui.theme import load_theme, to_ansi_roles, to_css_vars, to_qss
from jarvis.memory.session_store import SessionStore
from jarvis.ui.trajectory import iter_turns, replay_transcript


def test_event_kinds_table():
    assert len(EVENT_KINDS) == 11, EVENT_KINDS.keys()
    for kind, spec in EVENT_KINDS.items():
        assert spec["fields"] and spec["emitted_by"] and spec["description"], kind
    for kind in ("text_delta", "thinking_delta", "tool_start", "tool_end",
                 "todo_set", "plan_proposed", "diff_proposed",
                 "approval_request", "status", "error", "turn_end"):
        assert kind in EVENT_KINDS, kind
    event = new_event("text_delta", {"text": "你好"})
    assert event == {"kind": "text_delta", "payload": {"text": "你好"}}
    try:
        new_event("token", {"text": "x"})
    except ValueError:
        print("[OK] 事件表：11 种事件齐全，旧名 token 已不在表里")
        return
    raise AssertionError("未知事件种类该报错")


def test_short():
    assert short("  hello   world  ") == "hello world"
    assert short("x" * 100) == "x" * 60 + "…"
    print("[OK] 事件：short 截断正常")


def test_summarise_all_tools():
    cases = {
        "get_current_time": ({}, "看现在几点"),
        "calculator": ({"expression": "1+1"}, "算 1+1"),
        "read_text_file": ({"path": "README.md"}, "读 README.md"),
        "list_files": ({"path": "src"}, "列出 src"),
        "write_text_file": ({"path": "a.txt"}, "写 a.txt"),
        "edit_file": ({"path": "a.py"}, "改 a.py"),
        "fetch_url": ({"url": "https://example.com"}, "访问 https://example.com"),
        "run_shell": ({"command": "git status"}, "执行 git status"),
        "run_shell_background": ({"command": "make"}, "后台跑 make"),
        "glob_files": ({"pattern": "*.py"}, "找文件 *.py"),
        "grep_files": ({"pattern": "TODO", "path": "src"}, "搜 TODO（src）"),
        "git_status": ({}, "git status"),
        "git_diff": ({}, "git diff"),
        "git_log": ({"count": 5}, "git log（5 条）"),
        "git_commit": ({"message": "fix"}, "提交：fix"),
        "web_search": ({"query": "python"}, "搜索 python"),
        "remember_fact": ({"fact": "喜欢茶"}, "记住：喜欢茶"),
        "recall_facts": ({"query": "茶"}, "回忆：茶"),
        "delete_fact": ({"fact": "喜欢茶"}, "忘掉：喜欢茶"),
        "update_fact": ({"old_fact": "a", "new_fact": "b"}, "改记忆：a → b"),
        "update_todo": ({"title": "写测试"}, "更新待办：写测试"),
        "job_status": ({"job_id": "j1"}, "查任务 j1"),
        "job_result": ({"job_id": "j1"}, "取任务结果 j1"),
        "delegate": ({"task": "调研 X"}, "派子代理：调研 X"),
        "use_skill": ({"name": "tdd"}, "用技能 tdd"),
    }
    for name, (args, expected) in cases.items():
        got = summarise_tool_call(name, args)
        assert got == expected, (name, got, expected)
        # JSON 字符串参数也要能解析
        assert summarise_tool_call(name, __import__("json").dumps(args, ensure_ascii=False)) == expected
    # mcp__ 前缀与未知工具走兜底，绝不甩原始 JSON
    assert summarise_tool_call("mcp__gh_search", {}) == "MCP 工具 gh_search"
    assert summarise_tool_call("weird_tool", {"x": "hello"}) == "weird_tool hello"
    assert summarise_tool_call("weird_tool", {}) == "weird_tool"
    assert "{" not in summarise_tool_call("read_text_file", {"path": "a.py"})
    print("[OK] 摘要：25 个内置工具 + mcp__ + 兜底全覆盖")


def test_tool_risk():
    assert tool_risk("run_shell") == "danger"
    assert tool_risk("run_shell_background") == "danger"
    for name in ("read_text_file", "list_files", "glob_files", "grep_files",
                 "git_status", "git_diff", "git_log", "get_current_time",
                 "calculator", "fetch_url", "web_search", "recall_facts",
                 "job_status", "job_result", "use_skill"):
        assert tool_risk(name) == "read", name
    for name in ("write_text_file", "edit_file", "git_commit",
                 "remember_fact", "delete_fact", "update_fact",
                 "update_todo", "delegate", "mcp__gh_search", "weird_tool"):
        assert tool_risk(name) == "write", name
    print("[OK] 风险：danger/read/write 分级正确，未知工具保守算 write")


def test_theme_generator_follows_tokens():
    theme = load_theme("dark")
    assert theme["color"]["bg"] == "#1e1e2e"
    # codeBg 只在 theme.json 里有，内置 fallback 没有——能读到它，
    # 说明 load_theme 真读了文件（不是一直走 fallback）。
    assert theme["color"]["codeBg"] == "#11111b", theme["color"]
    qss = to_qss(theme)
    css = to_css_vars(theme)
    assert "#1e1e2e" in qss and "#89b4fa" in qss
    assert "--color-bg: #1e1e2e;" in css and "--color-accent: #89b4fa;" in css
    # 改一个 token，两份产物都跟着变
    changed = dict(theme)
    changed["color"] = dict(theme["color"], accent="#ff0000")
    assert "#ff0000" in to_qss(changed) and "--color-accent: #ff0000;" in to_css_vars(changed)
    assert "#ff0000" not in to_qss(theme)
    # ANSI 角色表是固定映射，不随十六进制推导
    roles = to_ansi_roles(theme)
    assert roles["accent"] and roles["danger"] and roles["success"]
    assert to_ansi_roles() == roles
    print("[OK] 主题：改 token 就改三份产物，ANSI 角色表固定")


def test_qss_matches_old_dark_style():
    theme = load_theme("dark")
    generated = to_qss(theme)
    old = """
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
QComboBox, QSpinBox, QDoubleSpinBox { background: #181825; color: #cdd6f4;
            border: 1px solid #313244; border-radius: 6px; padding: 4px; }
"""
    norm = lambda s: re.sub(r"\s+", " ", s).strip()
    assert norm(generated) == norm(old), generated
    print("[OK] 主题：生成的 QSS 和旧手写 DARK_STYLE 视觉一致")


def test_turns_and_replay():
    store = SessionStore(root=os.path.join(tempfile.mkdtemp(), "sessions"))
    store.append_event("s1", "user", {"content": "第一问"})
    store.append_event("s1", "assistant", {"content": "答1"})
    store.append_event("s1", "tool_call", {"name": "calculator",
                                           "args": {"expression": "1+1"},
                                           "tool_call_id": "c1"})
    store.append_event("s1", "tool_result", {"name": "calculator",
                                             "result": "2", "tool_call_id": "c1"})
    store.append_event("s1", "user", {"content": "第二问"})
    store.append_event("s1", "error", {"message": "断网了"})
    events = store.load_events("s1")
    turns = iter_turns(events)
    assert len(turns) == 2, turns
    assert len(turns[0]) == 4 and len(turns[1]) == 2, turns
    assert iter_turns([]) == []
    items = replay_transcript("s1", store)
    kinds = [i["kind"] for i in items]
    assert kinds == ["user", "assistant", "tool_call", "tool_result",
                     "user", "error"], kinds
    assert items[3]["result"] == "2"
    assert items[5]["content"] == "断网了"
    assert replay_transcript("nope", store) == []
    print("[OK] 轨迹：iter_turns 分轮、replay_transcript 重放（含 error）")


if __name__ == "__main__":
    test_event_kinds_table()
    test_short()
    test_summarise_all_tools()
    test_tool_risk()
    test_theme_generator_follows_tokens()
    test_qss_matches_old_dark_style()
    test_turns_and_replay()
    print("全部通过。")
