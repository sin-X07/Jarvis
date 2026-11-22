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
from types import SimpleNamespace

from fastapi.testclient import TestClient

from jarvis.ui import server
from jarvis.memory.memory import FactStore
from test_stream import FakeStreamClient, _chunk, _tool_delta
from test_loop import _text_reply, _tool_call


class _HybridFakeClient:
    """plan 轮非流式 + 执行轮流式：非流式 create 按 plan_replies 顺序
    返回回答（可带工具调用），流式 create 按 script 顺序出 chunk。"""
    def __init__(self, plan_replies, script):
        self._plan_replies = list(plan_replies)
        self._script = list(script)
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create))

    def _create(self, stream=False, **kwargs):
        if not stream:
            return self._plan_replies.pop(0)
        return iter(self._script.pop(0))


def _client_with_hybrid(plan_replies, script):
    tmp = tempfile.mkdtemp()
    server.STORE = FactStore(path=os.path.join(tmp, "memory.json"))
    server.get_client = lambda _k=None: _HybridFakeClient(plan_replies, script)
    return TestClient(server.app)

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


def test_index_empty_state_from_viewstate():
    """空态文案走 viewstate（U7 验收补修）：页面里出现 EMPTY_HINTS 的例句。"""
    from jarvis.ui.viewstate import EMPTY_HINTS, EMPTY_TAGLINE
    client = _client_with([])
    resp = client.get("/")
    assert resp.status_code == 200
    assert EMPTY_TAGLINE in resp.text, resp.text[:500]
    assert EMPTY_HINTS[0] in resp.text, "空态文案未从 viewstate 注入"
    print("[OK] 网页版：空态文案来自 viewstate（与 TUI/GUI 同一份）")


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
    assert len(tools) == 2, events
    assert tools[0]["line"] == "▸ 算 12 * (3 + 4)", tools  # tool_start 一行摘要
    assert tools[1]["line"].startswith("✓ calculator 已返回"), tools  # tool_end 结算
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


def test_index_has_u1_ui():
    """U1：页面有侧栏、模式/模型芯片、提示画布、断线横幅、设置区、
    工具卡片与代码复制、排队提示、context 条、textarea 输入框。"""
    client = _client_with([])
    html = client.get("/").text
    for needle in ('id="sidebar"', 'id="sessions"', 'id="search"',
                   'id="mode-chip"', 'id="model-chip"',
                   'id="empty"', 'id="suggestions"',
                   'id="disconnect"', 'id="reconnect"',
                   'id="settings"', 'id="settings-toggle"',
                   'id="queued"', 'id="ctxfill"',
                   'toolcard', 'copy-code', 'msg-actions',
                   '<textarea id="input"', 'autogrow'):
        assert needle in html, needle
    print("[OK] 网页版 U1：侧栏/芯片/画布/横幅/设置/卡片/排队/context 条都在")


def test_index_has_u6_ui():
    """U6：页面有快捷键总览面板 + 错误重试一次逻辑 + 统一空状态文案。"""
    client = _client_with([])
    html = client.get("/").text
    for needle in ('id="shortcuts"', 'id="shortcuts-toggle"', '⌨ 快捷键',
                   'errorStreak', 'hintFor', '复制问题，开新会话',
                   '能记住事情', 'function newSession'):
        assert needle in html, needle
    print("[OK] 网页版 U6：快捷键面板 / 重试一次 / 统一空状态都在")


