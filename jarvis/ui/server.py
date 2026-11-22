"""Jarvis web server: the same agent loop, over HTTP + SSE.

Same core as the desktop GUI (agent.py / tools.py / memory.py), different
shell: the browser POSTs to /api/chat and reads Server-Sent Events back.
No PySide6 needed here -- this is the part Vercel runs.

Run locally:
    uvicorn jarvis.ui.server:app --reload
Then open:
    http://127.0.0.1:8000/

State: the server keeps no conversation between requests. The browser
owns the history and the facts (localStorage) and sends them with every
request; the server merges them, runs one agent loop, and returns the
updated history/facts in the final `done` event. That is what makes this
deployable on stateless serverless (Vercel): no session to lose, and the
long-term facts survive cold starts because the browser re-seeds them.

Key (BYOK): the server has no key of its own on Vercel. Each visitor
types their own DeepSeek API key into the page; the browser keeps it in
localStorage and sends it with every request. The server uses that key
for the one request only -- it is never stored, cached or logged. With
no key in the request, the server falls back to DEEPSEEK_API_KEY from
the environment (handy for local dev); with neither, it answers 400.

Logging: one JSON line per /api/chat call (time, duration, ok/error,
key source). No message content, history, facts or keys -- see _log_chat.
"""

import json
import os
import queue
import threading
import time
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from jarvis.core.agent import MODEL, SYSTEM_PROMPT, build_client, run_agent
from jarvis.memory.memory import ConversationMemory, FactStore, memory_tools
from jarvis.core.usage import UsageTracker
from jarvis.safety.diffutil import parse_diff
from jarvis.tools.tools import working_tree_diff
from jarvis.ui.events import summarise_tool_call, tool_risk
from jarvis.ui.theme import load_theme, to_css_vars
from jarvis.ui.viewstate import EMPTY_HINTS, EMPTY_TAGLINE

# 网页版默认最多 6 步：Vercel 免费版函数最长跑 60 秒，步数太多容易超时。
WEB_MAX_STEPS = 6
# 历史上限（条消息数，不含 system），和 ConversationMemory 的默认一致。
HISTORY_LIMIT = 30

app = FastAPI(title="Jarvis")

# 进程内的事实库：启动时从 memory.json 读（本地有的话），运行中新增的
# 事实尽量写盘；写盘失败（Vercel 只读环境）也不报错，见 memory.py。
STORE = FactStore()

_client = None


def get_client(api_key=None):
    """Client for one request. A caller-provided key wins (BYOK) and is
    never cached: it lives only for this request. Otherwise fall back to
    the cached client built from DEEPSEEK_API_KEY (local dev)."""
    if api_key:
        return build_client(api_key=api_key)
    global _client
    if _client is None:
        _client = build_client()
    return _client


class ChatRequest(BaseModel):
    message: str
    history: list = []  # 浏览器攒的对话：user/assistant/tool 消息，不含 system
    facts: list = []    # 浏览器 localStorage 里的长期事实
    api_key: str = ""   # 访客自己的 DeepSeek key：只用本轮请求，不存不记日志
    mode: str = "chat"  # "chat" 对话 / "plan" 规划（只出计划）/ "execute" 执行已批准的计划
    plan: dict | None = None  # execute 时批准的计划 {goal, steps, risks}
    plan_markdown: str | None = None  # 用户在卡片里改过的计划文本（有就按它解析）


def _system_prompt() -> str:
    facts = STORE.render_for_prompt()
    return f"{SYSTEM_PROMPT}\n{facts}" if facts else SYSTEM_PROMPT


def _run_agent_streamed(client, user_message, messages, schemas, funcs,
                        mode="chat", plan=None, plan_markdown=None):
    """Run the loop in a thread; events come back through a queue.

    run_agent is synchronous and calls on_event as it goes. To forward
    those events to the browser while they happen, the loop runs in a
    worker thread and the SSE generator yields from the queue.
    mode="plan"：只读调研出计划（make_plan），发 plan_ready 事件，不执行；
    mode="execute"：按已批准的计划执行，update_todo 变动发 todo_set 事件，
        执行完调 verify_plan 对照验收标准检查，发 verify 事件；
    mode="chat"：普通对话。计划轮与验收轮的 token 都进 usage（U7：
        make_plan/verify_plan 现在接 UsageTracker）；done 里 plan_only
        时用量行按计划轮的真实用量刷新，不再硬编码 0。
    """
    events = queue.Queue()

    def on_event(kind, payload):
        events.put((kind, payload))

    def target():
        started = time.monotonic()
        tracker = UsageTracker()
        try:
            if mode == "plan":
                from jarvis.memory.planner import make_plan
                plan_obj = make_plan(
                    client, MODEL, user_message,
                    extra_tools=schemas, extra_funcs=funcs,
                    max_steps=WEB_MAX_STEPS, on_event=on_event,
                    usage=tracker)
                events.put(("plan_ready", {
                    "markdown": plan_obj.render(), "goal": plan_obj.goal,
                    "steps": plan_obj.steps, "risks": plan_obj.risks}))
                events.put(("done", {
                    "answer": "", "plan_only": True,
                    "history": messages[1:], "facts": STORE.all_texts(),
                    "usage": {"tokens": tracker.total_tokens,
                              "prompt_tokens": tracker.prompt_tokens,
                              "completion_tokens": tracker.completion_tokens,
                              "unknown_calls": tracker.unknown_calls,
                              "cost_cny": round(tracker.cost_cny, 4)},
                    "duration_ms": round((time.monotonic() - started) * 1000),
                }))
                return
            if mode == "execute":
                from jarvis.memory.planner import Plan, parse_plan
                from jarvis.memory.todo import (TodoList, attach_todo_tools)
                if plan_markdown:
                    plan_obj = parse_plan(plan_markdown)
                else:
                    plan_obj = Plan(
                        goal=(plan or {}).get("goal", ""),
                        steps=(plan or {}).get("steps", []),
                        risks=(plan or {}).get("risks", []))
                todo = plan_obj.to_todos()
                attach_todo_tools(todo, schemas, funcs)
                todo.on_change = lambda: events.put(
                    ("todo_set", {"todos": todo.to_event()}))
                events.put(("todo_set", {"todos": todo.to_event()}))
                exec_message = (
                    "按顺序执行下面的计划，每完成一步调 update_todo 更新进度。"
                    f"\n\n{plan_obj.render()}\n\n任务：{user_message}")
            else:
                exec_message = user_message
            answer = run_agent(
                client, exec_message, history=messages,
                extra_tools=schemas, extra_funcs=funcs,
                on_event=on_event, stream=True, verbose=False,
                max_steps=WEB_MAX_STEPS, usage=tracker,
            )
            if mode == "execute":
                # U7：执行完对照验收标准检查（以前网页版从没验过）。
                # 验收失败不拦本轮：done 照常发，verify 事件带 error。
                try:
                    from jarvis.memory.planner import verify_plan
                    report = verify_plan(
                        client, MODEL, plan_obj, answer, usage=tracker)
                    events.put(("verify", {"report": report}))
                except Exception as exc:  # noqa: BLE001
                    events.put(("verify", {"report": "",
                                           "error": str(exc)}))
            # 整轮裁剪后把更新过的历史（不含 system）交给 done 事件
            mem = ConversationMemory(_system_prompt())
            mem.messages = messages
            mem.trim()
            events.put(("done", {
                "answer": answer,
                "history": mem.messages[1:],
                "facts": STORE.all_texts(),
                "usage": {"tokens": tracker.total_tokens,
                          "prompt_tokens": tracker.prompt_tokens,
                          "completion_tokens": tracker.completion_tokens,
                          "unknown_calls": tracker.unknown_calls,
                          "cost_cny": round(tracker.cost_cny, 4)},
                "duration_ms": round((time.monotonic() - started) * 1000),
            }))
        except Exception as exc:  # noqa: BLE001 - report to the browser, don't kill the stream
            events.put(("error", {"message": str(exc)}))

    # 注意：run_agent(history=...) 会把本轮问题追加进 messages，所以这里
    # 传的 messages 里不要提前放 user 消息。
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    while True:
        kind, payload = events.get()
        yield kind, payload
        if kind in ("done", "error"):
            break


