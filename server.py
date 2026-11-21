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
"""

import json
import queue
import threading

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from agent import SYSTEM_PROMPT, build_client, run_agent
from memory import ConversationMemory, FactStore, memory_tools

# 网页版默认最多 6 步：Vercel 免费版函数最长跑 60 秒，步数太多容易超时。
WEB_MAX_STEPS = 6
# 历史上限（条消息数，不含 system），和 ConversationMemory 的默认一致。
HISTORY_LIMIT = 30

app = FastAPI(title="Jarvis")

# 进程内的事实库：启动时从 memory.json 读（本地有的话），运行中新增的
# 事实尽量写盘；写盘失败（Vercel 只读环境）也不报错，见 memory.py。
STORE = FactStore()

_client = None


def get_client():
    """Lazy client: no API key -> fail the request, not the import."""
    global _client
    if _client is None:
        _client = build_client()
    return _client


class ChatRequest(BaseModel):
    message: str
    history: list = []  # 浏览器攒的对话：user/assistant/tool 消息，不含 system
    facts: list = []    # 浏览器 localStorage 里的长期事实


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
        try:
            answer = run_agent(
                client, user_message, history=messages,
                extra_tools=schemas, extra_funcs=funcs,
                on_event=on_event, stream=True, verbose=False,
                max_steps=WEB_MAX_STEPS,
            )
            # 整轮裁剪后把更新过的历史（不含 system）交给 done 事件
            mem = ConversationMemory(_system_prompt())
            mem.messages = messages
            mem.trim()
            events.put(("done", {
                "answer": answer,
                "history": mem.messages[1:],
                "facts": STORE.all_texts(),
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
</style>
</head>
<body>
<div id="app">
  <header><h1>Jarvis</h1><span id="status">就绪</span></header>
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

// 浏览器是状态的家：历史和长期事实存在 localStorage，每次请求一起发过去。
let history = JSON.parse(localStorage.getItem('jarvis-history') || '[]');
let facts = JSON.parse(localStorage.getItem('jarvis-facts') || '[]');
let busy = false;

function save() {
  localStorage.setItem('jarvis-history', JSON.stringify(history));
  localStorage.setItem('jarvis-facts', JSON.stringify(facts));
}

function addMsg(role, text) {
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  div.textContent = text;
  chatEl.appendChild(div);
  chatEl.scrollTop = chatEl.scrollHeight;
  return div;
}

// 刷新后按保存的历史重画（只画 user/assistant 的文字，tool 消息不单独画）。
history.forEach(m => {
  if (m.role === 'user') addMsg('user', m.content);
  else if (m.role === 'assistant' && m.content) addMsg('jarvis', m.content);
});
if (!history.length) addMsg('jarvis', 'Jarvis 已启动。直接说话，或者先记住点什么：“记住，我最喜欢蓝色”。');

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
      body: JSON.stringify({ message: text, history, facts }),
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
  } finally {
    setBusy(false);
  }
}

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
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="message 不能为空")
    try:
        client = get_client()
    except SystemExit as exc:
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
                yield _sse_event("done", payload)
            elif kind == "error":
                yield _sse_event("error", payload)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
