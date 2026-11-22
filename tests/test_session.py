"""P3 测试：会话落盘与上下文压缩（不用 key、不联网）。

验证：
  1. 会话：创建、追加事件、恢复消息（含 tool 调用链配对）、列表、分支、删除
  2. agent 循环的事件能直接落盘再恢复，恢复后的消息能接着跑循环
  3. 压缩：token 估算合理；整轮切，不拆 tool 链；最近几轮不动
  4. maybe_compact：没超阈值不动，超了才压
  5. 用量：UsageTracker 记数、算费用、小结一句话
"""

import os
import tempfile

from jarvis.core.agent import run_agent
from jarvis.memory.compaction import compact, estimate_messages_tokens, maybe_compact
from jarvis.memory.session_store import SessionStore
from test_loop import FakeClient, _text_reply, _tool_call
from jarvis.core.usage import UsageTracker, cost_usd, estimate_tokens


def test_session_roundtrip():
    store = SessionStore(root=tempfile.mkdtemp())
    sid = store.create(title="测试会话")
    store.append_event(sid, "user", {"content": "12*(3+4) 等于多少？"})
    store.append_event(sid, "assistant", {
        "content": None,
        "tool_calls": [{"id": "call_1", "type": "function", "function": {
            "name": "calculator", "arguments": '{"expression": "12 * (3 + 4)"}'}}]})
    store.append_event(sid, "tool_result", {
        "name": "calculator", "result": "84", "tool_call_id": "call_1"})
    store.append_event(sid, "assistant", {"content": "等于 84。"})
    messages = store.load_messages(sid)
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "tool", "assistant"], roles
    assert messages[2]["tool_call_id"] == "call_1", messages
    assert messages[1]["tool_calls"][0]["function"]["name"] == "calculator"
    sessions = store.list_sessions()
    assert len(sessions) == 1 and sessions[0]["turns"] == 1, sessions
    assert sessions[0]["title"] == "测试会话", sessions
    print("[OK] 会话：落盘→恢复，tool 调用链配对正确")


def test_session_fork_and_delete():
    store = SessionStore(root=tempfile.mkdtemp())
    sid = store.create()
    store.append_event(sid, "user", {"content": "你好"})
    store.append_event(sid, "assistant", {"content": "你好呀"})
    new_id = store.fork(sid)
    assert new_id != sid
    assert store.load_messages(new_id) == store.load_messages(sid)
    assert store.delete(sid) is True
    assert store.load_messages(sid) == []
    assert len(store.list_sessions()) == 1
    print("[OK] 会话：fork 复制全部事件，删除只删自己")


def test_agent_records_to_session():
    store = SessionStore(root=tempfile.mkdtemp())
    sid = store.create()
    client = FakeClient([
        _tool_call("call_1", "calculator", '{"expression": "12 * (3 + 4)"}'),
        _text_reply("12*(3+4) 等于 84。"),
    ])
    answer = run_agent(client, "12*(3+4) 等于多少？", verbose=False,
                       session_store=store, session_id=sid)
    assert "84" in answer, answer
    messages = store.load_messages(sid)
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "tool", "assistant"], roles
    # 恢复后的消息能直接当 history 接着跑（第二轮）
    history = [{"role": "system", "content": "你是 Jarvis。"}] + messages
    client2 = FakeClient([_text_reply("是的，84。")])
    answer2 = run_agent(client2, "确定吗？", history=history, verbose=False)
    assert "84" in answer2, answer2
    print("[OK] 会话：agent 循环的事件落盘后，恢复出来能接着跑")


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("你好世界") == 4  # 中文 1 字 1 token
    assert estimate_tokens("hello world, this is a test") > 5
    print("[OK] 用量：token 估算中文 1 字 1 token、英文约 4 字符 1 token")


def test_compact_keeps_recent_turns():
    messages = [{"role": "system", "content": "你是 Jarvis。"}]
    for i in range(6):
        messages.append({"role": "user", "content": f"第{i}个问题"})
        messages.append({"role": "assistant", "content": f"第{i}个回答"})
    new, summary = compact(messages, lambda old: "摘要：前几轮聊了颜色",
                            keep_turns=2)
    assert summary, summary
    assert new[0]["role"] == "system"
    assert new[1]["content"].startswith("【此前对话摘要】"), new[1]
    # 最近 2 轮（第 4、5 轮）还在
    assert new[-4]["content"] == "第4个问题" and new[-3]["content"] == "第4个回答", new
    assert new[-2]["content"] == "第5个问题" and new[-1]["content"] == "第5个回答", new
    print("[OK] 压缩：旧轮次变摘要，最近 2 轮不动")


def test_compact_keeps_tool_chain():
    messages = [
        {"role": "system", "content": "你是 Jarvis。"},
        {"role": "user", "content": "算一下"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "calculator", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "84"},
        {"role": "assistant", "content": "等于 84。"},
        {"role": "user", "content": "还有呢"},
        {"role": "assistant", "content": "没了。"},
        {"role": "user", "content": "再问一个"},
        {"role": "assistant", "content": "请讲。"},
    ]
    # 压掉最早的 1 轮（含 tool 链整轮一起进摘要），不拆链
    new, summary = compact(messages, lambda old: "摘要：算过 84", keep_turns=2)
    assert summary
    roles = [m["role"] for m in new]
    assert "tool" not in roles, roles  # tool 消息随整轮进了摘要，没剩半截
    assert new[-2]["content"] == "再问一个", new
    print("[OK] 压缩：tool 调用链整轮进摘要，不拆散")


def test_maybe_compact_threshold():
    small = [{"role": "system", "content": "x"},
             {"role": "user", "content": "hi"},
             {"role": "assistant", "content": "hello"}]
    new, did, _ = maybe_compact(small, lambda old: "摘要", limit_tokens=100000)
    assert not did and new is small
    big = [{"role": "system", "content": "x"}]
    for i in range(10):
        big.append({"role": "user", "content": "问题" * 200})
        big.append({"role": "assistant", "content": "回答" * 200})
    new, did, summary = maybe_compact(big, lambda old: "摘要", limit_tokens=1000)
    assert did and summary, (did, summary)
    assert estimate_messages_tokens(new) < estimate_messages_tokens(big)
    print("[OK] 压缩：没超阈值不动，超了才压")


def test_usage_tracker_and_cost():
    tracker = UsageTracker()
    tracker.record({"prompt_tokens": 1000, "completion_tokens": 500,
                    "unknown": False})
    tracker.record({"prompt_tokens": None, "completion_tokens": None,
                    "unknown": True})
    assert tracker.calls == 2 and tracker.total_tokens == 1500, tracker
    assert tracker.unknown_calls == 1
    assert tracker.last_prompt_tokens == 1000
    assert tracker.context_pct() == 1000 / 1_000_000
    assert abs(tracker.cost_usd - cost_usd(1000, 500)) < 1e-9
    summary = tracker.summary()
    assert "1500" in summary and "¥" in summary and "未返回用量" in summary
    assert "估算" not in summary, summary
    print("[OK] 用量：记真数、算费用、未知调用不混入合计、context 条取真数")


if __name__ == "__main__":
    test_session_roundtrip()
    test_session_fork_and_delete()
    test_agent_records_to_session()
    test_estimate_tokens()
    test_compact_keeps_recent_turns()
    test_compact_keeps_tool_chain()
    test_maybe_compact_threshold()
    test_usage_tracker_and_cost()
    print("全部通过。")