def _sse_event(kind: str, payload) -> str:
    return f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _log_chat(duration_ms, ok, reason, key_source):
    """One JSON line per /api/chat call: time, duration, success/failure.

    Deliberately minimal: no message content, no history, no facts, and
    never the API key. Vercel shows these lines in Runtime Logs; locally
    they appear in the uvicorn terminal. One line per call, so they are
    easy to grep and count.
    """
    print(json.dumps({
        "event": "chat",
        "ts": datetime.now(timezone.utc).isoformat(),
        "duration_ms": round(duration_ms),
        "ok": ok,
        "reason": reason,
        "key_source": key_source,
    }, ensure_ascii=False), flush=True)


# 聊天页以内嵌字符串放在这里（不再单独放 index.html 文件）：
# 本地 uvicorn 和 Vercel 都直接由 GET / 返回它，不用另配静态托管。
# 想拆成独立的 index.html 也很容易：把这段存成文件，GET / 改成 FileResponse。
INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jarvis</title>
<style>
  /*__THEME_VARS__*/
  /*__THEME_VARS_LIGHT__*/
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--color-bg); color: var(--color-text);
         font: 15px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif; }
  #layout { display: flex; height: 100vh; }
  #app { flex: 1; min-width: 0; max-width: 860px; margin: 0 auto; height: 100vh;
         display: flex; flex-direction: column; padding: 16px; }
  header { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  h1 { font-size: 20px; margin: 0; color: var(--color-accent); }
  .chip { background: var(--color-surfaceRaised); color: var(--color-text);
          border: 1px solid var(--color-border); border-radius: 999px;
          padding: 2px 10px; font-size: 13px; white-space: nowrap; }
  button.chip { cursor: pointer; font: inherit; }
  button.chip:hover { border-color: var(--color-accent); }
  #status { color: var(--color-textMuted); font-size: 13px; margin-left: auto; }
  #chat { flex: 1; overflow-y: auto; margin: 12px 0; }
  .msg { position: relative; margin: 8px 0; padding: 8px 12px; border-radius: 10px;
         white-space: pre-wrap; word-break: break-word; }
  .user { background: var(--color-surfaceRaised); margin-left: 15%; text-align: right; }
  .jarvis { background: var(--color-surface); margin-right: 15%; }
  .tool { background: none; color: var(--color-textFaint); font-style: italic;
          font-size: 13px; padding: 2px 12px; }
  .error { background: var(--color-surface); color: var(--color-danger); }
  .msg pre { background: var(--color-codeBg); padding: 8px; border-radius: 6px; overflow-x: auto; }
  .msg code { background: var(--color-codeBg); padding: 1px 4px; border-radius: 4px; }
  .msg pre code { background: none; padding: 0; }
  /* 工具卡片：折叠，状态 + 耗时；点开看参数和输出 */
  .toolcard { background: var(--color-surface); border: 1px solid var(--color-border);
              border-radius: 8px; margin: 8px 0; font-size: 13px; overflow: hidden; }
  .toolcard.read { border-left: 3px solid var(--color-info); }
  .toolcard.write { border-left: 3px solid var(--color-warning); }
  .toolcard.danger { border-left: 3px solid var(--color-danger); }
  .toolcard > summary { cursor: pointer; padding: 6px 10px; list-style: none;
              display: flex; gap: 8px; align-items: baseline; }
  .toolcard > summary::-webkit-details-marker { display: none; }
  .toolcard .t-status { color: var(--color-textMuted); white-space: nowrap; }
  .toolcard .t-dur { color: var(--color-textFaint); margin-left: auto; white-space: nowrap; }
  .toolcard .t-body { padding: 0 10px 8px; color: var(--color-textMuted); }
  .toolcard .t-body pre { background: var(--color-codeBg); padding: 8px; border-radius: 6px;
              overflow-x: auto; white-space: pre-wrap; word-break: break-word; }
  /* 思考 disclosure */
  .thinking { background: none; border: 1px dashed var(--color-border);
              border-radius: 8px; margin: 8px 0; font-size: 13px;
              color: var(--color-textMuted); }
  .thinking > summary { cursor: pointer; padding: 6px 10px; }
  .thinking > div { padding: 0 10px 8px; white-space: pre-wrap; }
  /* 计划卡片（U4-1）：目标 + 步骤 + 风险，文本可改，批准/拒绝 */
  .plancard { background: var(--color-surface); border: 1px solid var(--color-accent);
              border-radius: 8px; margin: 8px 0; padding: 10px 12px; font-size: 13px; }
  .plancard h3 { margin: 0 0 6px; font-size: 14px; }
  .plancard .p-steps { white-space: pre-wrap; color: var(--color-text); }
  .plancard .p-risks { white-space: pre-wrap; color: var(--color-textMuted); margin-top: 6px; }
  .plancard textarea { width: 100%; min-height: 120px; margin-top: 8px;
              background: var(--color-bg); color: var(--color-text);
              border: 1px solid var(--color-border); border-radius: 6px;
              font: inherit; padding: 8px; box-sizing: border-box; }
  .plancard .p-btns { display: flex; gap: 8px; margin-top: 8px; }
  .plancard .p-btns button { cursor: pointer; padding: 6px 14px; border-radius: 6px;
              border: 1px solid var(--color-border);
              background: var(--color-surfaceRaised); color: var(--color-text); font: inherit; }
  .plancard .p-btns button.primary { background: var(--color-accent); color: var(--color-onAccent); border: none; }
  /* 待办清单（U4-2）：侧栏，☐/◐/☑，整表重渲染 */
  #todos { border-top: 1px solid var(--color-border); margin-top: 8px; padding-top: 8px; }
  #todos h3 { margin: 0 0 4px; font-size: 13px; color: var(--color-textMuted); }
  #todo-list { list-style: none; margin: 0; padding: 0; font-size: 13px; }
  #todo-list li { padding: 3px 8px; white-space: pre-wrap; word-break: break-word; }
  #todo-list li.done { color: var(--color-textFaint); }
  #todo-list li.active { color: var(--color-accent); }
  /* Diff 抽屉（U5-3）：右侧滑出，按文件分组，+/- 行绿/红 */
  #diff-drawer { position: fixed; top: 0; right: 0; bottom: 0;
              width: min(480px, 92vw); background: var(--color-surface);
              border-left: 1px solid var(--color-border); z-index: 30;
              display: flex; flex-direction: column; padding: 12px;
              box-sizing: border-box; }
  #diff-drawer[hidden] { display: none; }
  #diff-drawer h3 { margin: 0 0 8px; font-size: 15px; }
  #diff-drawer #diff-btns { display: flex; gap: 8px; margin-bottom: 8px; }
  #diff-drawer #diff-btns button { cursor: pointer; padding: 6px 12px;
              border-radius: 6px; border: 1px solid var(--color-border);
              background: var(--color-surfaceRaised); color: var(--color-text);
              font: inherit; }
  #diff-content { overflow-y: auto; flex: 1; font-size: 13px; }
  #diff-content h4 { margin: 10px 0 4px; font-size: 13px; }
  #diff-content .d-line { white-space: pre-wrap; word-break: break-word;
              font-family: var(--font-mono); }
  #diff-content .d-add { color: var(--color-diffAdd); }
  #diff-content .d-del { color: var(--color-diffDel); }
  #diff-content .d-hunk { color: var(--color-textMuted); }
  #diff-content .d-note { color: var(--color-textMuted); margin-top: 8px; }
  /* 消息操作：悬停出现 */
  .msg-actions { position: absolute; top: -11px; right: 8px; display: none; gap: 4px; }
  .msg:hover .msg-actions { display: flex; }
  .msg-actions button { background: var(--color-surfaceRaised);
              color: var(--color-textMuted); border: 1px solid var(--color-border);
              border-radius: 6px; padding: 2px 8px; font-size: 12px; cursor: pointer; }
  .msg-actions button:hover { color: var(--color-text); border-color: var(--color-accent); }
  /* 代码块：语言标签 + 复制按钮 */
  .codeblock { margin: 8px 0; }
  .codehead { display: flex; justify-content: space-between; align-items: center;
              background: var(--color-codeBg); border-radius: 6px 6px 0 0; padding: 2px 8px;
              font-size: 12px; color: var(--color-textMuted); }
  .codehead button { background: none; color: var(--color-textMuted);
              border: 1px solid var(--color-border); border-radius: 4px;
              padding: 1px 8px; font-size: 12px; cursor: pointer; }
  .codehead button:hover { color: var(--color-text); }
  .codeblock pre { margin: 0; border-radius: 0 0 6px 6px; }
  /* 错误卡片 + 重试 */
  .errcard { background: var(--color-surface); border: 1px solid var(--color-danger);
             border-radius: 8px; margin: 8px 0; padding: 10px 12px; }
  .errcard .e-msg { color: var(--color-danger); }
  .errcard button { margin-top: 8px; }
  /* 断线横幅 */
  #disconnect { background: var(--color-warning); color: var(--color-bg);
                border-radius: 8px; padding: 8px 12px; margin: 8px 0;
                display: flex; gap: 8px; align-items: center; font-size: 13px; }
  #disconnect button { background: var(--color-bg); color: var(--color-warning);
                padding: 4px 12px; }
  /* 快捷键总览（U6-2） */
  #shortcuts { background: var(--color-surface); border-bottom: 1px solid var(--color-border);
               padding: 6px 10px; font-size: 13px; color: var(--color-textMuted); }
  /* 提示画布（空状态） */
  #empty { flex: 1; display: flex; flex-direction: column; align-items: center;
           justify-content: center; text-align: center; gap: 8px; padding: 24px; }
  #empty h2 { margin: 0; color: var(--color-accent); font-size: 20px; }
  #empty p { margin: 0; color: var(--color-textMuted); }
  #empty .keyhint { color: var(--color-warning); font-size: 13px; }
  #suggestions { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; }
  #suggestions button { background: var(--color-surface);
              color: var(--color-text); border: 1px solid var(--color-border);
              border-radius: 999px; padding: 6px 14px; font: inherit; cursor: pointer; }
  #suggestions button:hover { border-color: var(--color-accent); }
  /* 状态行：context 条 + 数字 */
  #usage { color: var(--color-textMuted); font-size: 13px; margin-top: 8px; }
  #usage .ctxbar { height: 6px; background: var(--color-surfaceRaised);
              border-radius: 3px; overflow: hidden; margin-top: 4px; }
  #usage .ctxfill { height: 100%; background: var(--color-accent); width: 0; }
  #usage .ctxfill.warn { background: var(--color-warning); }
  #usage .ctxfill.hot { background: var(--color-danger); }
  /* 会话侧栏 */
  #sidebar { width: 240px; flex-shrink: 0; background: var(--color-surface);
             border-right: 1px solid var(--color-border); display: flex;
             flex-direction: column; padding: 12px; gap: 8px; overflow: hidden; }
  #layout.noside #sidebar { display: none; }
  .side-head { display: flex; justify-content: space-between; align-items: center;
               font-weight: bold; }
  .side-head button, #expand-side { background: none; color: var(--color-textMuted);
               border: none; font-size: 15px; cursor: pointer; padding: 4px 8px; }
  #search { background: var(--color-bg); color: var(--color-text);
            border: 1px solid var(--color-border); border-radius: 6px;
            padding: 6px 8px; font: inherit; }
  #sessions { list-style: none; margin: 0; padding: 0; overflow-y: auto; flex: 1; }
  #sessions li { display: flex; gap: 4px; align-items: center; padding: 6px 8px;
                 border-radius: 6px; cursor: pointer; font-size: 13px; }
  #sessions li:hover { background: var(--color-surfaceRaised); }
  #sessions li.active { background: var(--color-surfaceRaised);
                 outline: 1px solid var(--color-accent); }
  #sessions li .t { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #sessions li .n { color: var(--color-textFaint); font-size: 12px; }
  #sessions li .mini { background: none; border: none; color: var(--color-textFaint);
                 cursor: pointer; padding: 0 2px; font-size: 13px; display: none; }
  #sessions li:hover .mini { display: inline; }
  #sessions li .mini:hover { color: var(--color-text); }
  #settings-toggle { background: none; color: var(--color-textMuted);
              border: 1px solid var(--color-border); border-radius: 6px;
              padding: 6px; cursor: pointer; font: inherit; text-align: left; }
  /* composer */
  #row { display: flex; gap: 8px; align-items: flex-end; }
  #input { flex: 1; background: var(--color-surface); color: var(--color-text);
           border: 1px solid var(--color-border); border-radius: 8px; padding: 10px;
           font: inherit; resize: none; line-height: 1.6; max-height: 154px; }
  button { background: var(--color-accent); color: var(--color-bg); border: none;
           border-radius: 8px; padding: 10px 18px; font: inherit;
           cursor: pointer; }
  button:disabled { background: var(--color-surfaceDisabled); color: var(--color-textMuted); cursor: default; }
  #send.stop { background: var(--color-danger); color: var(--color-bg); }
  #clear { background: none; color: var(--color-textMuted); padding: 10px 8px; }
  #queued { color: var(--color-warning); font-size: 13px; margin-top: 6px; }
  #keybar { display: flex; gap: 8px; margin-top: 10px; align-items: center; }
  #key { flex: 1; background: var(--color-surface); color: var(--color-text);
         border: 1px solid var(--color-border); border-radius: 8px; padding: 8px;
         font: inherit; }
  #keybar button { padding: 8px 12px; }
  #keystatus { color: var(--color-textMuted); font-size: 13px; white-space: nowrap; }
  .hint { color: var(--color-textMuted); font-size: 13px; margin: 6px 0 0; }
  #toolbar { display: flex; gap: 8px; margin-top: 10px; align-items: center; flex-wrap: wrap; }
  #toolbar button { padding: 8px 12px; }
  #trajectory { background: var(--color-surface); border: 1px solid var(--color-border); border-radius: 8px;
         padding: 8px; margin-top: 8px; font-size: 13px; color: var(--color-textMuted);
         max-height: 160px; overflow-y: auto; }
  @media (max-width: 640px) {
    #app { padding: 10px; }
    .user { margin-left: 5%; }
    .jarvis { margin-right: 5%; }
    #sidebar { position: fixed; left: 0; top: 0; bottom: 0; z-index: 10;
               box-shadow: 4px 0 16px rgba(0, 0, 0, 0.4); }
    #layout.noside #sidebar { display: none; }
    header { gap: 6px; }
    #status { margin-left: 0; width: 100%; }
    button { padding: 10px 12px; }
  }
