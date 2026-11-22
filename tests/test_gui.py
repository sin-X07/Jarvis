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

from PySide6.QtCore import QEventLoop, QThread
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton

from jarvis.ui.gui import MainWindow
from jarvis.memory.memory import FactStore
from test_stream import FakeStreamClient, _chunk, _tool_delta
from jarvis.ui.worker import AgentWorker


def _app():
    app = QApplication.instance()
    return app or QApplication(sys.argv)


_KEEP_ALIVE = []  # 跑完的 worker/thread 包装全程保活，进程退出时再一起释放


def _close_window(window):
    """关窗口并把挂起的删除事件跑完：MainWindow 销毁时会产生一批
    Shiboken 延迟删除，若攒到后面的 pump 才处理，队列里可能已有
    损坏的条目而段错误。关完就地处理，别攒。"""
    import gc as _gc
    app = QApplication.instance()
    window.close()
    if app is not None:
        app.processEvents(QEventLoop.AllEvents, 100)
    del window
    _gc.collect()
    if app is not None:
        app.processEvents(QEventLoop.AllEvents, 100)


def _pump_until(cond, timeout_ms=5000):
    """跑 Qt 事件循环直到 cond() 为真或超时。

    用 processEvents 轮询，不建嵌套 QEventLoop / QTimer：局部事件循环
    对象（loop、timer、singleShot 绑的 loop.quit）在函数返回后还可能
    被事件派发器碰到，曾导致段错误（QTimer::timeout 里 Shiboken 删
    已释放的包装对象）。轮询没有这类悬空对象。
    """
    import time as _time
    app = QApplication.instance()
    deadline = _time.monotonic() + timeout_ms / 1000.0
    while _time.monotonic() < deadline:
        if cond():
            return True
        if app is not None:
            app.processEvents(QEventLoop.AllEvents, 50)
        _time.sleep(0.01)
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
    thread.quit()  # 别等 finished→quit 的排队投递：poll 循环一真就返回，
    thread.wait(3000)  # wait() 又不处理事件，quit 送不到就死锁 3 秒
    assert not thread.isRunning(), "worker 线程没停下来"
    _KEEP_ALIVE.append((worker, thread))
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
    assert window.transcript_text().startswith("Jarvis 已启动")
    # U3：顶栏芯片 + 底部状态栏 + 右侧 Tab 都在
    assert window.model_label.text().startswith("模型：")
    assert window.permission_label.text().startswith("权限：")
    assert window.context_label.text().startswith("context ")
    assert window.usage_label.text().startswith("用量：")
    assert window.tabs.count() == 4
    assert [window.tabs.tabText(i) for i in range(4)] == [
        "会话", "待办", "Diff", "检查点"]
    assert window.send_button.text() == "发送"
    _close_window(window)
    print("[OK] 主窗口：能建起来，记忆侧栏与欢迎语正常，U3 芯片/状态栏/Tab 都在")


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
    assert window.transcript_text().startswith("Jarvis 已启动")
    # P7 控件（U3 布局：恢复检查点进了“检查点”Tab，不再是按钮）
    assert window.session_list.count() == 1, window.session_list.count()
    assert window.session_list.item(0).data(0x0100) == sid
    assert window.settings_button.text() == "设置"
    assert window.trajectory_button.text() == "轨迹"
    assert window.new_session_button.text() == "新会话"
    assert window.usage_label.text().startswith("用量：")
    assert window.permission_label.text().startswith("权限：")
    # 点会话能恢复（消息回填成气泡）
    window.resume_session(sid)
    assert "你好" in window.transcript_text()
    _close_window(window)
    print("[OK] 主窗口 P7：会话列表、设置/新会话按钮、用量标签都在，点会话能恢复成气泡")


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
    thread.quit()  # 别等 finished→quit 的排队投递：poll 循环一真就返回，
    thread.wait(3000)  # wait() 又不处理事件，quit 送不到就死锁 3 秒
    assert not thread.isRunning(), "worker 线程没停下来"
    _KEEP_ALIVE.append((worker, thread))
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


# ---------------- U3 测试：气泡 / 卡片 / Stop / 排队 / 重新生成 ----------

