"""网页版的离线测试：不用 key、不联网。

FakeStreamClient 按剧本返回 chunk；TestClient 直接调 FastAPI 应用。
验证：
  1. GET / 返回聊天页
  2. GET /api/health 正常
  3. POST /api/chat：token 按顺序流出来，done 里有回答、更新过的历史和事实
  4. 流式工具调用：tool 事件发出，calculator 正常执行
  5. 请求里带的 key 真被用来建 client（BYOK）
  6. 没带 key 且环境里也没有时报 400，提示填 key
  7. 没带 key 时用环境里的 key（本地开发兜底）
  8. 成功调用记一行 JSON 日志（时间、耗时、ok、key 来源，不含 key）
  9. 缺 key 的 400 也记一行日志（reason=missing_key）
  10. 空消息报 400
  11. 导入 server 不牵连 PySide6（网页版不依赖 GUI）

Run:  python test_server.py
"""

import contextlib
import io
import json
import os
import sys
import tempfile

from fastapi.testclient import TestClient

from jarvis.ui import server
from jarvis.memory.memory import FactStore
from test_stream import FakeStreamClient, _chunk, _tool_delta

_real_get_client = server.get_client  # 缺 key 测试时恢复成真的 get_client


def _parse_sse(text):
    events = []
    for block in text.strip().split("\n\n"):
        event = data = None
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:"):].strip())
        if event is not None:
            events.append((event, data))
    return events


def _client_with(script):
    # 用临时事实库，避免测试写进项目里的 memory.json
    tmp = tempfile.mkdtemp()
    server.STORE = FactStore(path=os.path.join(tmp, "memory.json"))
    server.get_client = lambda api_key=None: FakeStreamClient(script)
    return TestClient(server.app)


def _client_recording(script):
    # 记录传进 get_client 的 key，验证请求自带的 key 真被用上了
    tmp = tempfile.mkdtemp()
    server.STORE = FactStore(path=os.path.join(tmp, "memory.json"))
    seen = {}

    def fake_get_client(api_key=None):
        seen["api_key"] = api_key
        return FakeStreamClient(script)

    server.get_client = fake_get_client
    return TestClient(server.app), seen


def test_no_pyside():
    assert "PySide6" not in sys.modules, "网页版不应导入 PySide6"
    print("[OK] 网页版：import jarvis.ui.server 不牵连 PySide6")


def test_index_and_health():
    client = _client_with([])
    resp = client.get("/")
    assert resp.status_code == 200, resp.status_code
    assert "Jarvis" in resp.text and "/api/chat" in resp.text, resp.text[:200]
    assert client.get("/api/health").json() == {"ok": True}
    print("[OK] 网页版：GET / 返回聊天页，/api/health 正常")


def test_chat_stream_text():
    client = _client_with([
        [_chunk("你好"), _chunk("，我是 Jarvis。")],
    ])
    resp = client.post("/api/chat", json={
        "message": "你好", "history": [], "facts": [], "api_key": "sk-test"})
    assert resp.status_code == 200, resp.text[:200]
    events = _parse_sse(resp.text)
    tokens = [d["text"] for e, d in events if e == "token"]
    assert tokens == ["你好", "，我是 Jarvis。"], events
    done = [d for e, d in events if e == "done"]
    assert len(done) == 1, events
    assert done[0]["answer"] == "你好，我是 Jarvis。", done
    roles = [m["role"] for m in done[0]["history"]]
    assert roles == ["user", "assistant"], roles
    assert done[0]["history"][1]["content"] == "你好，我是 Jarvis。", done
    print("[OK] 网页版：token 按顺序流出，done 带回答、历史和事实")


def test_chat_tool_call():
    client = _client_with([
        [
            _chunk(tool_calls=[_tool_delta(0, "call_1", "calculator",
                                           '{"expression": "12 *')]),
            _chunk(tool_calls=[_tool_delta(0, None, None, ' (3 + 4)"}')]),
        ],
        [_chunk("12*(3+4) 等于 84。")],
    ])
    resp = client.post("/api/chat", json={
        "message": "12*(3+4) 等于多少？", "history": [], "facts": [],
        "api_key": "sk-test"})
    assert resp.status_code == 200, resp.text[:200]
    events = _parse_sse(resp.text)
    tools = [d for e, d in events if e == "tool"]
    assert len(tools) == 1 and "calculator" in tools[0]["line"], events
    done = [d for e, d in events if e == "done"]
    assert len(done) == 1 and "84" in done[0]["answer"], events
    print("[OK] 网页版：工具调用事件发出，calculator 正常执行")


def test_chat_uses_provided_key():
    client, seen = _client_recording([
        [_chunk("你好")],
    ])
    resp = client.post("/api/chat", json={
        "message": "你好", "history": [], "facts": [], "api_key": "sk-test-123"})
    assert resp.status_code == 200, resp.text[:200]
    assert seen.get("api_key") == "sk-test-123", seen
    print("[OK] 网页版：请求里带的 key 真被用来建 client 了")