</style>
</head>
<body>
<div id="layout">
  <aside id="sidebar">
    <div class="side-head"><span>会话</span><button id="collapse-side" title="收起侧栏">«</button></div>
    <input id="search" placeholder="搜索会话…" autocomplete="off">
    <button id="newsession">＋ 新会话</button>
    <button id="forksession" title="把当前会话另存一份（继续聊，分叉）">⑂ 分叉</button>
    <ul id="sessions"></ul>
    <div id="todos"><h3>待办</h3><ul id="todo-list"><li>（清单是空的，规划模式出计划后出现在这里）</li></ul></div>
    <button id="settings-toggle">⚙ 设置</button>
  </aside>
  <div id="app">
    <header>
      <button id="expand-side" title="展开侧栏" style="display:none">☰</button>
      <h1>Jarvis</h1>
      <span class="chip" id="model-chip" title="模型"><!--__MODEL__--></span>
      <button class="chip" id="mode-chip" title="点击切换：对话 / 规划（只读）">💬 对话</button>
      <button class="chip" id="theme-chip" title="点击切换深色 / 浅色">🌙 深色</button>
      <span id="status">就绪</span>
    </header>
    <div id="toolbar">
      <button id="rewind" title="撤销上一轮（删掉最近一问一答）">撤销上一轮</button>
      <button id="timeline" title="显示/隐藏对话时间线">时间线</button>
      <button id="diff-toggle" title="显示/隐藏工作区当前改动">Diff</button>
      <button id="shortcuts-toggle" title="显示/隐藏快捷键总览">⌨ 快捷键</button>
    </div>
    <div id="shortcuts" hidden>
      <b>快捷键</b>：Enter 发送 / Shift+Enter 换行；忙时 Tab 把追问排进队列；发送中点“停止”掐断本轮；消息悬停可复制 / 重新生成；代码块右上角复制；工具卡片点开看详情；模式芯片切对话 / 规划（规划只读，先出计划再批准执行）。
    </div>
    <div id="disconnect" hidden>连接已断开，transcript 已保留。<button id="reconnect">手动重连</button></div>
    <div id="chat"></div>
    <div id="empty" hidden>
      <h2>Jarvis 已启动</h2>
      <p>能记住事情、读写文件、跑命令。直接说话，或者先记住点什么。</p>
      <p class="keyhint" id="empty-keyhint" hidden>先在设置里填入你的 DeepSeek API key 再开始（key 只存在你的浏览器里）。</p>
      <div id="suggestions">
        <button data-q="记住，我最喜欢蓝色">记住，我最喜欢蓝色</button>
        <button data-q="帮我写一个 Python 快速排序">帮我写一个 Python 快速排序</button>
        <button data-q="今天北京天气怎么样？">今天北京天气怎么样？</button>
        <button data-q="总结一下这个项目是干什么的">总结一下这个项目是干什么的</button>
      </div>
    </div>
    <div id="usage">用量：—<div class="ctxbar"><div class="ctxfill" id="ctxfill"></div></div></div>
    <div id="row">
      <textarea id="input" rows="1" placeholder="和 Jarvis 说点什么…（回车发送，Shift+回车换行）" autocomplete="off"></textarea>
      <button id="send">发送</button>
      <button id="clear" title="清空对话（长期记忆还在）">清空</button>
    </div>
    <div id="queued" hidden></div>
    <div id="settings" hidden>
      <div id="keybar">
        <input id="key" type="password" placeholder="你的 DeepSeek API key（sk-…），只存在你的浏览器里" autocomplete="off">
        <button id="savekey">保存</button>
        <button id="clearkey" title="清除浏览器里存的 key">清除</button>
        <span id="keystatus"></span>
      </div>
      <p class="hint">key 只存在你的浏览器本地，每次请求发给服务器调 DeepSeek 用，服务器不保存它。</p>
    </div>
    <div id="trajectory" style="display:none"></div>
    <div id="diff-drawer" hidden>
      <h3>工作区改动</h3>
      <div id="diff-btns">
        <button id="diff-accept">接受全部</button>
        <button id="diff-reject">拒绝全部</button>
        <button id="diff-close">关闭</button>
      </div>
      <div id="diff-content">（点 Diff 打开时加载）</div>
    </div>
  </div>