def _send_text(window, text):
    window.input_box.setPlainText(text)
    window.send()


def _wait_idle(window, timeout_ms=8000):
    return _pump_until(lambda: not window._busy, timeout_ms)


def test_gui_streaming_bubbles():
    """一轮流式回答：用户气泡 + Jarvis 气泡，文字逐 token 进气泡。"""
    _app()
    client = FakeStreamClient([
        [_chunk("你好"), _chunk("，我是 Jarvis。")],
    ])
    window = MainWindow(client)
    _send_text(window, "你好")
    assert window.send_button.text() == "■ 停止", "发送中按钮该变 Stop"
    assert _wait_idle(window), "一轮没结束"
    text = window.transcript_text()
    assert "你好" in text and "你好，我是 Jarvis。" in text, text
    assert window.status_label.text() == "就绪"
    assert window.send_button.text() == "发送"
    window.close()
    print("[OK] GUI：流式回答进气泡，发送中按钮变 Stop，结束恢复")


def test_gui_tool_card():
    """工具调用出卡片：标题有摘要，结算后有 ✓ 和耗时，点标题能折叠。"""
    _app()
    from jarvis.ui.gui import ToolCardWidget
    client = FakeStreamClient([
        [_chunk(tool_calls=[_tool_delta(0, "c1", "calculator",
                                        '{"expression": "12 * (3 + 4)"}')])],
        [_chunk("等于 84。")],
    ])
    window = MainWindow(client)
    _send_text(window, "12*(3+4) 等于多少？")
    assert _wait_idle(window), "一轮没结束"
    cards = window.findChildren(ToolCardWidget)
    assert len(cards) == 1, f"工具卡片该有 1 张：{len(cards)}"
    card = cards[0]
    assert "算" in card.header.text(), card.header.text()
    assert "✓" in card.header.text() and "ms" in card.header.text(), \
        card.header.text()
    assert card._collapsed is True and card.body.isHidden(), "卡片默认折叠"
    card.header.click()
    assert card._collapsed is False, "点标题该展开"
    assert not card.body.isHidden(), "点标题该展开"
    assert "84" in card.body.toPlainText(), card.body.toPlainText()
    window.close()
    print("[OK] GUI：工具卡片有摘要 + ✓ + 耗时，点标题展开看参数/输出")


def test_gui_thinking_widget():
    """思考流进独立的 disclosure，不和正文混在一起。"""
    _app()
    from jarvis.ui.gui import ThinkingWidget
    client = FakeStreamClient([
        [_chunk(reasoning="先想想…"), _chunk("答案。")],
    ])
    window = MainWindow(client)
    _send_text(window, "想想")
    assert _wait_idle(window), "一轮没结束"
    boxes = window.findChildren(ThinkingWidget)
    assert len(boxes) == 1, f"思考 disclosure 该有 1 个：{len(boxes)}"
    assert "先想想" in boxes[0].body.toPlainText()
    assert "答案。" in window.transcript_text()
    window.close()
    print("[OK] GUI：思考流进 disclosure，正文另起气泡")


class _SlowStreamClient:
    """每段隔 delay 秒才吐一个 chunk，模拟长回答（给 Stop/排队测试用）。"""

    def __init__(self, chunks, delay=0.05):
        import time as _time
        self._chunks = chunks
        self._delay = delay
        self._time = _time
        self.chat = SimpleNamespace(completions=SimpleNamespace(
            create=self._create))

    def _create(self, **kwargs):
        def gen():
            for chunk in self._chunks:
                self._time.sleep(self._delay)
                yield chunk
        return gen()


def test_gui_stop_keeps_text():
    """点 Stop：已出文字保留并标记“■ 已停止”，不是报错。"""
    _app()
    chunks = [_chunk(f"字{i} ") for i in range(30)]
    window = MainWindow(_SlowStreamClient(chunks, delay=0.05))
    _send_text(window, "说一段很长的话")
    ok = _pump_until(lambda: "字0" in window.transcript_text(), 5000)
    assert ok, "还没出字就停了，测不到保留"
    window._on_send_button()  # 点 Stop
    assert _wait_idle(window, 5000), "停止后没恢复空闲"
    text = window.transcript_text()
    assert "■ 已停止" in text, text
    assert "字0" in text, "已出的文字要保留"
    assert window.status_label.text() == "已停止"
    window.close()
    print("[OK] GUI：Stop 后已出文字保留并标记“■ 已停止”")


