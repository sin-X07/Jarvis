"""Offline test of the agent loop: no API key, no network.

A FakeClient replays a scripted model: first it asks to call the
calculator, then it answers using the tool result. If run_agent wires
the loop correctly, the final answer contains the computed value.

Run:  python test_loop.py
"""

from types import SimpleNamespace

from agent import run_agent


class FakeClient:
    """Minimal stand-in for OpenAI client, returns scripted replies."""

    def __init__(self, script):
        self._script = list(script)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        return self._script.pop(0)


def _tool_call(call_id, name, arguments):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content=None,
            tool_calls=[SimpleNamespace(
                id=call_id,
                function=SimpleNamespace(name=name, arguments=arguments),
            )],
        ))]
    )


def _text_reply(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content=text, tool_calls=None,
        ))]
    )


def test_calculator_loop():
    client = FakeClient([
        _tool_call("call_1", "calculator", '{"expression": "12 * (3 + 4)"}'),
        _text_reply("12*(3+4) 等于 84。"),
    ])
    answer = run_agent(client, "12*(3+4) 等于多少？", verbose=False)
    assert "84" in answer, f"unexpected answer: {answer}"
    print("[OK] 循环正确：模型调了 calculator，拿到 84 后才回答")


def test_direct_answer():
    client = FakeClient([_text_reply("你好，我是 Jarvis。")])
    answer = run_agent(client, "你好", verbose=False)
    assert "Jarvis" in answer, f"unexpected answer: {answer}"
    print("[OK] 无工具调用时直接返回模型回答")


if __name__ == "__main__":
    test_calculator_loop()
    test_direct_answer()
    print("全部通过。")