</div>
<script>
const chatEl = document.getElementById('chat');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('send');
const statusEl = document.getElementById('status');
const keyEl = document.getElementById('key');
const keyStatusEl = document.getElementById('keystatus');
const usageEl = document.getElementById('usage');
const ctxFillEl = document.getElementById('ctxfill');
const sessionsEl = document.getElementById('sessions');
const emptyEl = document.getElementById('empty');
const disconnectEl = document.getElementById('disconnect');
const queuedEl = document.getElementById('queued');
const modeChipEl = document.getElementById('mode-chip');
const layoutEl = document.getElementById('layout');

// 运行状态：busy 时发送按钮变 Stop；Tab 把追问排进队列，轮次结束自动发。
let busy = false;
let abortCtl = null;
let queued = null;        // 排队中的追问文本（忙时 Tab 排队）
let lastUserText = '';    // 最近一次发出的用户消息（错误重试 / 断线重连用）
let stopped = false;      // 本轮是不是被 Stop 掐断的
let mode = localStorage.getItem('jarvis-mode') || 'chat';  // chat | plan
let sessionFilter = '';
let lastCtxPct = null;  // 最近一次 status 事件的 context 占比（done 后保持）
let pendingPlan = null;  // 最近一次 plan 事件的计划（批准时原样发回去）
const todoListEl = document.getElementById('todo-list');

// 浏览器是状态的家：历史和长期事实存在 localStorage，每次请求一起发过去。
// 多会话：jarvis-sessions = {id: {title, history, facts}}，当前会话 id 单独存。
let sessions = JSON.parse(localStorage.getItem('jarvis-sessions') || '{}');
let currentSession = localStorage.getItem('jarvis-current-session') || '';
if (!currentSession || !sessions[currentSession]) {
  currentSession = 's' + Date.now().toString(36);
  sessions[currentSession] = { title: '新会话', history: [], facts: [] };
  // 兼容旧版：旧 key 里有历史就迁过来
  const oldHistory = JSON.parse(localStorage.getItem('jarvis-history') || '[]');
  const oldFacts = JSON.parse(localStorage.getItem('jarvis-facts') || '[]');
  if (oldHistory.length) sessions[currentSession].history = oldHistory;
  if (oldFacts.length) sessions[currentSession].facts = oldFacts;
}
let history = sessions[currentSession].history || [];
let facts = sessions[currentSession].facts || [];
// API key 也只存在浏览器本地：每次请求发给服务器调 DeepSeek 用，服务器不保存它。
let apiKey = localStorage.getItem('jarvis-api-key') || '';
function refreshKeyUI() {
  keyEl.value = apiKey;
  keyStatusEl.textContent = apiKey ? 'key 已保存（仅本浏览器）' : '未填 key';
}
refreshKeyUI();
function refreshModeChip() {
  modeChipEl.textContent = mode === 'plan' ? '📋 规划（只读）' : '💬 对话';
  modeChipEl.title = mode === 'plan'
    ? '规划模式：只读，不给写工具。点击切回对话'
    : '对话模式。点击切换到规划（只读）';
}
refreshModeChip();
modeChipEl.addEventListener('click', () => {
  if (busy) return;
  mode = mode === 'plan' ? 'chat' : 'plan';
  localStorage.setItem('jarvis-mode', mode);
  refreshModeChip();
  statusEl.textContent = mode === 'plan' ? '规划模式（只读）' : '对话模式';
});

// 主题切换（U6-1）：深色是 :root 默认，浅色用 html[data-theme="light"]
// 覆盖变量；选择存在 localStorage，下次打开还在。
const themeChipEl = document.getElementById('theme-chip');
let theme = localStorage.getItem('jarvis-theme') || 'dark';
function refreshThemeChip() {
  document.documentElement.setAttribute('data-theme', theme);
  themeChipEl.textContent = theme === 'light' ? '☀️ 浅色' : '🌙 深色';
}
refreshThemeChip();
themeChipEl.addEventListener('click', () => {
  theme = theme === 'light' ? 'dark' : 'light';
  localStorage.setItem('jarvis-theme', theme);
  refreshThemeChip();
});
document.getElementById('savekey').addEventListener('click', () => {
  apiKey = keyEl.value.trim();
  if (apiKey) localStorage.setItem('jarvis-api-key', apiKey);
  else localStorage.removeItem('jarvis-api-key');
  refreshKeyUI();
  statusEl.textContent = apiKey ? 'key 已保存' : 'key 已清除';
});
document.getElementById('clearkey').addEventListener('click', () => {
  apiKey = '';
  localStorage.removeItem('jarvis-api-key');
  refreshKeyUI();
  statusEl.textContent = 'key 已清除';
});

