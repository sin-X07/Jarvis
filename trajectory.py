"""轨迹回放（P8-1）：把会话 JSONL 画成一条时间线。

会话文件里每行是一个事件（user / assistant / tool_call /
tool_result / summary / todo / plan），这个模块只读不写，按时间
顺序排成一行行给人看：谁说了什么、调了哪个工具、工具返回了
多少字、哪一段被压缩成了摘要。

三个出口共用同一个 render_trajectory()：
  CLI：jarvis --trajectory <会话id>
  GUI：“轨迹”按钮弹对话框
  网页：页面里的“时间线”按钮（浏览器 localStorage 版，不走这里）
"""

from session_store import SessionStore


def _short(text, limit: int = 80) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


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
    return "\n".join(lines)
