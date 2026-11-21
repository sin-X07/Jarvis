"""检查点与回滚（P4-3）：写文件前自动备份，改坏了能撤销。

原理很简单：每次写文件前，把原文件复制一份到检查点目录；回滚时
把最近的一份复制回去。检查点存在用户目录
~/.jarvis/checkpoints/<会话id>/，不污染项目仓库（计划原文写的是
项目内 .jarvis，这里改到用户目录，和会话、审计放一起）。

- snapshot(path)：备份一次，返回检查点 id；原文件不存在时不备份。
- rewind(steps)：恢复最近 steps 次备份（默认 1 次），返回恢复的文件。
- 只保留最近 50 个检查点，老的自动清理。
- snapshot_hook(path)：tools.py 写前钩子的适配器，签名 (ok, message)。
"""

import json
import os
import shutil
from datetime import datetime, timezone

from jarvis.core.config import default_config_dir

MAX_KEEP = 50


class CheckpointStore:
    def __init__(self, session_id: str, root: str | None = None):
        self.session_id = session_id
        self.root = root or os.path.join(
            default_config_dir(), "checkpoints", session_id)
        os.makedirs(self.root, exist_ok=True)
        self._manifest_path = os.path.join(self.root, "manifest.json")
        self._manifest = self._load_manifest()

    def _load_manifest(self) -> list:
        try:
            with open(self._manifest_path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _save_manifest(self) -> None:
        try:
            with open(self._manifest_path, "w", encoding="utf-8") as f:
                json.dump(self._manifest, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def snapshot(self, path: str):
        """备份 path 的当前内容，返回检查点 id；文件不存在返回 None。"""
        if not os.path.isfile(path):
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        name = os.path.basename(path)
        ckpt_id = stamp + "-" + name
        dest = os.path.join(self.root, ckpt_id)
        try:
            shutil.copy2(path, dest)
        except OSError:
            return None
        self._manifest.append({
            "id": ckpt_id, "path": os.path.abspath(path),
            "ts": datetime.now(timezone.utc).isoformat()})
        self._manifest = self._manifest[-MAX_KEEP:]
        self._save_manifest()
        return ckpt_id

    def list_checkpoints(self) -> list:
        return [dict(row) for row in self._manifest]

    def rewind(self, steps: int = 1) -> list:
        """恢复最近 steps 次备份，返回恢复的文件路径列表。

        按时间倒序恢复：最近一次备份先恢复；同一文件的多次备份，
        恢复后以更早的那份为准（也就是回到 steps 次写入之前）。
        """
        restored = []
        for _ in range(max(1, steps)):
            if not self._manifest:
                break
            row = self._manifest.pop()
            src = os.path.join(self.root, row["id"])
            dest = row["path"]
            try:
                if os.path.isfile(src):
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.copy2(src, dest)
                    restored.append(dest)
                if os.path.isfile(src):
                    os.remove(src)
            except OSError:
                continue
        self._save_manifest()
        return restored

    def snapshot_hook(self, path: str):
        """tools.py 写前钩子：备份，失败也不拦写入（备份是保险，不是门禁）。"""
        try:
            self.snapshot(path)
        except Exception:
            pass
        return True, ""


# -- 进程内当前检查点库：CLI/GUI 启动会话时装上 ----------------------

_CURRENT = None


def set_current_store(store) -> None:
    global _CURRENT
    _CURRENT = store


def current_store():
    return _CURRENT


def snapshot_if_active(path: str) -> None:
    """如果当前装了检查点库，备份 path 一次（供钩子注册时调用）。"""
    store = current_store()
    if store is not None:
        store.snapshot(path)