function refreshSessionSelect() {
  const cur = currentSession;
  sessionsEl.innerHTML = '';
  Object.entries(sessions)
    .filter(([, s]) => !sessionFilter
      || (s.title || '新会话').includes(sessionFilter))
    .sort((a, b) => (b[1].history || []).length - (a[1].history || []).length
      || String(b[0]).localeCompare(String(a[0])))
    .forEach(([id, s]) => {
      const li = document.createElement('li');
      if (id === cur) li.className = 'active';
      const title = document.createElement('span');
      title.className = 't';
      title.textContent = (s.title || '新会话').slice(0, 20);
      const count = document.createElement('span');
      count.className = 'n';
      count.textContent = (s.history || []).length;
      const rename = document.createElement('button');
      rename.className = 'mini';
      rename.title = '重命名';
      rename.textContent = '✎';
      rename.addEventListener('click', e => {
        e.stopPropagation();
        const next = prompt('重命名会话', s.title || '新会话');
        if (next !== null && next.trim()) {
          sessions[id].title = next.trim().slice(0, 20);
          save();
        }
      });
      const del = document.createElement('button');
      del.className = 'mini';
      del.title = '删除会话';
      del.textContent = '✕';
      del.addEventListener('click', e => {
        e.stopPropagation();
        if (!confirm('删除这个会话？（只删浏览器本地记录）')) return;
        delete sessions[id];
        if (id === currentSession) {
          const rest = Object.keys(sessions);
          if (rest.length) {
            currentSession = rest[0];
            history = sessions[currentSession].history || [];
            facts = sessions[currentSession].facts || [];
          } else {
            currentSession = 's' + Date.now().toString(36);
            sessions[currentSession] = { title: '新会话', history: [], facts: [] };
            history = [];
          }
          localStorage.setItem('jarvis-current-session', currentSession);
        }
        save();
        redraw();
      });
      li.append(title, count, rename, del);
      li.addEventListener('click', () => {
        if (busy || id === currentSession) return;
        currentSession = id;
        localStorage.setItem('jarvis-current-session', currentSession);
        history = sessions[id].history || [];
        facts = sessions[id].facts || [];
        save();
        redraw();
        statusEl.textContent = '已切换会话';
      });
      sessionsEl.appendChild(li);
    });
}

function save() {
  sessions[currentSession] = {
    title: sessions[currentSession].title
      || ((history.find(m => m.role === 'user') || {}).content || '新会话').slice(0, 20),
    history, facts,
  };
  localStorage.setItem('jarvis-sessions', JSON.stringify(sessions));
  localStorage.setItem('jarvis-current-session', currentSession);
  // 兼容旧版：旧 key 也同步一份
  localStorage.setItem('jarvis-history', JSON.stringify(history));
  localStorage.setItem('jarvis-facts', JSON.stringify(facts));
  refreshSessionSelect();
}

function esc(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function md2html(text) {
  // 极简 Markdown：代码块（带语言标签 + 复制按钮）、行内代码、粗体、标题、列表
  const lines = text.split('\\n');
  let out = '', inCode = false, codeLang = '';
  const list = [];
  function inline(s) {
    s = esc(s);
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    s = s.replace(/\\*\\*([^*]+)\\*\\*/g, '<b>$1</b>');
    return s;
  }
  function flushList() {
    if (list.length) { out += '<ul>' + list.join('') + '</ul>'; list.length = 0; }
  }
  for (const line of lines) {
    if (line.trim().startsWith('```')) {
      if (inCode) { out += '</code></pre></div>'; inCode = false; }
      else {
        flushList();
        codeLang = line.trim().slice(3).trim() || 'text';
        out += '<div class="codeblock"><div class="codehead"><span>'
          + esc(codeLang) + '</span><button class="copy-code">复制</button></div>'
          + '<pre><code>';
        inCode = true;
      }
      continue;
    }
    if (inCode) { out += esc(line) + '\\n'; continue; }
    const t = line.trim();
    if (t.startsWith('- ')) list.push('<li>' + inline(t.slice(2)) + '</li>');
    else if (t.startsWith('# ')) { flushList(); out += '<h3>' + inline(t.slice(2)) + '</h3>'; }
    else if (t === '') flushList();
    else { flushList(); out += '<p>' + inline(line) + '</p>'; }
  }
  flushList();
  if (inCode) out += '</code></pre></div>';
  return out;
}

// 代码块复制按钮（事件委托：md2html 动态生成的按钮也管得到）
chatEl.addEventListener('click', e => {
  const btn = e.target.closest('.copy-code');
  if (!btn) return;
  const code = btn.closest('.codeblock').querySelector('code').innerText;
  navigator.clipboard.writeText(code).then(() => {
    btn.textContent = '已复制';
    setTimeout(() => { btn.textContent = '复制'; }, 1200);
  }).catch(() => { btn.textContent = '复制失败'; });
});

function addMsg(role, text) {
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  // 内容放 .msg-body 里：流式追加只动它，不会把操作按钮冲掉
  const body = document.createElement('div');
  body.className = 'msg-body';
  if (role === 'jarvis') body.innerHTML = md2html(text);
  else body.textContent = text;
  div.appendChild(body);
  // 悬停操作：复制；Jarvis 的气泡还能重新生成
  const actions = document.createElement('div');
  actions.className = 'msg-actions';
  const copyBtn = document.createElement('button');
  copyBtn.textContent = '复制';
  copyBtn.addEventListener('click', () => {
    navigator.clipboard.writeText(body.innerText).then(() => {
      copyBtn.textContent = '已复制';
      setTimeout(() => { copyBtn.textContent = '复制'; }, 1200);
    });
  });
  actions.appendChild(copyBtn);
  if (role === 'jarvis') {
    const regenBtn = document.createElement('button');
    regenBtn.textContent = '重新生成';
    regenBtn.title = '删掉这条回答，按上一问重新生成';
    regenBtn.addEventListener('click', () => {
      if (busy) return;
      regenerate();
    });
    actions.appendChild(regenBtn);
  }
  div.appendChild(actions);
  chatEl.appendChild(div);
  chatEl.scrollTop = chatEl.scrollHeight;
  return div;
}

// 待办清单（U4-2）：todo 事件整表重渲染；同一份清单三个界面一样。
function renderTodos(todos) {
  todoListEl.innerHTML = '';
  if (!todos || !todos.length) {
    const li = document.createElement('li');
    li.textContent = '（清单是空的，规划模式出计划后出现在这里）';
    todoListEl.appendChild(li);
    return;
  }
  todos.forEach(t => {
    const li = document.createElement('li');
    const mark = t.status === 'done' ? '☑ ' : (t.status === 'active' ? '◐ ' : '☐ ');
    li.textContent = mark + t.content + (t.acceptance ? ' —— 验收：' + t.acceptance : '');
    if (t.status === 'done') li.className = 'done';
    if (t.status === 'active') li.className = 'active';
    todoListEl.appendChild(li);
  });
}

// 计划卡片（U4-1）：目标 + 步骤 + 风险，markdown 可改，批准 / 拒绝。
function showPlanCard(payload) {
  pendingPlan = payload;
  const card = document.createElement('div');
  card.className = 'plancard';
  const h = document.createElement('h3');
  h.textContent = '📋 计划：' + (payload.goal || '');
  card.appendChild(h);
  const steps = document.createElement('div');
  steps.className = 'p-steps';
  steps.textContent = (payload.steps || []).map((s, i) =>
    (i + 1) + '. ' + s.title + (s.acceptance ? ' —— 验收：' + s.acceptance : '')).join('\\n');
  card.appendChild(steps);
  if (payload.risks && payload.risks.length) {
    const r = document.createElement('div');
    r.className = 'p-risks';
    r.textContent = '风险：' + payload.risks.join('；');
    card.appendChild(r);
  }
  const edit = document.createElement('textarea');
  edit.value = payload.markdown || '';
  edit.title = '直接改这里的文本，再点批准执行，改过的内容会生效';
  card.appendChild(edit);
  const btns = document.createElement('div');
  btns.className = 'p-btns';
  const ok = document.createElement('button');
  ok.className = 'primary';
  ok.textContent = '批准执行';
  ok.addEventListener('click', () => {
    if (busy) return;
    card.querySelectorAll('button').forEach(b => { b.disabled = true; });
    const note = document.createElement('div');
    note.textContent = '已批准，开始按计划执行。';
    note.style.color = 'var(--color-textMuted)';
    chatEl.appendChild(note);
    send(lastUserText, { modeOverride: 'execute', plan: pendingPlan,
                         planMarkdown: edit.value, skipUserBubble: true });
  });
  const no = document.createElement('button');
  no.textContent = '拒绝';
  no.addEventListener('click', () => {
    const note = document.createElement('div');
    note.textContent = '已拒绝，计划没有执行。';
    note.style.color = 'var(--color-textMuted)';
    chatEl.appendChild(note);
    card.remove();
    pendingPlan = null;
    statusEl.textContent = '就绪';
  });
  btns.append(ok, no);
  card.appendChild(btns);
  chatEl.appendChild(card);
  chatEl.scrollTop = chatEl.scrollHeight;
}

// 重新生成（简版，不做分支树）：删掉最后一条 assistant 回答及
// 其后的 tool 消息，按上一条 user 消息重发。
function regenerate() {
  let cut = history.length;
  while (cut > 0 && history[cut - 1].role !== 'user') cut--;
  if (cut === 0) return;
  const lastUser = history[cut - 1].content;
  history = history.slice(0, cut - 1);
  save();
  redraw();
  send(lastUser);
}

function redraw() {
  chatEl.innerHTML = '';
  history.forEach(m => {
    if (m.role === 'user') addMsg('user', m.content);
    else if (m.role === 'assistant' && m.content) addMsg('jarvis', m.content);
  });
  const empty = !history.length;
  emptyEl.hidden = !empty;
  chatEl.style.display = empty ? 'none' : '';
  document.getElementById('empty-keyhint').hidden = !!apiKey;
}

// 刷新后按保存的历史重画（只画 user/assistant 的文字，tool 消息画成卡片）。
redraw();
refreshSessionSelect();

function setBusy(b) {
  busy = b;
  // 发送中按钮变 Stop（点它掐断本轮）；输入框保持可用，Tab 可以排队追问
  sendBtn.textContent = b ? '停止' : '发送';
  sendBtn.classList.toggle('stop', b);
  if (!b) inputEl.focus();
}

function stop() {
  if (abortCtl) abortCtl.abort();
}

let errorStreak = 0;  // 连续出错次数（U6-4：重试只给一次）

// 已知错误的恢复提示（U6-4；Python 那份在 jarvis/ui/events.py 的 friendly_error）
function hintFor(message) {
  const t = (message || '').toLowerCase();
  if (t.includes('429') || t.includes('rate limit')) return '（请求太频繁：模型端在限流，等一会儿再试。）';
  if (t.includes('401') || t.includes('unauthorized') || t.includes('incorrect api key') || t.includes('invalid api key') || t.includes('authentication')) return '（key 无效或过期：去设置里重新填 key。）';
  if (t.includes('connect') || t.includes('network') || t.includes('timed out') || t.includes('timeout') || t.includes('unreachable')) return '（网络不通：检查网络或代理，稍后再试。）';
  if (t.includes('context_length_exceeded') || t.includes('context length') || t.includes('maximum context')) return '（上下文超长：开新会话，或等自动压缩。）';
  if (t.includes('insufficient_quota') || t.includes('insufficient quota')) return '（账户额度不足：去 DeepSeek 平台看余额。）';
  return '';
}

// 就地错误卡片：第一次给“重试”，重试一次还失败改给“复制问题，开新会话”
function showErrorCard(message) {
  errorStreak += 1;
  const card = document.createElement('div');
  card.className = 'errcard';
  const msg = document.createElement('div');
  msg.className = 'e-msg';
  msg.textContent = '出错了：' + message + hintFor(message);
  if (errorStreak === 1) {
    const retry = document.createElement('button');
    retry.textContent = '重试';
    retry.addEventListener('click', () => {
      card.remove();
      if (lastUserText) send(lastUserText);
    });
    card.append(msg, retry);
  } else {
    const copyNew = document.createElement('button');
    copyNew.textContent = '复制问题，开新会话';
    copyNew.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(lastUserText); } catch (e) {}
      card.remove();
      newSession();
      inputEl.value = lastUserText;
      inputEl.focus();
    });
    card.append(msg, copyNew);
  }
  chatEl.appendChild(card);
  chatEl.scrollTop = chatEl.scrollHeight;
  statusEl.textContent = '出错';
}

