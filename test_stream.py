"""流式调用的离线测试：不用 key、不联网。

FakeStreamClient 按剧本返回 chunk（流式碎片）：文字一段段到，
工具调用的参数也可能拆成两段到。验证三件事：
  1. 文本碎片拼成完整回答，token 事件按顺序发出
  2. 拆成两段的工具调用参数能拼起来并执行
  3. 流式回答进历史时是 dict（下次调用能接着用）

Run:  python test_stream.py
"""

from types import SimpleNamespace

from agent import run_agent


class FakeStreamClient:
    """每次 create 返回写好的 chunk 列表（stream=True 时）或单条回复。"""

    def __init__(self, script):
        self._script = list(script)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        item = self._script.pop(0)
        if kwargs.get("stream"):
            return iter(item)
        return item


def _chunk(content=None, tool_calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            delta=SimpleNamespace(content=content, tool_calls=tool_calls),
            finish_reason=None,
        )]
    )


def _tool_delta(index, call_id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _text_reply(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content=text, tool_calls=None,
        ))]
    )


def test_stream_text_answer():
    events = []
    client = FakeStreamClient([
        [_chunk("你好"), _chunk("，我是 Jarvis。")],
    ])
    answer = run_agent(client, "你好", stream=True, verbose=False,
                       on_event=lambda kind, payload: events.append((kind, payload)))
    assert answer == "你好，我是 Jarvis。", answer
    tokens = [p for k, p in events if k == "token"]
    assert tokens == ["你好", "，我是 Jarvis。"], tokens
    print("[OK] 流式文本：碎片按顺序拼成完整回答，token 事件依次发出")


def test_stream_tool_call_split_args():
    events = []
    client = FakeStreamClient([
        [  # 工具调用的参数拆成两段到达
            _chunk(tool_calls=[_tool_delta(0, "call_1", "calculator",
                                           '{"expression": "12 *')]),
            _chunk(tool_calls=[_tool_delta(0, None, None, ' (3 + 4)"}')]),
        ],
        [_chunk("12*(3+4) 等于 84。")],
    ])
    answer = run_agent(client, "12*(3+4) 等于多少？", stream=True, verbose=False,
                       on_event=lambda kind, payload: events.append((kind, payload)))
    assert "84" in answer, answer
    tool_calls = [p for k, p in events if k == "tool_call"]
    assert len(tool_calls) == 1, tool_calls
    assert tool_calls[0]["name"] == "calculator", tool_calls
    assert tool_calls[0]["args"] == {"expression": "12 * (3 + 4)"}, tool_calls
    print("[OK] 流式工具调用：拆成两段的参数拼起来了，calculator 正常执行")


def test_stream_history_is_dict():
    history = [
        {"role": "system", "content": "你是 Jarvis。"},
    ]
    client = FakeStreamClient([
        [_chunk("蓝色。")],
        [_chunk("是的，你最喜欢蓝色。")],
    ])
    run_agent(client, "我最喜欢什么颜色？", history=history,
              stream=True, verbose=False)
    run_agent(client, "你确定吗？", history=history,
              stream=True, verbose=False)
    assistant_msgs = [m for m in history if m.get("role") == "assistant"]
    assert len(assistant_msgs) == 2, history
    assert assistant_msgs[0]["content"] == "蓝色。", assistant_msgs
    assert all(isinstance(m, dict) for m in history), history
    print("[OK] 流式历史：回答以 dict 存进历史，第二轮能接着用")


if __name__ == "__main__":
    test_stream_text_answer()
    test_stream_tool_call_split_args()
    test_stream_history_is_dict()
    print("全部通过。")