def test_index_uses_theme_vars():
    """U1：颜色全部走 theme.json 的 CSS 变量；十六进制值只出现在
    :root 变量定义里（生成器注入），组件样式里没有硬编码颜色。"""
    import re
    client = _client_with([])
    html = client.get("/").text
    assert "--color-bg: #1e1e2e;" in html, "主题变量没注入"
    assert "--color-codeBg: #11111b;" in html
    assert "--color-bg: #eff1f5;" in html, "浅色主题变量没注入"
    assert "var(--color-bg)" in html and "var(--color-accent)" in html
    assert "deepseek-flash" in html, "模型芯片应显示模型 id"
    # 两份变量块（深色 :root + 浅色 html[data-theme="light"]）都是
    # 生成器注入的 token，剥掉再查；组件样式里不应有硬编码颜色。
    rest = re.sub(r":root \{.*?\}", "", html, flags=re.S)
    rest = re.sub(r"html\[data-theme=\"light\"\] \{.*?\}", "", rest,
                  flags=re.S)
    assert not re.search(r"#[0-9a-fA-F]{6}", rest), "变量块外还有硬编码颜色"
    print("[OK] 网页版 U1：颜色走 CSS 变量，硬编码只剩 :root 变量定义")


def test_chat_status_and_thinking_events():
    """U1：每次模型调用后发 status 事件（真 token 数）；思考流发
    thinking 事件。"""
    client = _client_with([
        [_chunk(reasoning="先想想…"), _chunk("答案。")],
    ])
    resp = client.post("/api/chat", json={
        "message": "想想", "history": [], "facts": [], "api_key": "sk-test"})
    assert resp.status_code == 200, resp.text[:200]
    events = _parse_sse(resp.text)
    thinking = [d["text"] for e, d in events if e == "thinking"]
    assert thinking == ["先想想…"], events
    status = [d for e, d in events if e == "status"]
    assert len(status) == 1, events
    assert status[0]["model"] == "deepseek-flash"
    assert status[0]["calls"] == 1 and "total_tokens" in status[0]
    assert "unknown_calls" in status[0] and "context_pct" in status[0]
    print("[OK] 网页版 U1：thinking 与 status 事件发出，含真 token 字段")


PLAN_TEXT = ("目标：写个问候脚本\n步骤：\n"
             "1. 写文件 —— 验收：文件存在\n"
             "2. 跑一遍 —— 验收：输出问候语\n风险：\n- 无\n")


def test_chat_plan_mode_blocks_write():
    """U4-1：mode=plan 只出计划（make_plan 只读调研）：先发 plan 事件，
    done 带 plan_only；模型若中途想调写工具，当场被拦（拒绝理由进
    tool end 的 result_preview），计划文本照常解析。"""
    client = _client_with_hybrid([
        _tool_call("call_1", "write_text_file",
                   '{"path": "a.txt", "content": "hi"}'),
        _text_reply(PLAN_TEXT),
    ], [])
    resp = client.post("/api/chat", json={
        "message": "写个问候脚本", "history": [], "facts": [],
        "api_key": "sk-test", "mode": "plan"})
    assert resp.status_code == 200, resp.text[:200]
    events = _parse_sse(resp.text)
    plans = [d for e, d in events if e == "plan"]
    assert len(plans) == 1, events
    assert plans[0]["goal"] == "写个问候脚本", plans
    assert [s["title"] for s in plans[0]["steps"]] == ["写文件", "跑一遍"], plans
    ends = [d for e, d in events if e == "tool" and d["phase"] == "end"]
    assert ends and "规划模式" in ends[0]["result_preview"], events
    done = [d for e, d in events if e == "done"]
    assert len(done) == 1 and done[0]["plan_only"] is True, events
    assert done[0]["answer"] == "", done
    print("[OK] 网页版 U4：规划模式发 plan 事件，写工具被拦，done 带 plan_only")