// 开新会话：新 id + 清空历史（“复制问题，开新会话”与侧栏“新会话”共用）
function newSession() {
  if (busy) return;
  currentSession = 's' + Date.now().toString(36);
  sessions[currentSession] = { title: '新会话', history: [], facts };
  history = [];
  save();
  redraw();
  statusEl.textContent = '新会话';
}

// U7：分叉当前会话（与 CLI/TUI 的 /fork 对齐）：复制历史与事实到
// 一个新会话并切过去，原会话不动。
function forkSession() {
  if (busy) return;
  const src = sessions[currentSession] || { title: '新会话', history: [], facts: [] };
  const id = 's' + Date.now().toString(36);
  sessions[id] = {
    title: ((src.title || '新会话') + '（分叉）').slice(0, 20),
    history: JSON.parse(JSON.stringify(src.history || [])),
    facts: JSON.parse(JSON.stringify(src.facts || [])),
  };
  currentSession = id;
  history = sessions[id].history;
  facts = sessions[id].facts;
  save();
  redraw();
  statusEl.textContent = '已分叉到新会话';
}

function setUsageLine(text, ctxPct) {
  usageEl.firstChild.textContent = text;
  if (ctxPct === null || ctxPct === undefined) {
    ctxFillEl.style.width = '0';
    ctxFillEl.className = 'ctxfill';
  } else {
    ctxFillEl.style.width = Math.min(100, ctxPct * 100).toFixed(1) + '%';
    ctxFillEl.className = 'ctxfill' + (ctxPct >= 0.95 ? ' hot' : ctxPct >= 0.8 ? ' warn' : '');
  }
}

