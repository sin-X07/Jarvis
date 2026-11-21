"""P0 测试：模型适配层——重试与用量（不用 key、不联网）。

验证：
  1. 非流式调用：返回消息 + 真实 usage
  2. 429 类异常重试 3 次后成功（退避 1s→2s→4s，用假 sleep，不真等）
  3. 不可重试的异常不重试，直接抛出
  4. 重试耗尽后抛出最后一次异常
  5. 流式调用：碎片拼成完整消息，用量是估算（estimated=True）
  6. jlog 记一行 JSON，不含敏感内容
"""

import contextlib
import io
import json
from types import SimpleNamespace

from jlog import log_event
from model import call_model
from test_stream import FakeStreamClient, _chunk


class FakeRateLimitError(Exception):
    """名字里带 RateLimit，触发重试。"""


class FakeValueError(ValueError):
    """不可重试的异常。"""


def _msg_client(answer="你好", usage=None):
    msg = SimpleNamespace(content=answer, tool_calls=None)
    choice = SimpleNamespace(message=msg)
    resp = SimpleNamespace(choices=[choice], usage=usage)

    class _Completions:
        def create(self, **kwargs):
            return resp

    class _Chat:
        completions = _Completions()

    return SimpleNamespace(chat=_Chat())


def _flaky_client(fail_times, exc_factory):
    state = {"n": 0}

    class _Completions:
        def create(self, **kwargs):
            state["n"] += 1
            if state["n"] <= fail_times:
                raise exc_factory()
            msg = SimpleNamespace(content="好了", tool_calls=None)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=msg)], usage=None)

    class _Chat:
        completions = _Completions()

    return SimpleNamespace(chat=_Chat()), state


def test_non_stream_usage():
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
    client = _msg_client("你好", usage)
    msg, got = call_model(client, "deepseek-flash",
                          [{"role": "user", "content": "你好"}], tools=[])
    assert msg["content"] == "你好", msg
    assert got == {"prompt_tokens": 10, "completion_tokens": 5,
                   "estimated": False}, got
    print("[OK] 模型层：非流式返回真实 usage")


def test_retry_then_success():
    client, state = _flaky_client(2, FakeRateLimitError)
    slept = []
    msg, _ = call_model(client, "deepseek-flash",
                        [{"role": "user", "content": "hi"}], tools=[],
                        sleep=slept.append)
    assert msg["content"] == "好了", msg
    assert state["n"] == 3, state  # 2 次失败 + 1 次成功
    assert slept == [1, 2], slept  # 指数退避 1s → 2s
    print("[OK] 模型层：429 重试 2 次后成功，退避 1s→2s")


def test_non_retryable_no_retry():
    client, state = _flaky_client(5, FakeValueError)
    try:
        call_model(client, "deepseek-flash",
                   [{"role": "user", "content": "hi"}], tools=[],
                   retry_times=3, sleep=lambda s: None)
    except FakeValueError:
        assert state["n"] == 1, state  # 只调了一次，没重试
        print("[OK] 模型层：不可重试的异常不重试")
        return
    raise AssertionError("不可重试的异常该直接抛出")


def test_retry_exhausted():
    client, state = _flaky_client(99, FakeRateLimitError)
    try:
        call_model(client, "deepseek-flash",
                   [{"role": "user", "content": "hi"}], tools=[],
                   retry_times=3, sleep=lambda s: None)
    except FakeRateLimitError:
        assert state["n"] == 4, state  # 1 次 + 3 次重试
        print("[OK] 模型层：重试耗尽后抛出异常")
        return
    raise AssertionError("重试耗尽该抛出异常")


def test_stream_estimated_usage():
    client = FakeStreamClient([[_chunk("你好"), _chunk("呀")]])
    msg, usage = call_model(client, "deepseek-flash",
                            [{"role": "user", "content": "你好"}], tools=[],
                            stream=True)
    assert msg["content"] == "你好呀", msg
    assert usage["estimated"] is True, usage
    assert usage["prompt_tokens"] > 0 and usage["completion_tokens"] > 0, usage
    print("[OK] 模型层：流式用量按字符估算并标 estimated")


def test_jlog_one_json_line():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        log_event("chat", duration_ms=12, ok=True, reason="ok")
    line = buf.getvalue().strip()
    row = json.loads(line)
    assert row["event"] == "chat" and row["ok"] is True, row
    assert "ts" in row and "key" not in str(row).lower(), row
    print("[OK] 日志：一行 JSON，只记摘要")


if __name__ == "__main__":
    test_non_stream_usage()
    test_retry_then_success()
    test_non_retryable_no_retry()
    test_retry_exhausted()
    test_stream_estimated_usage()
    test_jlog_one_json_line()
    print("全部通过。")
