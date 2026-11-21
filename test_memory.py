"""记忆层的离线测试：不用 key、不联网。

覆盖三件事：
  1. 短期记忆裁剪：system 永不删、按整轮删、不断开 tool 调用链
  2. 长期记忆：存取、去重、检索、落盘再读回来
  3. 多轮对话：run_agent 两次调用共享一份 history，第二轮能看到第一轮

Run:  python test_memory.py
"""

import os
import tempfile
from types import SimpleNamespace

from agent import run_agent
from memory import ConversationMemory, FactStore, memory_tools


# --- fake client：和 test_loop.py 同一套路，返回写好的剧本 ---

class FakeClient:
    def __init__(self, script):
        self._script = list(script)
        self.seen_messages = []  # 每次调用收到的 messages，攒下来检查
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        # 存一份当时的快照（messages 列表之后还会被追加，不能只存引用）
        self.seen_messages.append(list(kwargs.get("messages", [])))
        return self._script.pop(0)


def _text_reply(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content=text, tool_calls=None,
        ))]
    )


def _assistant_msg(content=None, tool_calls=None):
    """模拟模型返回的 message 对象（带 role 属性，不像 dict 那样有 .get）。"""
    return SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls)


# --- 1. 短期记忆裁剪 ---

def test_trim_keeps_system_and_whole_turns():
    memory = ConversationMemory("你是 Jarvis。", max_messages=5)
    memory.add_user("第一问")
    memory.add_assistant(_assistant_msg("第一答"))
    memory.add_user("第二问")
    memory.add_assistant(_assistant_msg("第二答"))
    memory.add_user("第三问")
    # 此时 1 system + 5 条 = 6 > 5，触发裁剪：整轮删掉最旧的“第一问+第一答”
    roles = [m.get("role") if isinstance(m, dict) else m.role for m in memory.messages]
    texts = [m.get("content") if isinstance(m, dict) else m.content for m in memory.messages]
    assert roles[0] == "system", "system prompt 被删了"
    assert "第一问" not in texts and "第一答" not in texts, "最旧的一轮没删掉"
    assert "第二问" in texts and "第三问" in texts, "不该删的轮次被删了"
    print("[OK] 裁剪：system 保留，按整轮删最旧的")


def test_trim_never_breaks_tool_chain():
    memory = ConversationMemory("你是 Jarvis。", max_messages=5)
    call = SimpleNamespace(id="call_1",
                           function=SimpleNamespace(name="calculator", arguments="{}"))
    memory.add_user("算 12*(3+4)")
    memory.add_assistant(_assistant_msg(tool_calls=[call]))
    memory.add_tool_result("call_1", "84")
    memory.add_assistant(_assistant_msg("等于 84。"))
    memory.add_user("再算 2+2")
    # 满了，裁剪必须把“算 12*(3+4)”整轮连同 tool 消息一起删，不能只删一半
    tool_msgs = [m for m in memory.messages
                 if (m.get("role") if isinstance(m, dict) else m.role) == "tool"]
    assistant_msgs = [m for m in memory.messages
                      if (m.get("role") if isinstance(m, dict) else m.role) == "assistant"]
    assert not tool_msgs, f"tool 消息被孤立了（它的 assistant 消息已被删）：{tool_msgs}"
    assert not assistant_msgs, "整轮应该连同 assistant 消息一起删掉"
    texts = [m.get("content") if isinstance(m, dict) else m.content for m in memory.messages]
    assert "再算 2+2" in texts, "最新一轮被误删"
    print("[OK] 裁剪：tool 调用链不断开（整轮一起删）")


# --- 2. 长期记忆 ---

def test_fact_store(tmp_path):
    path = os.path.join(tmp_path, "memory.json")
    store = FactStore(path)
    assert store.add("用户最喜欢的颜色是蓝色") == "记住了：用户最喜欢的颜色是蓝色"
    assert "已经记过了" in store.add("用户最喜欢的颜色是蓝色")
    store.add("用户每天早上七点起床")
    store.add("用户养了一只叫豆豆的猫")

    hits = store.search("喜欢什么颜色")
    assert hits and "蓝色" in hits[0], f"检索没命中蓝色：{hits}"
    assert store.search("完全不相关的东西") == []

    # 落盘再读回来，事实还在
    store2 = FactStore(path)
    assert len(store2.all_texts()) == 3, store2.all_texts()
    print("[OK] 长期记忆：存取、去重、检索、落盘重读都正常")


def test_memory_tools():
    with tempfile.TemporaryDirectory() as tmp:
        store = FactStore(os.path.join(tmp, "memory.json"))
        schemas, funcs = memory_tools(store)
        assert funcs["remember_fact"](fact="用户不吃辣") == "记住了：用户不吃辣"
        assert "不吃辣" in funcs["recall_facts"](query="吃辣吗")
        assert "没找到" in funcs["recall_facts"](query="毫不相关")
        assert {s["function"]["name"] for s in schemas} == {"remember_fact", "recall_facts"}
    print("[OK] 记忆工具：remember_fact / recall_facts 可被模型调用")


# --- 3. 多轮对话：history 跨调用共享 ---

def test_multi_turn_history():
    memory = ConversationMemory("你是 Jarvis。")
    client = FakeClient([
        _text_reply("蓝色。"),
        _text_reply("是的，你最喜欢蓝色。"),
    ])
    run_agent(client, "我最喜欢什么颜色？", history=memory.messages, verbose=False)
    run_agent(client, "你确定吗？", history=memory.messages, verbose=False)
    second_call_messages = client.seen_messages[1]
    seen = [m.get("content") if isinstance(m, dict) else m.content
            for m in second_call_messages]
    assert "我最喜欢什么颜色？" in seen, "第二轮看不到第一轮的问题"
    assert "蓝色。" in seen, "第二轮看不到模型的上一句回答"
    assert "你确定吗？" in seen
    print("[OK] 多轮对话：第二轮能看到第一轮的问题和回答")


if __name__ == "__main__":
    test_trim_keeps_system_and_whole_turns()
    test_trim_never_breaks_tool_chain()
    with tempfile.TemporaryDirectory() as tmp:
        test_fact_store(tmp)
    test_memory_tools()
    test_multi_turn_history()
    print("全部通过。")