async function send(textOverride, opts) {
  if (busy) return;
  const text = (textOverride !== undefined ? textOverride : inputEl.value).trim();
  if (!text) return;
  lastUserText = text;
  inputEl.value = '';
  autogrow();
  emptyEl.hidden = true;
  chatEl.style.display = '';
  disconnectEl.hidden = true;
  // 执行轮（批准计划后）不重复挂用户气泡：任务气泡在计划轮已经挂了
  if (!opts || !opts.skipUserBubble) addMsg('user', text);
  setBusy(true);
  stopped = false;
  statusEl.textContent = '思考中…';
  let bubble = null;  // 流式回答的那个气泡，token 来了就往里加字
  let bubbleBody = null;
  let thinkBox = null, thinkBody = null;  // 思考 disclosure
  let curCard = null;  // 当前工具卡片
  let sawDone = false;
  let sawPlanOnly = false;  // 本轮是不是只出计划（done 里没回答可补气泡）
  abortCtl = new AbortController();

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, history, facts, api_key: apiKey,
                             mode: (opts && opts.modeOverride) || mode,
                             plan: (opts && opts.plan) || undefined,
                             plan_markdown: (opts && opts.planMarkdown) || undefined }),
      signal: abortCtl.signal,
    });
    if (!resp.ok) {
      const detail = await resp.json().catch(() => ({}));
      throw new Error(detail.detail || ('HTTP ' + resp.status));
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\\n\\n')) !== -1) {
        const raw = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        let event = '', data = '';
        raw.split('\\n').forEach(line => {
          if (line.startsWith('event:')) event = line.slice(6).trim();
          else if (line.startsWith('data:')) data = line.slice(5).trim();
        });
        if (!data) continue;
        const payload = JSON.parse(data);
        if (event === 'token') {
          if (!bubble) {
            bubble = addMsg('jarvis', '');
            bubbleBody = bubble.querySelector('.msg-body');
          }
          bubbleBody.textContent += payload.text;
          chatEl.scrollTop = chatEl.scrollHeight;
        } else if (event === 'thinking') {
          if (!thinkBox) {
            thinkBox = document.createElement('details');
            thinkBox.className = 'thinking';
            thinkBox.innerHTML = '<summary>思考中…</summary><div></div>';
            thinkBody = thinkBox.querySelector('div');
            chatEl.appendChild(thinkBox);
          }
          thinkBody.textContent += payload.text;
        } else if (event === 'status') {
          const p = payload;
          lastCtxPct = p.context_pct ?? null;
          const total = p.total_tokens ?? ((p.prompt_tokens || 0) + (p.completion_tokens || 0));
          setUsageLine('用量：本轮 ' + total + ' tokens（输入 ' + (p.prompt_tokens ?? '—')
            + ' / 输出 ' + (p.completion_tokens ?? '—') + '），约 ¥'
            + (p.cost_cny ?? 0).toFixed(4)
            + (p.unknown_calls ? '（' + p.unknown_calls + ' 次未返回用量）' : ''),
            p.context_pct);
        } else if (event === 'tool') {
          if (payload.phase === 'start') {
            curCard = document.createElement('details');
            curCard.className = 'toolcard ' + (payload.risk || 'write');
            const sum = document.createElement('summary');
            sum.innerHTML = '<span class="t-summary"></span><span class="t-status">进行中…</span><span class="t-dur"></span>';
            sum.querySelector('.t-summary').textContent = payload.line;
            const body = document.createElement('div');
            body.className = 't-body';
            const pre = document.createElement('pre');
            pre.textContent = JSON.stringify(payload.args ?? {}, null, 2);
            body.appendChild(pre);
            curCard.append(sum, body);
            chatEl.appendChild(curCard);
          } else if (payload.phase === 'end' && curCard) {
            const ok = payload.status === 'ok';
            curCard.querySelector('.t-status').textContent = ok ? '✓ 完成' : '✗ 失败';
            curCard.querySelector('.t-dur').textContent = payload.duration_ms + 'ms';
            const pre = document.createElement('pre');
            pre.textContent = payload.result_preview || '';
            curCard.querySelector('.t-body').appendChild(pre);
            curCard = null;
          }
          statusEl.textContent = payload.line;
          chatEl.scrollTop = chatEl.scrollHeight;
        } else if (event === 'plan') {
          showPlanCard(payload);
          statusEl.textContent = '计划已出，等批准';
        } else if (event === 'todo') {
          renderTodos(payload.todos);
        } else if (event === 'verify') {
          // U7：计划执行完的验收报告（与 CLI/TUI/GUI 同款内容）。
          const note = document.createElement('div');
          note.className = 'msg jarvis';
          note.textContent = payload.report
            ? '验收检查：\n' + payload.report
            : '验收检查失败：' + (payload.error || '未知错误');
          chatEl.appendChild(note);
          chatEl.scrollTop = chatEl.scrollHeight;
        } else if (event === 'done') {
          sawDone = true;
          errorStreak = 0;  // 本轮成功：连续出错清零（U6-4）
          history = payload.history;
          facts = payload.facts;
          save();
          if (payload.plan_only) {
            // 计划轮：没有回答气泡；用量行按计划轮的真实用量刷新
            //（U7 以前这里硬编码 0，计划轮 token 全漏）。
            sawPlanOnly = true;
            const u = payload.usage || {};
            setUsageLine('用量：本次 ' + (u.tokens || 0) + ' tokens（输入 '
              + (u.prompt_tokens ?? '—') + ' / 输出 ' + (u.completion_tokens ?? '—')
              + '），约 ¥' + (u.cost_cny || 0).toFixed(4)
              + (u.unknown_calls ? '（' + u.unknown_calls + ' 次未返回用量）' : ''),
              lastCtxPct);
            statusEl.textContent = '计划已出，等批准';
          } else {
            if (bubble && payload.answer) bubbleBody.innerHTML = md2html(payload.answer);
            if (thinkBox) thinkBox.querySelector('summary').textContent = '思考过程（点击展开）';
            const u = payload.usage || {};
            setUsageLine('用量：本次 ' + (u.tokens || 0) + ' tokens（输入 '
              + (u.prompt_tokens ?? '—') + ' / 输出 ' + (u.completion_tokens ?? '—')
              + '），约 ¥' + (u.cost_cny || 0).toFixed(4) + '，耗时 '
              + ((payload.duration_ms || 0) / 1000).toFixed(1) + ' 秒'
              + (u.unknown_calls ? '（' + u.unknown_calls + ' 次未返回用量）' : ''),
              lastCtxPct);
            statusEl.textContent = '就绪';
          }
        } else if (event === 'error') {
          throw new Error(payload.message);
        }
      }
    }
    // 流正常结束却没收到 done：SSE 断了，transcript 保留，露横幅让人手动重连
    if (!sawDone && !stopped) {
      disconnectEl.hidden = false;
      statusEl.textContent = '连接已断开';
    }
    // 没收到过 token（比如模型一次答完）：按 done 里的回答补一个气泡。
    // 计划轮没有新回答，不补（否则会把上一轮的回答重复挂出来）。
    if (!bubble && sawDone && !sawPlanOnly) {
      const last = history.filter(m => m.role === 'assistant').pop();
      if (last && last.content) addMsg('jarvis', last.content);
    }
  } catch (err) {
    if (err.name === 'AbortError') {
      stopped = true;
      if (bubble) {
        const note = document.createElement('div');
        note.style.color = 'var(--color-textFaint)';
        note.textContent = '■ 已停止';
        bubbleBody.appendChild(note);
      }
      statusEl.textContent = '已停止';
    } else {
      showErrorCard(err.message);
      if (err.message.includes('API key')) {
        document.getElementById('settings').hidden = false;
        keyEl.focus();
      }
    }
  } finally {
    setBusy(false);
    abortCtl = null;
    // 排队里的追问：轮次结束自动发
    if (queued) {
      const next = queued;
      queued = null;
      queuedEl.hidden = true;
      send(next);
    }
  }
}

document.getElementById('search').addEventListener('input', e => {
  sessionFilter = e.target.value.trim();
  refreshSessionSelect();
});
document.getElementById('collapse-side').addEventListener('click', () => {
  layoutEl.classList.add('noside');
  document.getElementById('expand-side').style.display = '';
});
document.getElementById('expand-side').addEventListener('click', () => {
  layoutEl.classList.remove('noside');
  document.getElementById('expand-side').style.display = 'none';
});
document.getElementById('settings-toggle').addEventListener('click', () => {
  const panel = document.getElementById('settings');
  panel.hidden = !panel.hidden;
});
document.getElementById('reconnect').addEventListener('click', () => {
  disconnectEl.hidden = true;
  if (lastUserText) send(lastUserText);
});
document.getElementById('newsession').addEventListener('click', newSession);
document.getElementById('forksession').addEventListener('click', forkSession);
document.getElementById('shortcuts-toggle').addEventListener('click', () => {
  const el = document.getElementById('shortcuts');
  el.hidden = !el.hidden;
});
document.getElementById('rewind').addEventListener('click', () => {
  if (busy) return;
  // 删掉最近一对 user + assistant（中间的 tool 消息跟着一起删）
  let cut = history.length;
  while (cut > 0 && history[cut - 1].role !== 'user') cut--;
  if (cut > 0) cut--;
  if (cut < history.length) {
    history = history.slice(0, cut);
    save();
    redraw();
    statusEl.textContent = '已撤销上一轮';
  } else {
    statusEl.textContent = '没有可撤销的对话';
  }
});
document.getElementById('timeline').addEventListener('click', () => {
  const panel = document.getElementById('trajectory');
  if (panel.style.display === 'none') {
    panel.innerHTML = history.map((m, i) => {
      const who = m.role === 'user' ? '你' : (m.role === 'assistant' ? 'Jarvis' : '工具');
      const text = (m.content || '').replace(/\\s+/g, ' ').slice(0, 40);
      return '<div>' + (i + 1) + '. [' + who + '] ' + esc(text) + '</div>';
    }).join('') || '<div>还没有对话。</div>';
    panel.style.display = 'block';
  } else {
    panel.style.display = 'none';
  }
});

