"""检查点的离线测试：不用 key、不联网。

验证：
  1. snapshot/rewind：写前备份，恢复后文件回到备份时的内容
  2. rewind_to：恢复到某个时间点之前的状态（悬停恢复检查点用）
  3. snapshot_if_active：返回钩子约定的 (ok, message)，注册后写文件
     不再被拦（U5 修的 TUI bug：之前返回 None，解包失败导致所有
     写文件都被拦）
  4. register_before_write_hook：同一个函数只注册一次

Run:  python test_checkpoint.py
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis.memory.checkpoint import (
    CheckpointStore, current_store, set_current_store, snapshot_if_active)
from jarvis.tools import tools

ORIGINAL_DIR = tools.PROJECT_DIR


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_snapshot_rewind_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "a.txt")
        _write(target, "v1")
        store = CheckpointStore("s1", root=os.path.join(tmp, "ckpts"))
        assert store.snapshot(target) is not None
        _write(target, "v2")
        assert store.snapshot(target) is not None
        _write(target, "v3")
        restored = store.rewind(1)
        assert restored == [target], restored
        assert _read(target) == "v2", _read(target)
        restored = store.rewind(1)
        assert _read(target) == "v1", _read(target)
        assert store.list_checkpoints() == [], store.list_checkpoints()
    print("[OK] 检查点：备份/恢复往返，文件回到备份时的内容")


def test_rewind_to_timestamp():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "a.txt")
        store = CheckpointStore("s1", root=os.path.join(tmp, "ckpts"))
        _write(target, "v1")
        store.snapshot(target)
        time.sleep(0.01)
        _write(target, "v2")
        store.snapshot(target)
        rows = store.list_checkpoints()
        time.sleep(0.01)
        _write(target, "v3")
        store.snapshot(target)
        # 回到第二份备份之前：文件应为 v2，第一份检查点保留
        restored = store.rewind_to(rows[1]["ts"])
        assert _read(target) == "v2", _read(target)
        assert len(store.list_checkpoints()) == 1, store.list_checkpoints()
        # 回到第一份之前：文件回到 v1
        restored = store.rewind_to(rows[0]["ts"])
        assert _read(target) == "v1", _read(target)
        assert store.list_checkpoints() == [], store.list_checkpoints()
    print("[OK] 检查点 rewind_to：按时间点恢复，旧检查点保留")


def test_snapshot_hook_contract():
    """U5 修的 bug：snapshot_if_active 必须返回 (True, "")，否则
    _run_before_write_hooks 解包失败，所有写文件都被拦。"""
    assert snapshot_if_active("/no/such/file") == (True, "")
    with tempfile.TemporaryDirectory() as tmp:
        tools.PROJECT_DIR = tmp
        old_hooks = list(tools._BEFORE_WRITE_HOOKS)
        old_store = current_store()
        try:
            store = CheckpointStore("s1", root=os.path.join(tmp, "ckpts"))
            set_current_store(store)
            tools.register_before_write_hook(snapshot_if_active)
            result = tools.write_text_file("a.txt", "hello")
            assert result.startswith("已写入"), result
            assert _read(os.path.join(tmp, "a.txt")) == "hello"
            # 同一个钩子再注册一次也不重复
            tools.register_before_write_hook(snapshot_if_active)
            assert tools._BEFORE_WRITE_HOOKS.count(snapshot_if_active) == 1
        finally:
            tools._BEFORE_WRITE_HOOKS[:] = old_hooks
            set_current_store(old_store)
            tools.PROJECT_DIR = ORIGINAL_DIR
    print("[OK] 检查点钩子：返回 (True, \"\")，注册后写文件正常且只注册一次")


if __name__ == "__main__":
    test_snapshot_rewind_roundtrip()
    test_rewind_to_timestamp()
    test_snapshot_hook_contract()
    print("全部通过。")