def test_gui_confirm_inline_card():
    """写操作确认长在聊天流里（不弹窗）：点“允许一次”才写。"""
    import tempfile
    from jarvis.tools import tools
    from jarvis.safety import audit as audit_mod
    from jarvis.safety.audit import AuditLog
    from jarvis.tools import shell_tools
    from jarvis.ui.gui import ConfirmCardWidget

    _app()
    tmp = tempfile.mkdtemp()
    old_proj = tools.PROJECT_DIR
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
    try:
        window = MainWindow(client)
        window._gate.mode = "edit"
        _send_text(window, "写个文件")
        ok = _pump_until(
            lambda: window.findChildren(ConfirmCardWidget), 5000)
        assert ok, "确认卡片没出现在聊天流里"
        card = window.findChildren(ConfirmCardWidget)[0]
        assert "hello.txt" in card.findChild(QLabel).text(), \
            card.findChild(QLabel).text()
        assert not os.path.exists(os.path.join(tmp, "hello.txt")), \
            "没点允许就写了"
        # 点“允许一次”
        for btn in card.findChildren(QPushButton):
            if btn.text() == "允许一次":
                btn.click()
                break
        assert _wait_idle(window), "一轮没结束"
        assert open(os.path.join(tmp, "hello.txt")).read() == "hi\n"
        assert "写好了" in window.transcript_text()
        window.close()
    finally:
        tools.PROJECT_DIR = old_proj
        shell_tools.PROJECT_DIR = old_shell_proj
        audit_mod._DEFAULT = old_audit
        tools._BEFORE_WRITE_HOOKS[:] = old_hooks
    print("[OK] GUI：确认卡片长在聊天流里，点“允许一次”才写文件")


def test_gui_queue_fifo():
    """忙时回车排队：队列可见，轮次结束按 FIFO 自动发。"""
    _app()
    chunks = [_chunk(f"字{i} ") for i in range(10)]
    window = MainWindow(_SlowStreamClient(chunks, delay=0.05))
    _send_text(window, "第一问")
    assert _pump_until(lambda: window._busy, 5000)
    _send_text(window, "第二问")
    assert window._queue == ["第二问"], window._queue
    assert not window.queue_label.isHidden(), "排队提示该出现"
    assert _pump_until(lambda: len(window._queue) == 0, 8000), "排队没发出去"
    assert _wait_idle(window, 8000), "第二轮没结束"
    text = window.transcript_text()
    assert "第一问" in text and "第二问" in text, text
    window.close()
    print("[OK] GUI：忙时回车排队成追问，轮次结束按 FIFO 自动发")


def test_gui_regenerate():
    """重新生成：旧回答气泡被拿掉，重发上一条用户消息，出新回答。"""
    _app()
    client = FakeStreamClient([
        [_chunk("第一版答案。")],
        [_chunk("第二版答案。")],
    ])
    window = MainWindow(client)
    _send_text(window, "讲个笑话")
    assert _wait_idle(window), "第一轮没结束"
    assert "第一版答案。" in window.transcript_text()
    from jarvis.ui.gui import MessageBubble
    bubbles = [w for w in window.findChildren(MessageBubble)
               if w.role == "jarvis"]
    assert bubbles, "没有 Jarvis 气泡"
    bubbles[-1].regen_btn.click()
    assert _wait_idle(window), "重新生成没结束"
    text = window.transcript_text()
    assert "第二版答案。" in text, text
    assert "第一版答案。" not in text, "旧回答气泡该被拿掉"
    window.close()
    print("[OK] GUI：重新生成拿掉旧回答，重发后出新回答")


