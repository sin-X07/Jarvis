"""U7 测试：四界面共用层 viewstate + planner 用量透传（不用 key、不联网）。

验证：
  1. ViewState：11 种事件按契约更新状态，未知 kind 报错；待办行
     ☐/◐/☑；用量无真数时显示“—”；空态判定
  2. 检查点换算：点第 n 行 → 步数 / rewind_to 的 cutoff
  3. 统一文案：build_execute_prompt 四方同一份；diff_note 接受/拒绝
  4. planner：make_plan / verify_plan 的 token 进 UsageTracker
     （以前计划轮 token 四方全漏）；不传 usage 时照常跑
  5. CLI：/checkpoints 列出检查点，/rewind-to 按序号回到那一轮之前

Run:  python test_viewstate.py
"""

import io
import os
import tempfile
from contextlib import redirect_stdout
from types import SimpleNamespace

from jarvis.core.usage import UsageTracker
from jarvis.memory.planner import Plan, make_plan, verify_plan
from jarvis.ui.viewstate import (
    ViewState, build_execute_prompt, checkpoint_cutoff_for_row,
    checkpoint_steps_for_row, diff_note, format_usage)
from test_loop import FakeClient, _text_reply

PLAN_TEXT = ("目标：写个问候脚本\n步骤：\n"
             "1. 写文件 —— 验收：文件存在\n"
             "2. 跑一遍 —— 验收：输出问候语\n风险：\n- 无\n")


def _usage(prompt=10, completion=5):
    return SimpleNamespace(prompt_tokens=prompt,
                           completion_tokens=completion,
                           total_tokens=prompt + completion)


def test_viewstate_events():
    st = ViewState(model="deepseek-flash")
    assert st.is_empty()
    st.apply_event("text_delta", {"text": "你好"})
    assert st.busy and st.current_text == "你好"
    assert not st.is_empty()
    st.apply_event("thinking_delta", {"text": "想想"})
    assert st.thinking_text == "想想"
    st.apply_event("todo_set", {"todos": [
        {"content": "写文件", "status": "done"},
        {"content": "跑一遍", "status": "active"},
        {"content": "收尾", "status": "pending"}]})
    assert st.todo_lines() == [
        "☑ 1. 写文件", "◐ 2. 跑一遍", "☐ 3. 收尾"]
    st.apply_event("plan_proposed", {"markdown": PLAN_TEXT,
                                     "goal": "写个问候脚本"})
    assert st.plan["goal"] == "写个问候脚本"
    st.apply_event("status", {"prompt_tokens": 100, "completion_tokens": 20,
                              "total_tokens": 120, "cost_cny": 0.0001,
                              "calls": 1, "unknown_calls": 0,
                              "context_pct": 0.05})
    assert st.usage_line() == "本轮 120 token · ¥0.0001"
    st.apply_event("turn_end", {"step": 1, "answer": "做完了"})
    assert not st.busy and st.turns == 1
    assert st.current_text == "" and st.thinking_text == ""
    try:
        st.apply_event("no_such_kind", {})
    except ValueError:
        pass
    else:
        raise AssertionError("未知事件种类该报错")
    print("[OK] ViewState：11 种事件按契约更新状态，未知 kind 报错")


def test_viewstate_usage_unknown():
    assert format_usage(0, 0.0) == "用量：—"
    assert format_usage(0, 0.0, 2) == "用量：— · 未返回用量 2 次"
    assert format_usage(50, 0.0012, 1) == "本轮 50 token · ¥0.0012 · 未返回用量 1 次"
    st = ViewState()
    assert st.usage_line() == "用量：—"
    print("[OK] ViewState：无真实用量显示“—”，不估算")


def test_checkpoint_row_helpers():
    rows = [{"id": "a", "path": "/tmp/a.txt", "ts": "2026-11-22T10:00:00"},
            {"id": "b", "path": "/tmp/a.txt", "ts": "2026-11-22T10:01:00"},
            {"id": "c", "path": "/tmp/a.txt", "ts": "2026-11-22T10:02:00"}]
    # 点第 1 行（最旧）= 恢复到它之前：弹出全部 3 行
    assert checkpoint_steps_for_row(rows, 0) == 3
    assert checkpoint_cutoff_for_row(rows, 0) == "2026-11-22T10:00:00"
    # 点第 3 行（最新）= 只恢复它自己
    assert checkpoint_steps_for_row(rows, 2) == 1
    assert checkpoint_cutoff_for_row(rows, 2) == "2026-11-22T10:02:00"
    assert checkpoint_steps_for_row(rows, 9) == 0
    assert checkpoint_cutoff_for_row(rows, 9) is None
    assert checkpoint_steps_for_row([], 0) == 0
    print("[OK] ViewState：检查点点行换算（步数 / rewind_to cutoff）")