def test_chat_execute_runs_plan_and_ticks_todos():
    """U4-2：mode=execute 按批准的计划执行，update_todo 变动发 todo
    事件（整表重渲染用），先 ☐/◐ 后逐项变 ☑，最后有回答。"""
    client = _client_with_hybrid([], [
        [_chunk(tool_calls=[_tool_delta(0, "call_1", "update_todo",
                                        '{"title": "写文件"}')])],
        [_chunk(tool_calls=[_tool_delta(0, "call_2", "update_todo",
                                        '{"title": "跑一遍"}')])],
        [_chunk("做完了")],
    ])
    resp = client.post("/api/chat", json={
        "message": "写个问候脚本", "history": [], "facts": [],
        "api_key": "sk-test", "mode": "execute",
        "plan": {"goal": "写个问候脚本",
                 "steps": [{"title": "写文件", "acceptance": "文件存在"},
                           {"title": "跑一遍", "acceptance": "输出问候语"}],
                 "risks": ["无"]}})
    assert resp.status_code == 200, resp.text[:200]
    events = _parse_sse(resp.text)
    todos = [d["todos"] for e, d in events if e == "todo"]
    assert len(todos) >= 3, events
    assert [t["status"] for t in todos[0]] == ["active", "pending"], todos
    assert [t["status"] for t in todos[-1]] == ["done", "done"], todos
    assert todos[0][0]["content"] == "写文件", todos
    done = [d for e, d in events if e == "done"]
    assert len(done) == 1 and done[0]["answer"] == "做完了", events
    print("[OK] 网页版 U4：执行已批准计划，todo 事件按步骤推进到全 ☑")


def test_chat_execute_with_edited_plan_markdown():
    """U4-1：批准时改过的计划文本（plan_markdown）优先，待办按改后建。"""
    client = _client_with_hybrid([], [
        [_chunk("按改后的计划做完了")],
    ])
    resp = client.post("/api/chat", json={
        "message": "写个问候脚本", "history": [], "facts": [],
        "api_key": "sk-test", "mode": "execute",
        "plan": {"goal": "写个问候脚本",
                 "steps": [{"title": "写文件", "acceptance": ""}],
                 "risks": []},
        "plan_markdown": "目标：写个问候脚本\n步骤：\n"
                         "1. 改后的步骤 —— 验收：改后验收\n风险：\n- 无\n"})
    assert resp.status_code == 200, resp.text[:200]
    events = _parse_sse(resp.text)
    todos = [d["todos"] for e, d in events if e == "todo"]
    assert todos and todos[0][0]["content"] == "改后的步骤", todos
    assert todos[0][0]["acceptance"] == "改后验收", todos
    print("[OK] 网页版 U4：改过的计划文本优先建待办")


def test_chat_execute_emits_verify():
    """U7：mode=execute 执行完发 verify 事件（验收报告），验收轮的
    token 也进 done 的用量；验收失败时 verify 带 error、done 照常发。"""
    client = _client_with_hybrid([
        _text_reply("1. 写文件：通过\n2. 跑一遍：通过",
                    usage=SimpleNamespace(prompt_tokens=20,
                                          completion_tokens=8,
                                          total_tokens=28)),
    ], [
        [_chunk("做完了")],
    ])
    resp = client.post("/api/chat", json={
        "message": "写个问候脚本", "history": [], "facts": [],
        "api_key": "sk-test", "mode": "execute",
        "plan": {"goal": "写个问候脚本",
                 "steps": [{"title": "写文件", "acceptance": "文件存在"},
                           {"title": "跑一遍", "acceptance": "输出问候语"}],
                 "risks": ["无"]}})
    assert resp.status_code == 200, resp.text[:200]
    events = _parse_sse(resp.text)
    verify = [d for e, d in events if e == "verify"]
    assert len(verify) == 1 and "通过" in verify[0]["report"], events
    done = [d for e, d in events if e == "done"]
    assert len(done) == 1 and done[0]["answer"] == "做完了", events
    assert done[0]["usage"]["tokens"] == 28, done[0]["usage"]
    print("[OK] 网页版 U7：执行完发 verify 事件，验收轮 token 进用量")