def test_gui_code_block_copy():
    """代码块有语言标签 + 复制链接，点复制进剪贴板。"""
    _app()
    from PySide6.QtCore import QUrl
    from jarvis.ui.gui import MessageBubble, _html_with_code_headers
    from jarvis.ui.md import to_html
    html_text, codes = _html_with_code_headers(
        to_html("试试：\n```python\nprint(1)\n```"))
    assert "python" in html_text and "复制" in html_text, html_text
    assert codes == ["print(1)\n"], codes
    window = MainWindow(FakeStreamClient([[_chunk("hi")]]))
    bubble = MessageBubble("jarvis", window._copy_text)
    bubble.set_markdown("试试：\n```python\nprint(1)\n```")
    assert "python" in bubble.body.toHtml()
    bubble._on_anchor(QUrl("copy-code:0"))
    assert QApplication.clipboard().text() == "print(1)\n", \
        QApplication.clipboard().text()
    window.close()
    print("[OK] GUI：代码块有语言标签 + 复制链接，点一下进剪贴板")


def test_gui_markdown_render_only_when_needed():
    """收尾渲染只换有 Markdown 的回答：纯文本不换（不闪烁），
    含 **粗体** 的换渲染版。"""
    _app()
    client = FakeStreamClient([
        [_chunk("纯文本回答。")],
        [_chunk("带 **粗体** 的回答。")],
    ])
    window = MainWindow(client)
    _send_text(window, "第一问")
    assert _wait_idle(window), "第一轮没结束"
    from jarvis.ui.gui import MessageBubble
    bubbles = [w for w in window.findChildren(MessageBubble)
               if w.role == "jarvis"]
    assert "纯文本回答。" in bubbles[-1].plain_text()
    assert "<b>" not in bubbles[-1].body.toHtml(), \
        "纯文本不该换渲染版"
    _send_text(window, "第二问")
    assert _wait_idle(window), "第二轮没结束"
    bubbles = [w for w in window.findChildren(MessageBubble)
               if w.role == "jarvis"]
    # Qt 的 toHtml() 会把 <b> 写成 font-weight:700 的 span，断言这个
    # 才是“真的渲染了粗体”；字面 <b> 在序列化结果里永远不会出现。
    assert "font-weight:700" in bubbles[-1].body.toHtml(), \
        bubbles[-1].body.toHtml()
    window.close()
    print("[OK] GUI：纯文本收尾不换渲染版，含 Markdown 的才换")


def test_gui_settings_dialog_lists_light_theme():
    """U6-1：设置里主题下拉有 dark/light 两项（light.json 已补）。"""
    from jarvis.core.config import load_config
    from jarvis.ui.gui import SettingsDialog

    _app()
    dlg = SettingsDialog(load_config())
    items = [dlg.theme_box.itemText(i)
             for i in range(dlg.theme_box.count())]
    assert "dark" in items and "light" in items, items
    assert dlg.theme_box.isEnabled(), "只有一个主题时下拉是禁用的"
    dlg.close()
    print("[OK] GUI：设置里主题下拉有 dark/light 两项")


def test_gui_diff_grouped_and_bubble_restore():
    """U5-1/U5-2：Diff Tab 按文件分组；气泡悬停“恢复检查点”回到挂载前。"""
    import tempfile
    import jarvis.ui.gui as gui
    from jarvis.memory.checkpoint import CheckpointStore
    from jarvis.ui.gui import MessageBubble
    from test_diffutil import GIT_DIFF

    _app()
    window = MainWindow(FakeStreamClient([[_chunk("hi")]]))
    old = gui.working_tree_diff
    gui.working_tree_diff = lambda: GIT_DIFF
    try:
        window.tabs.setCurrentIndex(2)  # Diff
        _pump_until(lambda: "a.py" in window.diff_view.toPlainText(), 3000)
        assert "a.py（+1 行 / -1 行）" in window.diff_view.toPlainText(), \
            window.diff_view.toPlainText()
        assert "b.py（+2 行 / -0 行）" in window.diff_view.toPlainText()
        # 恢复检查点：气泡挂载后备份、再改坏，点恢复回到挂载前
        tmp = tempfile.mkdtemp()
        target = os.path.join(tmp, "a.txt")
        with open(target, "w", encoding="utf-8") as f:
            f.write("原版")
        store = CheckpointStore("test-sid", root=os.path.join(tmp, "ckpts"))
        window._checkpoints = store
        bubble = MessageBubble(
            "user", window._copy_text,
            on_restore=window._on_bubble_restore)
        assert bubble.restore_btn is not None
        assert bubble.restore_btn.text() == "恢复检查点"
        store.snapshot(target)
        with open(target, "w", encoding="utf-8") as f:
            f.write("改坏了")
        window._on_bubble_restore(bubble)
        assert open(target, encoding="utf-8").read() == "原版"
        assert "已恢复检查点" in window.transcript_text()
    finally:
        gui.working_tree_diff = old
    window.close()
    print("[OK] GUI：Diff Tab 按文件分组；气泡“恢复检查点”回到挂载前")


