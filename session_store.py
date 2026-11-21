"""会话落盘（P3-1 / P3-2）：append-only 会话日志。

设计抄 DeepSeek Harness + Codex：
- 每个会话一个文件：~/.jarvis/sessions/<id>.jsonl，一行一个事件，
  只追加不修改。恢复、回放、审计都从这份日志来，不从内存里拼。
- 事件类型：meta（会话元信息）/ user / assistant / tool_call /
  tool_result / summary（压缩后留下的摘要）/ todo / plan。
- assistant 事件里自带 tool_calls；tool_result 事件带 tool_call_id，
  恢复时按 id 配对，拼回 OpenAI 要求的消息序列。
- 会话 id：时间戳 + 4 位随机后缀，如 20261121-152030-a1b2。

注意：日志里有对话内容（这是会话恢复的前提），但绝没有 key。
日志只存在用户自己的电脑上，不上传。
"""

import json
import os
import random
import string
from datetime import datetime, timezone

from config import default_config_dir


def _new_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return stamp + "-" + suffix


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionStore:
    """会话仓库：创建、追加事件、恢复消息、列表、分支、删除。"""

    def __init__(self, root: str | None = None):
        self.root = root or os.path.join(default_config_dir(), "sessions")
        os.makedirs(self.root, exist_ok=True)

    def _path(self, session_id: str) -> str:
        return os.path.join(self.root, session_id + ".jsonl")

    def create(self, title: str = "", model: str = "") -> str:
        session_id = _new_id()
        self.append_event(session_id, "meta", {
            "id": session_id, "title": title, "model": model,
            "created_at": _now_iso()})
        return session_id

    def append_event(self, session_id: str, event_type: str, payload: dict) -> None:
        event = {"ts": _now_iso(), "type": event_type}
        event.update(payload or {})
        try:
            with open(self._path(session_id), "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 落盘失败不拦主流程（内存里的对话照常进行）

    def load_events(self, session_id: str) -> list:
        events = []
        try:
            with open(self._path(session_id), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []
        return events

    def load_messages(self, session_id: str) -> list:
        """把事件还原成 OpenAI 消息列表（不含 system，调用方自己拼）。

        规则：user 事件 → user 消息；assistant 事件 → assistant 消息
        （含 tool_calls）；tool_result 事件 → tool 消息（按 tool_call_id
        配对）；summary 事件 → 一条 user 消息（此前对话摘要）。
        """
        messages = []
        for event in self.load_events(session_id):
            kind = event.get("type")
            if kind == "user":
                messages.append({"role": "user", "content": event.get("content", "")})
            elif kind == "assistant":
                msg = {"role": "assistant", "content": event.get("content")}
                if event.get("tool_calls"):
                    msg["tool_calls"] = event["tool_calls"]
                messages.append(msg)
            elif kind == "tool_result":
                messages.append({
                    "role": "tool",
                    "tool_call_id": event.get("tool_call_id", ""),
                    "content": event.get("result", ""),
                })
            elif kind == "summary":
                messages.append({
                    "role": "user",
                    "content": "【此前对话摘要】\n" + event.get("content", ""),
                })
        return messages

    def list_sessions(self) -> list:
        """列出所有会话：id、标题、创建/更新时间、轮数（按更新时间倒序）。"""
        out = []
        try:
            names = os.listdir(self.root)
        except OSError:
            return []
        for name in sorted(names, reverse=True):
            if not name.endswith(".jsonl"):
                continue
            session_id = name[:-len(".jsonl")]
            events = self.load_events(session_id)
            if not events:
                continue
            meta = next((e for e in events if e.get("type") == "meta"), {})
            turns = sum(1 for e in events if e.get("type") == "user")
            updated = events[-1].get("ts", "") if events else ""
            out.append({
                "id": session_id,
                "title": meta.get("title") or self._auto_title(events),
                "created_at": meta.get("created_at", ""),
                "updated_at": updated,
                "turns": turns,
            })
        return out

    @staticmethod
    def _auto_title(events: list) -> str:
        for event in events:
            if event.get("type") == "user" and event.get("content"):
                text = str(event["content"]).strip().split("\n")[0]
                return text[:30] + ("…" if len(text) > 30 else "")
        return "(空会话)"

    def set_title(self, session_id: str, title: str) -> None:
        self.append_event(session_id, "meta", {"title": title})

    def fork(self, session_id: str) -> str:
        """从当前点分支：复制全部事件到一个新会话，返回新 id。"""
        events = self.load_events(session_id)
        new_id = self.create(title="fork 自 " + session_id)
        for event in events:
            if event.get("type") == "meta":
                continue
            self.append_event(new_id, event.get("type", "user"),
                              {k: v for k, v in event.items()
                               if k not in ("ts", "type")})
        return new_id

    def delete(self, session_id: str) -> bool:
        try:
            os.remove(self._path(session_id))
            return True
        except OSError:
            return False