// Diff 抽屉（U5-3）：GET /api/diff，按文件分组渲染；接受只是记一条
// 提示（改动本来就在工作区）；拒绝在网页版没有检查点可回滚，直说去
// 桌面端/TUI 用检查点。
async function loadDiff() {
  const box = document.getElementById('diff-content');
  box.textContent = '加载中…';
  try {
    const resp = await fetch('/api/diff');
    const data = await resp.json();
    box.innerHTML = '';
    if (data.note) {
      box.textContent = data.note;
      return;
    }
    data.files.forEach(f => {
      const h = document.createElement('h4');
      h.textContent = f.path + '（' + f.summary + '）';
      box.appendChild(h);
      f.lines.forEach(line => {
        const div = document.createElement('div');
        div.className = 'd-line';
        div.textContent = line;
        if (line.startsWith('+') && !line.startsWith('+++')) div.classList.add('d-add');
        else if (line.startsWith('-') && !line.startsWith('---')) div.classList.add('d-del');
        else if (line.startsWith('@@')) div.classList.add('d-hunk');
        box.appendChild(div);
      });
    });
  } catch (err) {
    box.textContent = '加载失败：' + err.message;
  }
}
document.getElementById('diff-toggle').addEventListener('click', () => {
  const drawer = document.getElementById('diff-drawer');
  if (drawer.hidden) {
    drawer.hidden = false;
    loadDiff();
  } else {
    drawer.hidden = true;
  }
});
document.getElementById('diff-close').addEventListener('click', () => {
  document.getElementById('diff-drawer').hidden = true;
});
document.getElementById('diff-accept').addEventListener('click', () => {
  const note = document.createElement('div');
  note.className = 'd-note';
  note.textContent = '已接受当前改动（改动保留在工作区）。';
  document.getElementById('diff-content').appendChild(note);
});
document.getElementById('diff-reject').addEventListener('click', () => {
  const note = document.createElement('div');
  note.className = 'd-note';
  note.textContent = '网页版没有检查点可回滚：拒绝改动请在桌面端或 TUI 里打开检查点页恢复。';
  document.getElementById('diff-content').appendChild(note);
});

// composer：多行自增高（最多 6 行后内部滚动）
function autogrow() {
  inputEl.style.height = 'auto';
  const lineHeight = 24;  // 15px 字号 × 1.6 行高
  inputEl.style.height = Math.min(inputEl.scrollHeight, lineHeight * 6 + 20) + 'px';
}
inputEl.addEventListener('input', autogrow);

sendBtn.addEventListener('click', () => { busy ? stop() : send(); });
inputEl.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    if (busy) {
      statusEl.textContent = '正在回答…按 Tab 可把追问排进队列';
      return;
    }
    send();
  } else if (e.key === 'Tab' && busy) {
    // 忙时 Tab：把输入框里的追问排进队列，轮次结束自动发
    e.preventDefault();
    const text = inputEl.value.trim();
    if (!text) return;
    queued = text;
    inputEl.value = '';
    autogrow();
    queuedEl.hidden = false;
    queuedEl.textContent = '排队中：' + text;
    statusEl.textContent = '已排队，本轮结束后自动发送';
  }
});
document.getElementById('suggestions').addEventListener('click', e => {
  const btn = e.target.closest('[data-q]');
  if (btn && !busy) send(btn.dataset.q);
});
document.getElementById('clear').addEventListener('click', () => {
  if (busy) return;
  history = [];
  save();
  redraw();
  statusEl.textContent = '对话已清空，长期记忆还在。';
});
inputEl.focus();
</script>
</body>
</html>"""


# 主题与模型名在启动时注入页面（颜色只在 theme.json / light.json 里改一处）：
# /*__THEME_VARS__*/ -> :root { --color-*: ... }（深色默认），
# /*__THEME_VARS_LIGHT__*/ -> html[data-theme="light"] { ... }（浅色覆盖），
# <!--__MODEL__--> -> 模型 id。
INDEX_HTML = INDEX_HTML.replace(
    "/*__THEME_VARS__*/", to_css_vars(load_theme("dark")))
INDEX_HTML = INDEX_HTML.replace(
    "/*__THEME_VARS_LIGHT__*/",
    to_css_vars(load_theme("light")).replace(":root", "html[data-theme=\"light\"]", 1))
INDEX_HTML = INDEX_HTML.replace("<!--__MODEL__-->", MODEL)
# 空态文案走 viewstate（四界面同一份；主题/模型注入同款做法）。
INDEX_HTML = INDEX_HTML.replace(
    "能记住事情、读写文件、跑命令。直接说话，或者先记住点什么。",
    EMPTY_TAGLINE + " " + EMPTY_HINTS[0])


@app.get("/")
def index():
    return HTMLResponse(INDEX_HTML)


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/diff")
def diff():
    """U5-3：工作区当前改动（按文件分组，含未跟踪文件），给 Diff 抽屉用。

    只读：跑的是服务器所在工作区的 git diff。Vercel 上是部署快照，
    通常没有改动；本地跑 uvicorn 时就是本仓库的改动。"""
    text = working_tree_diff()
    if "不是一个 git 仓库" in text:
        return {"files": [], "note": "当前目录不是一个 git 仓库。"}
    files = parse_diff(text)
    if not files:
        return {"files": [], "note": "还没有改动。"}
    return {"files": files, "note": ""}


@app.post("/api/chat")
def chat(req: ChatRequest):
    start = time.monotonic()
    api_key = (req.api_key or "").strip()
    key_source = "byok" if api_key else ("env" if os.getenv("DEEPSEEK_API_KEY") else "none")
    if not req.message.strip():
        _log_chat((time.monotonic() - start) * 1000, False, "empty_message", key_source)
        raise HTTPException(status_code=400, detail="message 不能为空")
    if not api_key and not os.getenv("DEEPSEEK_API_KEY"):
        _log_chat((time.monotonic() - start) * 1000, False, "missing_key", key_source)
        raise HTTPException(
            status_code=400,
            detail="请先在页面上方填写你的 DeepSeek API key。key 只存在你的浏览器本地，"
                   "每次请求发给服务器调 DeepSeek 用，服务器不保存它。",
        )
    try:
        client = get_client(api_key or None)
    except SystemExit as exc:
        _log_chat((time.monotonic() - start) * 1000, False, "no_client", key_source)
        raise HTTPException(status_code=500, detail=str(exc))

    # 浏览器带来的事实是权威来源：先并进库里（去重），再跑循环。
    for fact in req.facts:
        if isinstance(fact, str):
            STORE.add(fact)
    schemas, funcs = memory_tools(STORE)

    # 组装消息：system + 浏览器历史（去掉可能混进来的 system）+ 本轮问题。
    # run_agent 会自己把 user 消息追加进 history，所以这里只给到历史为止。
    messages = [{"role": "system", "content": _system_prompt()}]
    for m in req.history[-HISTORY_LIMIT:]:
        if isinstance(m, dict) and m.get("role") != "system":
            messages.append(m)

    def event_stream():
        outcome = None  # "ok" / "error" / None（客户端中途断开）
        try:
            for kind, payload in _run_agent_streamed(
                    client, req.message, messages, schemas, funcs,
                    mode=req.mode, plan=req.plan,
                    plan_markdown=req.plan_markdown):
                if kind == "text_delta":
                    yield _sse_event("token", {"text": payload["text"]})
                elif kind == "thinking_delta":
                    yield _sse_event("thinking", {"text": payload["text"]})
                elif kind == "plan_ready":
                    yield _sse_event("plan", payload)
                elif kind == "todo_set":
                    yield _sse_event("todo", payload)
                elif kind == "verify":
                    yield _sse_event("verify", payload)
                elif kind == "status":
                    yield _sse_event("status", payload)
                elif kind == "tool_start":
                    yield _sse_event("tool", {
                        "phase": "start", "name": payload["name"],
                        "args": payload["args"],
                        "risk": tool_risk(payload["name"]),
                        "line": "▸ " + summarise_tool_call(
                            payload["name"], payload["args"])})
                elif kind == "tool_end":
                    mark = "✓" if payload["status"] == "ok" else "✗"
                    yield _sse_event("tool", {
                        "phase": "end", "name": payload["name"],
                        "status": payload["status"],
                        "duration_ms": payload["duration_ms"],
                        "result_preview": str(payload["result"])[:500],
                        "line": f"{mark} {payload['name']} "
                                f"已返回（{payload['duration_ms']}ms）"})
                elif kind == "done":
                    outcome = "ok"
                    yield _sse_event("done", payload)
                elif kind == "error":
                    outcome = "error"
                    yield _sse_event("error", payload)
        finally:
            # 流结束（正常、报错、中途断开都会走到这里）记一行日志。
            result = outcome or "cancelled"
            _log_chat((time.monotonic() - start) * 1000,
                      result == "ok", result, key_source)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