def test_gui_diff_and_checkpoint_tabs():
    """Diff Tab：非 git 目录直说看不到改动；检查点 Tab：点一条回滚文件。"""
    import tempfile
    from jarvis.memory.checkpoint import CheckpointStore

    _app()
    window = MainWindow(FakeStreamClient([[_chunk("hi")]]))
    window.tabs.setCurrentIndex(2)  # Diff（U4 起 Tab 顺序：会话/待办/Diff/检查点）
    _pump_until(lambda: "git 仓库" in window.diff_view.toPlainText(), 3000)
    assert "git 仓库" in window.diff_view.toPlainText(), \
        window.diff_view.toPlainText()
    # 造一个检查点：先写文件，再改掉它，点检查点恢复
    tmp = tempfile.mkdtemp()
    target = os.path.join(tmp, "a.txt")
    with open(target, "w", encoding="utf-8") as f:
        f.write("原版")
    store = CheckpointStore("test-sid", root=os.path.join(tmp, "ckpts"))
    store.snapshot(target)
    with open(target, "w", encoding="utf-8") as f:
        f.write("改坏了")
    window._checkpoints = store
    window._refresh_ckpts()
    assert window.ckpt_list.count() == 1, window.ckpt_list.count()
    window._on_ckpt_clicked(window.ckpt_list.item(0))
    assert open(target, encoding="utf-8").read() == "原版"
    assert "已恢复检查点" in window.transcript_text()
    assert window.ckpt_list.count() == 0, "恢复后清单该消费掉这一条"
    window.close()
    print("[OK] GUI：Diff Tab 非 git 目录直说；检查点 Tab 点一条回滚文件")


def test_gui_mode_chip_cycles():
    """档位芯片点一下换档，gate 跟着变。"""
    _app()
    from jarvis.ui import gui
    real_save = gui.save_config
    gui.save_config = lambda cfg: None  # 芯片每点一下都落盘，测试里别写真实配置
    try:
        window = MainWindow(FakeStreamClient([[_chunk("hi")]]))
        # 起始档位显式定死：配置是落盘的，上一轮跑完可能留在 readonly，
        # 不能假设默认 edit。
        window._config.permission = "edit"
        window._gate.mode = "edit"
        window.permission_label.setText("权限：edit")
        assert window._config.permission == "edit"
        window.permission_label.click()
        assert window._config.permission == "auto", window._config.permission
        assert window._gate.mode == "auto"
        assert window.permission_label.text() == "权限：auto"
        window.permission_label.click()
        assert window._config.permission == "readonly"
        window.close()
    finally:
        gui.save_config = real_save
    print("[OK] GUI：档位芯片点一下换档，gate 跟着变")


