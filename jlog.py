"""Jarvis 结构化日志（P0-5）。

一句话一个 JSON 行，写到 stdout：
    {"event": "chat", "level": "info", "ts": "...", ...}

规矩和 server.py 的 _log_chat 一样：只记调用摘要（时间、耗时、成败、
用量），绝不记消息内容、历史、事实，更不记 key。CLI / GUI / 网页
三端都用这一套，查问题时 grep 一个 event 名就能捞出所有相关行。
"""

import json
import sys
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(event: str, level: str = "info", **fields) -> None:
    """记一行 JSON 日志。fields 里不要放任何敏感内容。"""
    record = {"event": event, "level": level, "ts": _now_iso()}
    record.update(fields)
    print(json.dumps(record, ensure_ascii=False), file=sys.stdout, flush=True)


def log_chat(duration_ms, ok, reason, key_source="local", **extra) -> None:
    """一次 agent 调用的摘要行（CLI/GUI 版，对应网页版的 _log_chat）。"""
    log_event("chat", duration_ms=round(duration_ms), ok=ok,
              reason=reason, key_source=key_source, **extra)


def log_tool(name, ok, duration_ms, **extra) -> None:
    """一次工具调用的摘要行：只记工具名和成败，不记参数和返回内容。"""
    log_event("tool", name=name, ok=ok,
              duration_ms=round(duration_ms), **extra)


def log_error(event, message, **extra) -> None:
    """出错行：message 只写错误类型和一句话原因，不带原文。"""
    log_event(event, level="error", message=str(message)[:200], **extra)