def test_chat_missing_key():
    tmp = tempfile.mkdtemp()
    server.STORE = FactStore(path=os.path.join(tmp, "memory.json"))
    server.get_client = _real_get_client
    server._client = None
    old = os.environ.pop("DEEPSEEK_API_KEY", None)
    try:
        client = TestClient(server.app)
        resp = client.post("/api/chat", json={
            "message": "你好", "history": [], "facts": []})
        assert resp.status_code == 400, resp.status_code
        assert "API key" in resp.json()["detail"], resp.text
    finally:
        if old is not None:
            os.environ["DEEPSEEK_API_KEY"] = old
    print("[OK] 网页版：没带 key 且环境里也没有时报 400，提示填 key")


def test_chat_env_key_fallback():
    os.environ["DEEPSEEK_API_KEY"] = "sk-env-456"
    try:
        client, seen = _client_recording([
            [_chunk("你好")],
        ])
        resp = client.post("/api/chat", json={
            "message": "你好", "history": [], "facts": []})
        assert resp.status_code == 200, resp.text[:200]
        assert seen.get("api_key") is None, seen  # 没带 key：走环境 key 的路
    finally:
        os.environ.pop("DEEPSEEK_API_KEY", None)
    print("[OK] 网页版：没带 key 时用环境里的 key（本地开发兜底）")


def _chat_logs(text):
    return [json.loads(line) for line in text.splitlines()
            if line.strip().startswith('{"event": "chat"')]


def test_chat_logs_success():
    client, _seen = _client_recording([
        [_chunk("你好")],
    ])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        resp = client.post("/api/chat", json={
            "message": "你好", "history": [], "facts": [], "api_key": "sk-test-123"})
    assert resp.status_code == 200, resp.text[:200]
    logs = _chat_logs(buf.getvalue())
    assert len(logs) == 1, logs
    log = logs[0]
    assert log["ok"] is True and log["reason"] == "ok", log
    assert log["key_source"] == "byok", log
    assert isinstance(log["duration_ms"], int) and log["duration_ms"] >= 0, log
    assert "ts" in log and "sk-test-123" not in buf.getvalue(), log
    print("[OK] 网页版：成功调用记一行 JSON 日志（时间、耗时、ok、key 来源，不含 key）")


def test_chat_logs_missing_key():
    tmp = tempfile.mkdtemp()
    server.STORE = FactStore(path=os.path.join(tmp, "memory.json"))
    server.get_client = _real_get_client
    server._client = None
    old = os.environ.pop("DEEPSEEK_API_KEY", None)
    try:
        client = TestClient(server.app)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            resp = client.post("/api/chat", json={
                "message": "你好", "history": [], "facts": []})
        assert resp.status_code == 400, resp.status_code
        logs = _chat_logs(buf.getvalue())
        assert len(logs) == 1, logs
        assert logs[0]["ok"] is False, logs
        assert logs[0]["reason"] == "missing_key", logs
        assert logs[0]["key_source"] == "none", logs
    finally:
        if old is not None:
            os.environ["DEEPSEEK_API_KEY"] = old
    print("[OK] 网页版：缺 key 的 400 也记一行日志（reason=missing_key）")


def test_chat_empty_message():
    client = _client_with([])
    resp = client.post("/api/chat", json={"message": "  ", "history": [], "facts": []})
    assert resp.status_code == 400, resp.status_code
    print("[OK] 网页版：空消息报 400")


def test_chat_done_has_usage():
    """P7-4：done 事件带用量（tokens/费用）和耗时，页面用量面板的数据来源。"""
    client = _client_with([
        [_chunk("你好"), _chunk("，我是 Jarvis。")],
    ])
    resp = client.post("/api/chat", json={
        "message": "你好", "history": [], "facts": [], "api_key": "sk-test"})
    assert resp.status_code == 200, resp.text[:200]
    events = _parse_sse(resp.text)
    done = [d for e, d in events if e == "done"]
    assert len(done) == 1, events
    usage = done[0].get("usage")
    assert usage is not None, done[0]
    assert isinstance(usage.get("tokens"), int), usage
    assert isinstance(usage.get("cost_cny"), (int, float)), usage
    assert isinstance(done[0].get("duration_ms"), int), done[0]
    print("[OK] 网页版 P7：done 事件带用量 tokens/费用和耗时毫秒")


def test_index_has_p7_ui():
    """P7-4：聊天页里有会话下拉框、撤销、时间线、用量面板和 md2html。"""
    client = _client_with([])
    html = client.get("/").text
    for needle in ('id="sessions"', 'id="newsession"', 'id="rewind"',
                   'id="timeline"', 'id="usage"', 'id="trajectory"',
                   'md2html', 'jarvis-sessions'):
        assert needle in html, needle
    print("[OK] 网页版 P7：页面有会话/撤销/时间线/用量面板和 Markdown 渲染")


if __name__ == "__main__":
    test_no_pyside()
    test_index_and_health()
    test_chat_stream_text()
    test_chat_tool_call()
    test_chat_uses_provided_key()
    test_chat_missing_key()
    test_chat_env_key_fallback()
    test_chat_logs_success()
    test_chat_logs_missing_key()
    test_chat_empty_message()
    test_chat_done_has_usage()
    test_index_has_p7_ui()
    print("全部通过。")