def test_gui_plan_approve_executes_and_ticks_todos():
    """U4-1/U4-2：计划开关开着发消息 → 计划卡片 → 点批准执行，待办 Tab 打勾。"""
    from test_loop import _text_reply
    from jarvis.ui.gui import PlanCardWidget

    class _PlanFakeClient:
        """混合 fake：make_plan 非流式要 choices 对象，执行轮流式要 chunks。"""

        def __init__(self, plan_text, stream_script):
            self._plan_text = plan_text
            self._script = list(stream_script)
            self.chat = SimpleNamespace(completions=SimpleNamespace(
                create=self._create))

        def _create(self, stream=False, **kwargs):
            if not stream:
                return _text_reply(self._plan_text)
            chunks = self._script.pop(0)

            def gen():
                yield from chunks
            return gen()

    _app()
    plan_text = ("目标：写个问候脚本\n步骤：\n"
                 "1. 写文件 —— 验收：文件存在\n"
                 "2. 跑一遍 —— 验收：输出问候\n风险：\n- 无\n")
    client = _PlanFakeClient(plan_text, [
        [_chunk(tool_calls=[_tool_delta(
            0, "c1", "update_todo", '{"index": 1}')])],
        [_chunk("做完了。")],
    ])
    window = MainWindow(client)
    window.plan_button.setChecked(True)
    _send_text(window, "写个问候脚本")
    ok = _pump_until(lambda: window.findChildren(PlanCardWidget), 5000)
    assert ok, "计划卡片没出现"
    card = window.findChildren(PlanCardWidget)[0]
    assert "写个问候脚本" in card.edit.toPlainText(), card.edit.toPlainText()
    assert window._todo.items == [], "批准前不该执行、不该有清单"
    for btn in card.findChildren(QPushButton):
        if btn.text() == "批准执行":
            btn.click()
            break
    else:
        raise AssertionError("卡片上没有“批准执行”按钮")
    ok = _pump_until(
        lambda: not window._busy and "做完了" in window.transcript_text(),
        8000)
    assert ok, "批准后执行没跑完"
    assert window.todo_list.count() == 2, window.todo_list.count()
    assert window.todo_list.item(0).text().startswith("☑ 写文件"), \
        window.todo_list.item(0).text()
    assert window.todo_list.item(1).text().startswith("◐ 跑一遍"), \
        window.todo_list.item(1).text()
    window.close()
    print("[OK] GUI：计划开关出卡片，批准后执行，待办 Tab 实时打勾")


def test_gui_plan_reject_does_not_execute():
    """U4-1：计划卡片点拒绝，不执行，待办保持空。"""
    from test_loop import _text_reply
    from jarvis.ui.gui import PlanCardWidget

    class _PlanFakeClient2:
        def __init__(self, plan_text):
            self._plan_text = plan_text
            self.chat = SimpleNamespace(completions=SimpleNamespace(
                create=self._create))

        def _create(self, stream=False, **kwargs):
            if not stream:
                return _text_reply(self._plan_text)
            raise AssertionError("拒绝后不该再调模型")

    _app()
    plan_text = ("目标：写个问候脚本\n步骤：\n"
                 "1. 写文件 —— 验收：文件存在\n风险：\n- 无\n")
    window = MainWindow(_PlanFakeClient2(plan_text))
    window.plan_button.setChecked(True)
    _send_text(window, "写个问候脚本")
    ok = _pump_until(lambda: window.findChildren(PlanCardWidget), 5000)
    assert ok, "计划卡片没出现"
    card = window.findChildren(PlanCardWidget)[0]
    for btn in card.findChildren(QPushButton):
        if btn.text() == "拒绝":
            btn.click()
            break
    else:
        raise AssertionError("卡片上没有“拒绝”按钮")
    ok = _pump_until(
        lambda: not window._busy and "已拒绝" in window.transcript_text(),
        5000)
    assert ok, "拒绝后没恢复空闲"
    assert window._todo.items == []
    assert "做完了" not in window.transcript_text()
    window.close()
    print("[OK] GUI：计划卡片点拒绝，不执行，待办保持空")


def test_gui_geometry_saved_on_close():
    """关窗口时尺寸写进 config（U3-5），不直接写真实配置文件。"""
    _app()
    from jarvis.ui import gui
    saved = {}
    real_save = gui.save_config
    gui.save_config = lambda cfg: saved.update(
        width=cfg.window_width, height=cfg.window_height,
        sizes=cfg.splitter_sizes)
    try:
        window = MainWindow(FakeStreamClient([[_chunk("hi")]]))
        window.resize(1234, 700)
        window.close()
    finally:
        gui.save_config = real_save
    assert saved.get("width") == 1234, saved
    assert saved.get("height") == 700, saved
    assert isinstance(saved.get("sizes"), list), saved
    print("[OK] GUI：关窗口时尺寸与分隔条位置写进 config")


