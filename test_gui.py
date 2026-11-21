"""GUI 的离线冒烟测试：不用 key、不联网、不用真显示器。

QT_QPA_PLATFORM=offscreen 让 Qt 在没有显示器的环境里也能建窗口。
验证两件事：
  1. AgentWorker 在 QThread 里跑流式假模型，token/finished 信号能收到
  2. 主窗口能建起来，记忆侧栏正常

Run:  python test_gui.py
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from types import SimpleNamespace

from PySide6.QtCore import QEventLoop, QThread, QTimer
from PySide6.QtWidgets import QApplication

from gui import MainWindow
from memory import FactStore
from test_stream import FakeStreamClient, _chunk
from worker import AgentWorker


def _app():
    app = QApplication.instance()
    return app or QApplication(sys.argv)


def _pump_until(cond, timeout_ms=5000):
    """跑 Qt 事件循环直到 cond() 为真或超时。"""
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(50)
    timer.timeout.connect(lambda: loop.quit() if cond() else None)
    timer.start()
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    timer.stop()
    return cond()


def test_worker_signals():
    _app()
    client = FakeStreamClient([
        [_chunk("你好"), _chunk("，我是 Jarvis。")],
    ])
    worker = AgentWorker(client, "你好", [
        {"role": "system", "content": "你是 Jarvis。"},
    ])
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    got = {"tokens": [], "finished": None, "error": None}
    worker.token.connect(lambda t: got["tokens"].append(t))
    worker.finished.connect(lambda a: got.update(finished=a))
    worker.error.connect(lambda e: got.update(error=e))
    worker.finished.connect(thread.quit)
    worker.error.connect(thread.quit)
    thread.start()
    ok = _pump_until(lambda: got["finished"] is not None or got["error"] is not None)
    thread.wait(3000)
    assert ok, f"worker 没结束：{got}"
    assert got["error"] is None, got["error"]
    assert got["tokens"] == ["你好", "，我是 Jarvis。"], got["tokens"]
    assert got["finished"] == "你好，我是 Jarvis。", got["finished"]
    print("[OK] Worker：在 QThread 里跑完流式循环，token/finished 信号都收到了")


def test_main_window_builds():
    app = _app()
    client = FakeStreamClient([[_chunk("hi")]])
    window = MainWindow(client)
    assert window.windowTitle() == "Jarvis"
    # 记忆侧栏显示的是 memory.json 里的事实条数。本机可能已经存过事实，
    # 所以不断言为 0，只断言和仓库里的条数一致。
    assert window.memory_list.count() == len(FactStore().all_texts())
    assert window.chat_view.toPlainText().startswith("Jarvis 已启动")
    window.close()
    print("[OK] 主窗口：能建起来，记忆侧栏与欢迎语正常")


if __name__ == "__main__":
    test_worker_signals()
    test_main_window_builds()
    print("全部通过。")