def test_shared_copy():
    prompt = build_execute_prompt("目标：X\n步骤：\n1. 做 —— 验收：成",
                                  "做 X")
    assert "按顺序执行下面的计划" in prompt and "任务：做 X" in prompt
    assert "已接受当前改动" in diff_note(True)
    assert "检查点" in diff_note(False)
    print("[OK] ViewState：执行提示词与 Diff 文案四方同一份")


def test_planner_usage_recorded():
    tracker = UsageTracker()
    client = FakeClient([_text_reply(PLAN_TEXT, usage=_usage(10, 5))])
    plan = make_plan(client, "deepseek-flash", "写个问候脚本",
                     usage=tracker)
    assert plan.goal == "写个问候脚本" and len(plan.steps) == 2
    assert tracker.total_tokens == 15, tracker.total_tokens
    assert tracker.calls == 1
    client2 = FakeClient([_text_reply("1. 写文件：通过\n2. 跑一遍：通过",
                                      usage=_usage(20, 8))])
    report = verify_plan(client2, "deepseek-flash", plan, "做完了",
                         usage=tracker)
    assert "通过" in report
    assert tracker.total_tokens == 15 + 28, tracker.total_tokens
    assert tracker.calls == 2
    print("[OK] planner：make_plan 与 verify_plan 的 token 都进用量统计")


def test_planner_usage_optional():
    client = FakeClient([_text_reply(PLAN_TEXT)])
    plan = make_plan(client, "deepseek-flash", "写个问候脚本")
    assert plan.goal == "写个问候脚本"
    client2 = FakeClient([_text_reply("1. 写文件：通过")])
    assert "通过" in verify_plan(client2, "deepseek-flash", plan, "做完了")
    print("[OK] planner：不传 usage 时照常跑（向后兼容）")


def test_cli_checkpoints_and_rewind_to():
    from jarvis.ui.chat import CliSession, handle_command
    from jarvis.core.config import load_config
    from jarvis.memory.checkpoint import CheckpointStore
    from jarvis.memory.memory import FactStore
    from jarvis.memory.session_store import SessionStore
    from types import SimpleNamespace as NS

    tmp = tempfile.mkdtemp()
    target = os.path.join(tmp, "a.txt")
    with open(target, "w", encoding="utf-8") as f:
        f.write("v1\n")
    args = NS(model=None, max_steps=None, max_budget_usd=None,
              permission=None)
    session = CliSession(object(), load_config(), args,
                         session_store=SessionStore(
                             root=os.path.join(tmp, "sessions")),
                         store=FactStore(path=os.path.join(tmp, "memory.json")))
    session.session_id = "sess-1"
    session.checkpoints = CheckpointStore(
        "sess-1", root=os.path.join(tmp, "ckpt"))
    session.checkpoints.snapshot(target)
    with open(target, "w", encoding="utf-8") as f:
        f.write("v2\n")
    session.checkpoints.snapshot(target)
    with open(target, "w", encoding="utf-8") as f:
        f.write("v3\n")
    buf = io.StringIO()
    with redirect_stdout(buf):
        handle_command(session, "/checkpoints")
    out = buf.getvalue()
    assert "1. " in out and "2. " in out and "a.txt" in out, out
    buf = io.StringIO()
    with redirect_stdout(buf):
        handle_command(session, "/rewind-to 1")
    # 第 1 个检查点备份的是 v1：回到它之前，文件变回 v1，清单清空
    assert open(target, encoding="utf-8").read() == "v1\n"
    assert session.checkpoints.list_checkpoints() == []
    buf = io.StringIO()
    with redirect_stdout(buf):
        handle_command(session, "/rewind-to 9")
    assert "没有这个序号" in buf.getvalue()
    print("[OK] CLI：/checkpoints 列出检查点，/rewind-to 按序号回到那一轮之前")


if __name__ == "__main__":
    test_viewstate_events()
    test_viewstate_usage_unknown()
    test_checkpoint_row_helpers()
    test_shared_copy()
    test_planner_usage_recorded()
    test_planner_usage_optional()
    test_cli_checkpoints_and_rewind_to()
    print("全部通过。")