def test_gui_no_key_shows_error_card():
    """U6-3：没 key 时窗口照常打开，发消息给“去填 key”卡片，不发请求。"""
    from unittest.mock import patch
    from jarvis.ui.gui import ErrorCardWidget, MainWindow

    app = _app()
    with patch.object(MainWindow, "_open_first_run_wizard",
                      lambda self: None):
        window = MainWindow(None)
    _send_text(window, "你好")
    assert window._worker is None
    assert not window._busy
    cards = [window.chat_layout.itemAt(i).widget()
             for i in range(window.chat_layout.count())]
    cards = [w for w in cards if isinstance(w, ErrorCardWidget)]
    assert cards, "应挂一张错误卡片"
    label = cards[0].findChild(QLabel)
    assert label is not None and "还没填 DeepSeek key" in label.text()
    labels = [b.text() for b in cards[0].findChildren(QPushButton)]
    assert "去填 key" in labels
    _close_window(window)
    print("[OK] GUI：没 key 时发消息给“去填 key”卡片，不发请求")


def _error_cards(window):
    from jarvis.ui.gui import ErrorCardWidget
    out = []
    for i in range(window.chat_layout.count()):
        w = window.chat_layout.itemAt(i).widget()
        if isinstance(w, ErrorCardWidget):
            out.append(w)
    return out


def test_gui_error_retry_once_then_copy_new_session():
    """U6-4：第一次出错给“重试”+ 429 提示；重试还失败，改给
    “复制问题，开新会话”，点了把问题填回输入框。"""
    from jarvis.ui.gui import MainWindow

    app = _app()
    window = MainWindow(object())
    window._last_user_text = "问题X"
    window._on_error("RateLimit 429 too many requests")
    first = _error_cards(window)
    assert len(first) == 1
    label = first[0].findChild(QLabel)
    assert label is not None and "请求太频繁" in label.text()
    assert "重试" in [b.text() for b in first[0].findChildren(QPushButton)]
    window._on_error("RateLimit 429 too many requests")
    assert window._error_streak == 2
    second = _error_cards(window)[-1]
    labels = [b.text() for b in second.findChildren(QPushButton)]
    assert "复制问题，开新会话" in labels
    assert "重试" not in labels
    btn = [b for b in second.findChildren(QPushButton)
           if b.text() == "复制问题，开新会话"][0]
    btn.click()
    assert window.input_box.toPlainText() == "问题X"
    assert "新会话。" in window.transcript_text()
    assert QApplication.clipboard().text() == "问题X"
    _close_window(window)
    print("[OK] GUI：出错重试一次，第二次改给“复制问题，开新会话”")


def test_gui_settings_dialog_key_field():
    """U6-3：设置对话框有 key 字段（密码框）；保存时写 .env，不碰真文件
    用 monkeypatch 拦住写盘，只验调用参数与 key_saved。"""
    from unittest.mock import patch
    import jarvis.core.config as config_mod
    from jarvis.core.config import load_config
    from jarvis.ui.gui import SettingsDialog

    app = _app()
    dialog = SettingsDialog(load_config(), first_run=True)
    assert dialog.windowTitle() == "欢迎使用 Jarvis"
    assert dialog.key_edit.echoMode() == QLineEdit.Password
    assert "sk-" in dialog.key_edit.placeholderText()
    dialog.key_edit.setText("sk-test-xyz")
    calls = []
    with patch.object(config_mod, "save_key_to_dotenv",
                      lambda k, p: calls.append((k, p))):
        dialog.apply()
    assert len(calls) == 1
    assert calls[0][0] == "sk-test-xyz"
    assert calls[0][1].endswith(".env")
    assert dialog.key_saved == "sk-test-xyz"
    assert os.environ.get("DEEPSEEK_API_KEY") == "sk-test-xyz"
    del os.environ["DEEPSEEK_API_KEY"]
    print("[OK] GUI：设置对话框有 key 字段，保存进 .env")


