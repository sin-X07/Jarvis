"""操作审计日志（P1-6）。

每次写文件、跑命令、git 提交，都往 ~/.jarvis/audit.jsonl 追加一行：
    {"ts": ..., "action": "write_file", "target": "notes/todo.txt",
     "ok": true, "detail": "1234 字节"}

只记“干了什么”，不记内容：detail 里不放文件内容、命令输出、对话
内容，更不放 key。出问题回查时，按时间grep 就能还原操作序列。
"""

import json
import os
from datetime import datetime, timezone

from config import default_config_dir


class AuditLog:
    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(default_config_dir(), "audit.jsonl")

    def record(self, action: str, target: str = "",
               ok: bool = True, detail: str = "") -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "action": action,
                    "target": target,
                    "ok": ok,
                    "detail": detail[:500],
                }, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 审计记不下来不拦主流程

    def recent(self, limit: int = 50) -> list:
        """读最近 limit 条审计（给 /audit 命令和回查用）。"""
        try:
            with open(self.path, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return []
        rows = []
        for line in lines[-limit:]:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows


_DEFAULT = AuditLog()


def audit(action: str, target: str = "", ok: bool = True, detail: str = "") -> None:
    """往默认审计日志记一行。"""
    _DEFAULT.record(action, target, ok, detail)
