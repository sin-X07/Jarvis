"""Jarvis web server: the same agent loop, over HTTP + SSE.

Same core as the desktop GUI (agent.py / tools.py / memory.py), different
shell: the browser POSTs to /api/chat and reads Server-Sent Events back.
No PySide6 needed here -- this is the part Vercel runs.

Run locally:
    uvicorn server:app --reload
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

from agent import SYSTEM_PROMPT, build_client, run_agent
from memory import ConversationMemory, FactStore, memory_tools
from usage import UsageTracker

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


def _system_prompt() -> str:
    facts = STORE.render_for_prompt()
    return f"{SYSTEM_PROMPT}\n{facts}" if facts else SYSTEM_PROMPT


def _run_agent_streamed(client, user_message, messages, schemas, funcs):
    """Run the loop in a thread; events come back through a queue.

    run_agent is synchronous and calls on_event as it goes. To forward
    those events to the browser while they happen, the loop runs in a
    worker thread and the SSE generator yields from the queue.
    """
    events = queue.Queue()

    def on_event(kind, payload):
        events.put((kind, payload))

    def target():
        started = time.monotonic()
        tracker = UsageTracker()
        try:
            answer = run_agent(
                client, user_message, history=messages,
                extra_tools=schemas, extra_funcs=funcs,
                on_event=on_event, stream=True, verbose=False,
                max_steps=WEB_MAX_STEPS, usage=tracker,
            )
            # 整轮裁剪后把更新过的历史（不含 system）交给 done 事件
            mem = ConversationMemory(_system_prompt())
            mem.messages = messages
            mem.trim()
            events.put(("done", {
                "answer": answer,
                "history": mem.messages[1:],
                "facts": STORE.all_texts(),
                "usage": {"tokens": tracker.total_tokens,
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
  * { box-sizing: border-box; }
  body { margin: 0; background: #1e1e2e; color: #cdd6f4;
         font: 15px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif; }
  #app { max-width: 860px; margin: 0 auto; height: 100vh;
         display: flex; flex-direction: column; padding: 16px; }
  header { display: flex; justify-content: space-between; align-items: baseline; }
  h1 { font-size: 20px; margin: 0; color: #89b4fa; }
  #status { color: #a6adc8; font-size: 13px; }
  #chat { flex: 1; overflow-y: auto; margin: 12px 0; }
  .msg { margin: 8px 0; padding: 8px 12px; border-radius: 10px;
         white-space: pre-wrap; word-break: break-word; }
  .user { background: #313244; margin-left: 15%; text-align: right; }
  .jarvis { background: #181825; margin-right: 15%; }
  .tool { background: none; color: #6c7086; font-style: italic;
          font-size: 13px; padding: 2px 12px; }
  .error { background: #181825; color: #f38ba8; }
  #row { display: flex; gap: 8px; }
  #input { flex: 1; background: #181825; color: #cdd6f4;
           border: 1px solid #313244; border-radius: 8px; padding: 10px;
           font: inherit; }
  button { background: #89b4fa; color: #1e1e2e; border: none;
           border-radius: 8px; padding: 10px 18px; font: inherit;
           cursor: pointer; }
  button:disabled { background: #45475a; color: #a6adc8; cursor: default; }
  #clear { background: none; color: #a6adc8; padding: 10px 8px; }
  #keybar { display: flex; gap: 8px; margin-top: 10px; align-items: center; }
  #key { flex: 1; background: #181825; color: #cdd6f4;
         border: 1px solid #313244; border-radius: 8px; padding: 8px;
         font: inherit; }
  #keybar button { padding: 8px 12px; }
  #keystatus { color: #a6adc8; font-size: 13px; white-space: nowrap; }
  #toolbar { display: flex; gap: 8px; margin-top: 10px; align-items: center; }
  #toolbar select { flex: 1; background: #181825; color: #cdd6f4;
         border: 1px solid #313244; border-radius: 8px; padding: 8px; font: inherit; }
  #toolbar button { padding: 8px 12px; }
  #usage { color: #a6adc8; font-size: 13px; margin-top: 8px; }
  #trajectory { background: #181825; border: 1px solid #313244; border-radius: 8px;
         padding: 8px; margin-top: 8px; font-size: 13px; color: #a6adc8;
         max-height: 160px; overflow-y: auto; }
  .msg pre { background: #11111b; padding: 8px; border-radius: 6px; overflow-x: auto; }
  .msg code { background: #11111b; padding: 1px 4px; border-radius: 4px; }
</style>
</head>
<body>
<div id="app">
  <header><h1>Jarvis</h1><span id="status">就绪</span></header>
  <div id="keybar">
    <input id="key" type="password" placeholder="你的 DeepSeek API key（sk-…），只存在你的浏览器里" autocomplete="off">
    <button id="savekey">保存</button>
    <button id="clearkey" title="清除浏览器里存的 key">清除</button>
    <span id="keystatus"></span>
  </div>
  <div id="toolbar">
    <select id="sessions"></select>
    <button id="newsession">新会话</button>
    <button id="rewind" title="撤销上一轮（删掉最近一问一答）">撤销上一轮</button>
    <button id="timeline" title="显示/隐藏对话时间线">时间线</button>
  </div>
  <div id="usage">用量：—</div>
  <div id="trajectory" style="display:none"></div>
  <div id="chat"></div>
  <div id="row">
    <input id="input" placeholder="和 Jarvis 说点什么…（回车发送）" autocomplete="off">
    <button id="send">发送</button>
    <button id="clear" title="清空对话（长期记忆还在）">清空</button>
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
const sessionsEl = document.getElementById('sessions');

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
let busy = false;

function refreshSessionSelect() {
  const cur = currentSession;
  sessionsEl.innerHTML = '';
  Object.entries(sessions).forEach(([id, s]) => {
    const opt = document.createElement('option');
    opt.value = id;
    opt.textContent = (s.title || '新会话').slice(0, 20) + ' (' + (s.history || []).length + ' 条)';
    sessionsEl.appendChild(opt);
  });
  sessionsEl.value = cur;
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
  // 极简 Markdown：代码块、行内代码、粗体、标题、列表（和 md.py 同款子集）
  const lines = text.split('\\n');
  let out = '', inCode = false;
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
      if (inCode) { out += '</code></pre>'; inCode = false; }
      else { flushList(); out += '<pre><code>'; inCode = true; }
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
  if (inCode) out += '</code></pre>';
  return out;
}

function addMsg(role, text) {
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  if (role === 'jarvis') div.innerHTML = md2html(text);
  else div.textContent = text;
  chatEl.appendChild(div);
  chatEl.scrollTop = chatEl.scrollHeight;
  return div;
}

function redraw() {
  chatEl.innerHTML = '';
  history.forEach(m => {
    if (m.role === 'user') addMsg('user', m.content);
    else if (m.role === 'assistant' && m.content) addMsg('jarvis', m.content);
  });
  if (!history.length) {
    addMsg('jarvis', 'Jarvis 已启动。直接说话，或者先记住点什么：“记住，我最喜欢蓝色”。');
  }
}

// 刷新后按保存的历史重画（只画 user/assistant 的文字，tool 消息不单独画）。
redraw();
if (!history.length && !apiKey) {
  addMsg('jarvis', '先在上方填入你的 DeepSeek API key 再开始（key 只存在你的浏览器里）。');
}
refreshSessionSelect();

function setBusy(b) {
  busy = b;
  inputEl.disabled = b;
  sendBtn.disabled = b;
  if (!b) inputEl.focus();
}

async function send() {
  if (busy) return;
  const text = inputEl.value.trim();
  if (!text) return;
  inputEl.value = '';
  addMsg('user', text);
  setBusy(true);
  statusEl.textContent = '思考中…';
  let bubble = null;  // 流式回答的那个气泡，token 来了就往里加字

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, history, facts, api_key: apiKey }),
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
          if (!bubble) bubble = addMsg('jarvis', '');
          bubble.textContent += payload.text;
          chatEl.scrollTop = chatEl.scrollHeight;
        } else if (event === 'tool') {
          addMsg('tool', payload.line);
          statusEl.textContent = payload.line;
        } else if (event === 'done') {
          history = payload.history;
          facts = payload.facts;
          save();
          if (bubble && payload.answer) bubble.innerHTML = md2html(payload.answer);
          const u = payload.usage || {};
          usageEl.textContent = '用量：本次 ' + (u.tokens || 0) + ' tokens，约 ¥'
            + (u.cost_cny || 0).toFixed(4) + '，耗时 '
            + ((payload.duration_ms || 0) / 1000).toFixed(1) + ' 秒';
          statusEl.textContent = '就绪';
        } else if (event === 'error') {
          throw new Error(payload.message);
        }
      }
    }
    // 没收到过 token（比如模型一次答完）：按 done 里的回答补一个气泡。
    if (!bubble) {
      const last = history.filter(m => m.role === 'assistant').pop();
      if (last && last.content) addMsg('jarvis', last.content);
    }
  } catch (err) {
    addMsg('error', '出错了：' + err.message);
    statusEl.textContent = '出错';
    if (err.message.includes('API key')) keyEl.focus();
  } finally {
    setBusy(false);
  }
}

sessionsEl.addEventListener('change', () => {
  if (busy) { sessionsEl.value = currentSession; return; }
  currentSession = sessionsEl.value;
  localStorage.setItem('jarvis-current-session', currentSession);
  history = sessions[currentSession].history || [];
  facts = sessions[currentSession].facts || [];
  redraw();
  statusEl.textContent = '已切换会话';
});
document.getElementById('newsession').addEventListener('click', () => {
  if (busy) return;
  currentSession = 's' + Date.now().toString(36);
  sessions[currentSession] = { title: '新会话', history: [], facts };
  history = [];
  save();
  redraw();
  statusEl.textContent = '新会话';
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

sendBtn.addEventListener('click', send);
inputEl.addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
document.getElementById('clear').addEventListener('click', () => {
  if (busy) return;
  history = [];
  save();
  chatEl.innerHTML = '';
  addMsg('jarvis', '对话已清空，长期记忆还在。');
});
inputEl.focus();
</script>
</body>
</html>"""


@app.get("/")
def index():
    return HTMLResponse(INDEX_HTML)


@app.get("/api/health")
def health():
    return {"ok": True}


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
                    client, req.message, messages, schemas, funcs):
                if kind == "token":
                    yield _sse_event("token", {"text": payload})
                elif kind == "tool_call":
                    args = str(payload["args"])
                    if len(args) > 80:
                        args = args[:80] + "…"
                    yield _sse_event("tool", {"line": f"▸ 调用 {payload['name']}({args})"})
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