def test_gui_trim_transcript_caps_widgets():
    """U6-5：transcript 超 200 个组件就折叠最老的 50 条（_folded 计数），
    折叠提示在，组件数始终 <= 200；再挂 50 条会触发第二次折叠。"""
    from jarvis.ui.gui import MainWindow, NoteWidget

    app = _app()
    window = MainWindow(object())
    for i in range(210):
        window._add_transcript_widget(NoteWidget(f"第 {i} 条"))
    count = window.chat_layout.count() - 1  # 减掉末尾 stretch
    assert count <= 200, f"组件数应 <= 200，实际 {count}"
    assert "已折叠" in window.transcript_text()
    assert window._folded == 51  # 第一批：201 -> 150，折了 51 条
    for i in range(210, 260):
        window._add_transcript_widget(NoteWidget(f"第 {i} 条"))
    count = window.chat_layout.count() - 1
    assert count <= 200, f"组件数应 <= 200，实际 {count}"
    assert window._folded >= 100  # 第二次折叠已触发
    _close_window(window)
    print("[OK] GUI：长 transcript 按批折叠，组件数始终 <= 200")


def test_gui_shortcuts_dialog():
    """U6-2：帮助菜单有“快捷键…”；对话框里是本界面真实的快捷键表。"""
    from jarvis.ui.gui import MainWindow, ShortcutsDialog

    app = _app()
    window = MainWindow(object())
    menus = [a.text() for a in window.menuBar().actions()]
    assert "帮助" in menus, menus
    help_menu = [a for a in window.menuBar().actions()
                 if a.text() == "帮助"][0].menu()
    assert "快捷键…" in [a.text() for a in help_menu.actions()]
    assert "关于 Jarvis" in [a.text() for a in help_menu.actions()]
    dialog = ShortcutsDialog(window)
    label = dialog.findChild(QLabel)
    assert label is not None
    assert "Enter 发送" in label.text()
    assert "计划" in label.text() and "Diff" in label.text()
    assert "检查点" in label.text()
    _close_window(window)
    print("[OK] GUI：帮助菜单有快捷键总览，对话框是本界面真实的键表")


def _teardown_qt():
    """显式收尾：先放掉保活的 worker/thread 包装，再关 QApplication。

    顺序反了会在 Windows 上段错误（0xC0000005）：解释器退出时
    _KEEP_ALIVE 和 QApplication 按任意顺序释放，QThread 包装可能
    在 QApplication 析构后才被删。沙箱 Linux 上碰巧不崩，真机必崩。
    """
    import gc as _gc
    for _worker, _thread in list(_KEEP_ALIVE):
        try:
            _thread.quit()
            _thread.wait(3000)
        except Exception:  # noqa: BLE001 - 收尾不让异常盖住测试结果
            pass
    _KEEP_ALIVE.clear()
    _gc.collect()
    app = QApplication.instance()
    if app is not None:
        for _w in app.topLevelWidgets():
            _w.close()
        app.processEvents(QEventLoop.AllEvents, 100)
        app.quit()
        _gc.collect()


if __name__ == "__main__":
    test_worker_signals()
    test_main_window_builds()
    test_main_window_p7_widgets()
    test_settings_dialog()
    test_worker_confirm_flow()
    test_gui_streaming_bubbles()
    test_gui_tool_card()
    test_gui_thinking_widget()
    test_gui_stop_keeps_text()
    test_gui_confirm_inline_card()
    test_gui_queue_fifo()
    test_gui_regenerate()
    test_gui_code_block_copy()
    test_gui_markdown_render_only_when_needed()
    test_gui_diff_and_checkpoint_tabs()
    test_gui_diff_grouped_and_bubble_restore()
    test_gui_settings_dialog_lists_light_theme()
    test_gui_mode_chip_cycles()
    test_gui_plan_approve_executes_and_ticks_todos()
    test_gui_plan_reject_does_not_execute()
    test_gui_geometry_saved_on_close()
    test_gui_no_key_shows_error_card()
    test_gui_error_retry_once_then_copy_new_session()
    test_gui_settings_dialog_key_field()
    test_gui_trim_transcript_caps_widgets()
    test_gui_shortcuts_dialog()
    print("全部通过。")
    _teardown_qt()
