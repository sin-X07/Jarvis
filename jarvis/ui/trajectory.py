"""轨迹回放（P8-1）：把会话 JSONL 画成一条时间线。

会话文件里每行是一个事件（user / assistant / tool_call /
tool_result / summary / todo / plan），这个模块只读不写，按时间
顺序排成一行行给人看：谁说了什么、调了哪个工具、工具返回了
多少字、哪一段被压缩成了摘要。

三个出口共用同一个 render_trajectory()：
  CLI：jarvis --trajectory <会话id>
  GUI：“轨迹”按钮弹对话框
  网页：页面里的“时间线”按钮（浏览器 localStorage 版，不走这里）

U0-2 新增：`iter_turns(events)` 把事件按轮次分组（一条 user 事件
开一轮）；`replay_transcript(session_id, store)` 把 JSONL 日志重放
成界面要画的条目列表（断线重连、恢复会话、TUI 续跑都用它）。
事件类型说明：日志里的 type 和 UI 事件名是两套词——日志记
user / assistant / tool_call / tool_result / summary / todo /
plan / error；UI 事件名见 jarvis/ui/events.py。
"""

from jarvis.memory.session_store import SessionStore


def _short(text, limit: int = 80) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def iter_turns(events: list) -> list:
    """把事件按轮次分组：每条 user 事件开一轮；轮次前的 meta 不算轮。

    返回 [[事件, ...], ...]。空列表返回 []。
    """
    turns = []
    current = None
    for event in events:
        kind = event.get("type", "")
        if kind == "user":
            current = [event]
            turns.append(current)
        elif current is not None:
            current.append(event)
    return turns


def replay_transcript(session_id: str, store: SessionStore | None = None) -> list:
    """把会话 JSONL 重放成界面要画的条目列表（断线重连、恢复会话用）。

    每条是 {"kind": ..., "ts": ..., ...}：kind ∈ user / assistant /
    tool_call / tool_result / summary / todo / plan / error；assistant
    条目带 content 与 tool_calls；tool_result 条目带 name 与 result。
    会话不存在返回 []。
    """
    store = store or SessionStore()
    items = []
    for event in store.load_events(session_id):
        kind = event.get("type", "")
        if kind == "meta":
            continue
        item = {"kind": kind, "ts": str(event.get("ts", ""))}
        if kind == "user":
            item["content"] = event.get("content", "")
        elif kind == "assistant":
            item["content"] = event.get("content")
            item["tool_calls"] = event.get("tool_calls") or []
        elif kind in ("tool_call", "tool_result"):
            item["name"] = event.get("name", "?")
            item["args"] = event.get("args") or {}
            item["result"] = event.get("result", "")
            item["tool_call_id"] = event.get("tool_call_id", "")
        elif kind in ("summary", "todo", "plan", "error"):
            item["content"] = event.get("content") or event.get("message", "")
        else:
            continue
        items.append(item)
    return items


def render_trajectory(session_id: str, store: SessionStore | None = None) -> str:
    """把一个会话的事件画成时间线文本；会话不存在返回提示。"""
    store = store or SessionStore()
    events = store.load_events(session_id)
    if not events:
        return f"没有找到会话 {session_id}（可能 id 写错了，或还没有会话）。"
    lines = [f"会话 {session_id} 的轨迹（共 {len(events)} 个事件）：", ""]
    for event in events:
        kind = event.get("type", "")
        ts = str(event.get("ts", ""))[11:19]  # 只要 HH:MM:SS
        if kind == "meta":
            title = event.get("title") or "(无标题)"
            lines.append(f"{ts}  会话开始：{title}")
        elif kind == "user":
            lines.append(f"{ts}  你：{_short(event.get('content'))}")
        elif kind == "assistant":
            content = event.get("content")
            if event.get("tool_calls"):
                names = ", ".join(
                    c.get("function", {}).get("name", "?")
                    for c in event["tool_calls"])
                lines.append(f"{ts}  Jarvis：调工具 [{names}]"
                             + (f" {_short(content)}" if content else ""))
            elif content:
                lines.append(f"{ts}  Jarvis：{_short(content)}")
        elif kind == "tool_call":
            args = event.get("args") or {}
            lines.append(f"{ts}  ▸ 调用 {event.get('name', '?')}"
                         f"({_short(args, 60)})")
        elif kind == "tool_result":
            result = str(event.get("result", ""))
            lines.append(f"{ts}  ✓ {event.get('name', '?')} 返回"
                         f"（{len(result)} 字）")
        elif kind == "summary":
            lines.append(f"{ts}  … 旧对话已压缩成摘要："
                         f"{_short(event.get('content'), 60)}")
        elif kind == "todo":
            lines.append(f"{ts}  ☑ 待办：{_short(event.get('content'))}")
        elif kind == "plan":
            lines.append(f"{ts}  计划：{_short(event.get('content'))}")
        elif kind == "error":
            lines.append(f"{ts}  ✗ 出错：{_short(event.get('message'), 60)}")
    return "\n".join(lines)
