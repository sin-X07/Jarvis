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

from jarvis.ui.gui import MainWindow
from jarvis.memory.memory import FactStore
from test_stream import FakeStreamClient, _chunk
from jarvis.ui.worker import AgentWorker


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


def test_main_window_p7_widgets():
    app = _app()
    import os as _os
    import tempfile
    from jarvis.memory.session_store import SessionStore
    tmp = tempfile.mkdtemp()
    store = SessionStore(root=_os.path.join(tmp, "sessions"))
    sid = store.create(title="测试会话")
    store.append_event(sid, "user", {"content": "你好"})
    client = FakeStreamClient([[_chunk("hi")]])
    window = MainWindow(client, session_store=store)
    assert window.windowTitle() == "Jarvis"
    assert window.memory_list.count() == len(FactStore().all_texts())
    assert window.chat_view.toPlainText().startswith("Jarvis 已启动")
    # P7 新增控件都在
    assert window.session_list.count() == 1, window.session_list.count()
    assert window.session_list.item(0).data(0x0100) == sid
    assert window.settings_button.text() == "设置"
    assert window.rewind_button.text() == "恢复检查点"
    assert window.trajectory_button.text() == "轨迹"
    assert window.new_session_button.text() == "新会话"
    assert window.usage_label.text().startswith("用量：")
    assert window.permission_label.text().startswith("权限：")
    # 点会话能恢复（消息回填）
    window.resume_session(sid)
    assert "你好" in window.chat_view.toPlainText()
    window.close()
    print("[OK] 主窗口 P7：会话列表、设置/恢复/新会话按钮、用量标签都在，点会话能恢复")


def test_settings_dialog():
    _app()
    from jarvis.ui import gui
    from jarvis.core.config import load_config
    from jarvis.ui.gui import SettingsDialog
    config = load_config()
    dialog = SettingsDialog(config)
    assert dialog.model_edit.text() == config.model
    assert dialog.permission_box.currentText() == config.permission
    dialog.model_edit.setText("deepseek-chat")
    dialog.permission_box.setCurrentText("readonly")
    dialog.steps_spin.setValue(3)
    saved = {}
    real_save = gui.save_config
    gui.save_config = lambda cfg: saved.update(model=cfg.model,
                                               permission=cfg.permission,
                                               max_steps=cfg.max_steps)
    try:
        dialog.apply()
    finally:
        gui.save_config = real_save
    assert saved == {"model": "deepseek-chat", "permission": "readonly",
                     "max_steps": 3}, saved
    print("[OK] 设置对话框：改模型/档位/步数，保存时写回配置")


def test_worker_confirm_flow():
    """写操作在 edit 档位弹确认：worker 发信号，主线程回 True，写成功。"""
    import tempfile
    from jarvis.tools import tools
    from jarvis.safety import audit as audit_mod
    from jarvis.safety.audit import AuditLog
    from jarvis.safety.permissions import ApprovalGate
    from test_stream import FakeStreamClient, _chunk, _tool_delta

    _app()
    tmp = tempfile.mkdtemp()
    old_proj, old_shell_proj = tools.PROJECT_DIR, None
    from jarvis.tools import shell_tools
    old_shell_proj = shell_tools.PROJECT_DIR
    old_audit = audit_mod._DEFAULT
    old_hooks = list(tools._BEFORE_WRITE_HOOKS)
    tools.PROJECT_DIR = tmp
    shell_tools.PROJECT_DIR = tmp
    audit_mod._DEFAULT = AuditLog(os.path.join(tmp, "audit.jsonl"))
    tools._BEFORE_WRITE_HOOKS.clear()
    client = FakeStreamClient([
        [_chunk(tool_calls=[_tool_delta(
            0, "c1", "write_text_file", '{"path": "hello.txt",')]),
         _chunk(tool_calls=[_tool_delta(
             0, None, None, ' "content": "hi\\n"}')])],
        [_chunk("写好了")],
    ])
    gate = ApprovalGate("edit")
    worker = AgentWorker(client, "写个文件", [
        {"role": "system", "content": "你是 Jarvis。"},
    ], gate=gate)
    answered = {"n": 0}

    def _answer(action):
        answered["n"] += 1
        assert action["kind"] == "write_file", action
        worker.answer_confirm(True)

    worker.confirm_requested.connect(_answer)
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    got = {"finished": None, "error": None}
    worker.finished.connect(lambda a: got.update(finished=a))
    worker.error.connect(lambda e: got.update(error=e))
    worker.finished.connect(thread.quit)
    worker.error.connect(thread.quit)
    thread.start()
    ok = _pump_until(lambda: got["finished"] is not None or got["error"] is not None)
    thread.wait(3000)
    tools.PROJECT_DIR = old_proj
    shell_tools.PROJECT_DIR = old_shell_proj
    audit_mod._DEFAULT = old_audit
    tools._BEFORE_WRITE_HOOKS[:] = old_hooks
    assert ok, f"worker 没结束：{got}"
    assert got["error"] is None, got["error"]
    assert got["finished"] == "写好了", got
    assert answered["n"] == 1, answered
    assert open(os.path.join(tmp, "hello.txt")).read() == "hi\n"
    print("[OK] Worker 确认：edit 档位写文件先问用户，点了同意才写")


if __name__ == "__main__":
    test_worker_signals()
    test_main_window_builds()
    test_main_window_p7_widgets()
    test_settings_dialog()
    test_worker_confirm_flow()
    print("全部通过。")