def test_chat_plan_only_usage_counts_plan_tokens():
    """U7：mode=plan 的 done 用量不再硬编码 0，计划轮的真实用量进统计。"""
    client = _client_with_hybrid([
        _text_reply(PLAN_TEXT,
                    usage=SimpleNamespace(prompt_tokens=10,
                                          completion_tokens=5,
                                          total_tokens=15)),
    ], [])
    resp = client.post("/api/chat", json={
        "message": "写个问候脚本", "history": [], "facts": [],
        "api_key": "sk-test", "mode": "plan"})
    assert resp.status_code == 200, resp.text[:200]
    events = _parse_sse(resp.text)
    done = [d for e, d in events if e == "done"]
    assert len(done) == 1 and done[0]["plan_only"] is True, events
    assert done[0]["usage"]["tokens"] == 15, done[0]["usage"]
    assert done[0]["usage"]["prompt_tokens"] == 10, done[0]["usage"]
    print("[OK] 网页版 U7：plan_only 的 done 用量是计划轮的真实 token")


def test_api_diff():
    """U5-3：GET /api/diff 返回按文件分组的改动；非 git 目录给提示。"""
    from test_diffutil import GIT_DIFF
    import jarvis.ui.server as srv

    old = srv.working_tree_diff
    srv.working_tree_diff = lambda: GIT_DIFF
    try:
        resp = TestClient(server.app).get("/api/diff")
    finally:
        srv.working_tree_diff = old
    assert resp.status_code == 200, resp.status_code
    data = resp.json()
    assert [f["path"] for f in data["files"]] == ["a.py", "b.py"], data
    assert data["files"][0]["summary"] == "+1 行 / -1 行", data
    assert data["files"][1]["added"] == 2, data
    assert data["note"] == "", data
    srv.working_tree_diff = lambda: "当前目录不是一个 git 仓库。"
    try:
        resp = TestClient(server.app).get("/api/diff")
    finally:
        srv.working_tree_diff = old
    assert resp.json() == {"files": [], "note": "当前目录不是一个 git 仓库。"}
    print("[OK] 网页版 U5：GET /api/diff 返回按文件分组的改动")


def test_chat_tool_card_phases():
    """U1-1：tool 事件分 start/end 两段，start 带摘要与风险，end 带
    状态与耗时（前端据此画工具卡片）。"""
    client = _client_with([
        [
            _chunk(tool_calls=[_tool_delta(0, "call_1", "calculator",
                                           '{"expression": "12 *')]),
            _chunk(tool_calls=[_tool_delta(0, None, None, ' (3 + 4)"}')]),
        ],
        [_chunk("12*(3+4) 等于 84。")],
    ])
    resp = client.post("/api/chat", json={
        "message": "12*(3+4) 等于多少？", "history": [], "facts": [], "api_key": "sk-test"})
    assert resp.status_code == 200, resp.text[:200]
    events = _parse_sse(resp.text)
    tools = [d for e, d in events if e == "tool"]
    assert [t["phase"] for t in tools] == ["start", "end"], tools
    assert tools[0]["line"] == "▸ 算 12 * (3 + 4)", tools
    assert tools[0]["risk"] == "read", tools
    assert tools[1]["status"] == "ok" and tools[1]["duration_ms"] >= 0, tools
    assert "result_preview" in tools[1], tools
    print("[OK] 网页版 U1：tool 事件分段，含摘要/风险/状态/耗时/输出预览")


if __name__ == "__main__":
    test_no_pyside()
    test_index_and_health()
    test_index_empty_state_from_viewstate()
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
    test_index_has_u1_ui()
    test_index_uses_theme_vars()
    test_chat_status_and_thinking_events()
    test_chat_plan_mode_blocks_write()
    test_chat_execute_runs_plan_and_ticks_todos()
    test_chat_execute_with_edited_plan_markdown()
    test_chat_execute_emits_verify()
    test_chat_plan_only_usage_counts_plan_tokens()
    test_api_diff()
    test_chat_tool_card_phases()
    test_index_has_u6_ui()
    print("全部通过。")
