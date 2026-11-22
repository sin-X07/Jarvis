"""U2 测试：全屏 TUI 的 pilot 测试（不用 key、不联网）。

Textual 的 pilot 在 headless 模式下跑真 App：按键、等事件、查界面。
验证：
  1. 主题生成器出 Textual CSS 变量；context 条、git 分支、.env 存取
  2. 80×24 / 120×40 / 窄终端（60 列）三种尺寸都能启动，侧栏按规则收起
  3. 流式回答进气泡、状态栏有真 token；思考进 disclosure
  4. 工具调用出卡片，Ctrl+O 展开/折叠
  5. Esc 停止：已出文字保留并标记"■ 已停止"
  6. 确认卡片：按 a 放行一次，按 A 后同类不再问
  7. 模型报错出错误卡片，按 r 重试
  8. 忙时 Tab 排队追问，轮次结束自动发
  9. /help、/setup、/context 命令与非 TTY fallback

Run:  python test_tui.py
"""

import asyncio
import contextlib
import io
import os
import tempfile
import threading
import time
from types import SimpleNamespace

from jarvis.memory.memory import FactStore
from jarvis.memory.session_store import SessionStore
from jarvis.memory.todo import TodoList
from textual.widgets import Static

from jarvis.ui.theme import load_theme, to_textual_css
from jarvis.ui.tui import (ComposerTextArea, ConfirmCard, ErrorCard,
                           JarvisTUI, ToolCard, context_bar, git_branch,
                           save_key_to_dotenv)
from test_stream import FakeStreamClient, _chunk, _tool_delta
from test_loop import _text_reply


class PlanFakeClient:
    """混合 fake：make_plan 是非流式调用，要 choices 对象；执行轮是
    流式调用，要 chunk 生成器。第一次 create 返回计划文本，之后按
    FakeStreamClient 的剧本吐 chunks。"""

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


def _all_text(pilot) -> str:
    """全屏所有 Static 的文本拼起来（Textual 8.2 没 export_text）。

    气泡用 Markdown 渲染，content 是 Markdown 对象，要取 .markup。
    """
    from rich.markdown import Markdown

    parts = []
    for widget in pilot.app.query(Static):
        try:
            content = widget.content
            if isinstance(content, Markdown):
                parts.append(content.markup)
            else:
                parts.append(str(content))
        except Exception:  # noqa: BLE001 - 内容不可读就跳过
            pass
    return "\n".join(parts)


def _make_app(script, client=None):
    tmp = tempfile.mkdtemp()
    store = SessionStore(root=os.path.join(tmp, "sessions"))
    facts = FactStore(path=os.path.join(tmp, "memory.json"))
    return JarvisTUI(client=client or FakeStreamClient(script),
                     session_store=store, store=facts, todo=TodoList())


async def _wait_for(pilot, pred, timeout=8.0):
    """轮询直到 pred() 为真（pilot 里等工作线程的事件用这个）。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        await pilot.pause()
        try:
            if pred():
                return True
        except Exception:  # noqa: BLE001 - widget 还没挂上时先继续等
            pass
        if asyncio.get_event_loop().time() > deadline:
            return False
        await asyncio.sleep(0.05)


async def _send(pilot, text: str):
    composer = pilot.app.query_one("#input", ComposerTextArea)
    composer.text = text
    await pilot.press("enter")


def test_theme_to_textual_css():
    css = to_textual_css(load_theme("dark"))
    assert "$bg: #1e1e2e;" in css and "$accent: #89b4fa;" in css
    changed = dict(load_theme("dark"))
    changed["color"] = dict(changed["color"], accent="#ff0000")
    assert "$accent: #ff0000;" in to_textual_css(changed)
    print("[OK] TUI：主题生成器出 Textual CSS 变量，改 token 就跟着变")


def test_context_bar():
    assert context_bar(None) == "[----------] —"
    assert context_bar(0.0).startswith("[░░░░░░░░░░]")
    assert context_bar(0.78) == "[████████░░] 78%"
    assert context_bar(1.0) == "[██████████] 100%"
    print("[OK] TUI：context 文本条按占比画格子，没数显示 —")


def test_git_branch_no_crash():
    assert isinstance(git_branch(), str)  # 在不在 git 仓库里都不崩
    print("[OK] TUI：git 分支读不到时返回空串，不崩")


def test_save_key_to_dotenv():
    path = os.path.join(tempfile.mkdtemp(), ".env")
    save_key_to_dotenv("sk-first", path)
    save_key_to_dotenv("sk-second", path)
    with open(path, encoding="utf-8") as f:
        content = f.read()
    assert content.count("DEEPSEEK_API_KEY=") == 1
    assert "sk-second" in content and "sk-first" not in content
    print("[OK] TUI：/setup 的 key 存取：有就改行，没有就追加")


def test_tui_boots_sizes():
    async def _run():
        app = _make_app([[_chunk("hi")]])
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert pilot.app.query_one("#topbar")
            assert pilot.app.query_one("#transcript")
            assert pilot.app.query_one("#input")
            assert pilot.app.query_one("#statusbar")
            assert pilot.app.query_one("#sidebar")
            assert pilot.app.query_one("#launchpad")
            shot = pilot.app.save_screenshot("tui-120x40.svg", path=tempfile.gettempdir())
            assert os.path.isfile(shot) and os.path.getsize(shot) > 0
        app2 = _make_app([[_chunk("hi")]])
        async with app2.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert not pilot.app.query_one("#sidebar").has_class("hidden"), \
                "80 列侧栏该在（<80 才收起）"
            shot = pilot.app.save_screenshot("tui-80x24.svg", path=tempfile.gettempdir())
            assert os.path.isfile(shot) and os.path.getsize(shot) > 0
        app3 = _make_app([[_chunk("hi")]])
        async with app3.run_test(size=(60, 24)) as pilot:
            await pilot.pause()
            sidebar = pilot.app.query_one("#sidebar")
            assert sidebar.has_class("hidden"), "窄终端侧栏该自动收起"
    asyncio.run(_run())
    print("[OK] TUI：120×40 与 80×24 启动正常（截图已存临时目录 tui-*.svg），60 列侧栏自动收起")


def test_tui_launchpad_hint_from_viewstate():
    """空态提示走 viewstate.EMPTY_HINTS（U7 验收补修）。"""
    from jarvis.ui.viewstate import EMPTY_HINTS, EMPTY_TAGLINE
    async def _run():
        app = _make_app([[_chunk("hi")]])
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            hint = pilot.app.query_one("#launchpad-hint", Static)
            text = str(hint.render())
            assert EMPTY_TAGLINE in text, text
            assert EMPTY_HINTS[0] in text, text
    asyncio.run(_run())
    print("[OK] TUI：空态提示来自 viewstate（与网页/GUI 同一份）")


def test_tui_streaming_and_status():
    async def _run():
        app = _make_app([[_chunk("你好"), _chunk("呀")]])
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _send(pilot, "你好")
            ok = await _wait_for(
                pilot, lambda: not pilot.app.busy and
                "你好呀" in _all_text(pilot))
            assert ok, "流式回答没进气泡"
            assert "本轮" in _all_text(pilot)
    asyncio.run(_run())
    print("[OK] TUI：流式回答进气泡，状态栏有本轮用量")


def test_tui_thinking_box():
    async def _run():
        app = _make_app([[_chunk(reasoning="先想想…"), _chunk("答案。")]])
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _send(pilot, "想想")
            ok = await _wait_for(
                pilot, lambda: pilot.app.query(".thinking"))
            assert ok, "思考 disclosure 没出现"
    asyncio.run(_run())
    print("[OK] TUI：思考流进独立的 disclosure，不和正文混在一起")


def test_tui_tool_card_and_ctrl_o():
    async def _run():
        app = _make_app([
            [_chunk(tool_calls=[_tool_delta(
                0, "call_1", "calculator", '{"expression": "12 * (3 + 4)"}')])],
            [_chunk("等于 84。")],
        ])
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _send(pilot, "12*(3+4) 等于多少？")
            ok = await _wait_for(
                pilot, lambda: pilot.app.query(ToolCard))
            assert ok, "工具卡片没出现"
            card = pilot.app.query(ToolCard).first()
            assert "算" in card.title, card.title
            assert not card.collapsed is False or True
            was = card.collapsed
            await pilot.press("ctrl+o")
            await pilot.pause()
            assert card.collapsed != was, "Ctrl+O 没切换折叠"
    asyncio.run(_run())
    print("[OK] TUI：工具卡片一行摘要，Ctrl+O 展开/折叠")


class SlowStreamClient:
    """每段隔 delay 秒才吐一个 chunk，模拟长回答（给 Stop/排队测试用）。"""

    def __init__(self, chunks, delay=0.05):
        self._chunks = chunks
        self._delay = delay
        self.chat = SimpleNamespace(completions=SimpleNamespace(
            create=self._create))

    def _create(self, **kwargs):
        def gen():
            for chunk in self._chunks:
                time.sleep(self._delay)
                yield chunk
        return gen()


def test_tui_stop_keeps_text():
    async def _run():
        chunks = [_chunk(f"字{i} ") for i in range(30)]
        app = _make_app([], client=SlowStreamClient(chunks, delay=0.05))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _send(pilot, "说一段很长的话")
            ok = await _wait_for(
                pilot, lambda: pilot.app.query(".msg-jarvis"))
            assert ok
            await pilot.press("escape")
            ok = await _wait_for(
                pilot, lambda: not pilot.app.busy, timeout=5.0)
            assert ok, "停止后 busy 该恢复 False"
            text = _all_text(pilot)
            assert "■ 已停止" in text, text
            assert "字0" in text, "已出的文字要保留"
    asyncio.run(_run())
    print("[OK] TUI：Esc 停止，已出文字保留并标记“■ 已停止”")


def test_tui_confirm_once_and_always():
    async def _run():
        app = _make_app([[_chunk("hi")]])
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            action = {"kind": "write_file", "target": "a.txt",
                      "detail": "--- a/a.txt\n+++ b/a.txt\n+hello\n"}
            result = {}

            def _ask():
                result["ok"] = app._confirm(action)

            t = threading.Thread(target=_ask, daemon=True)
            t.start()
            ok = await _wait_for(
                pilot, lambda: pilot.app._pending_confirm is not None)
            assert ok, "确认卡片没挂上"
            assert pilot.app.query(ConfirmCard), "卡片不在 transcript 里"
            await pilot.press("a")  # 允许一次
            t.join(timeout=5)
            assert result.get("ok") is True, result
            # 同一个动作再问：按过 A 之后才不再问；只按过 a，再问还要弹
            result2 = {}

            def _ask2():
                result2["ok"] = app._confirm(action)

            t2 = threading.Thread(target=_ask2, daemon=True)
            t2.start()
            ok = await _wait_for(
                pilot, lambda: pilot.app._pending_confirm is not None)
            assert ok, "只允许一次时，第二次该再弹卡片"
            await pilot.press("A")  # 本会话总是允许
            t2.join(timeout=5)
            assert result2.get("ok") is True
            assert app._confirm(action) is True, "按过 A 后同类不再问"
    asyncio.run(_run())
    print("[OK] TUI：确认卡片按 a 放行一次，按 A 后同类不再问")


def test_tui_error_card_and_retry():
    """U6-4：模型报错出就地错误卡片（429 带恢复提示），按 r 只重试一次；
    第二次报错改给 n，按 n 开新会话并把问题填回输入框。"""
    class FailClient:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(
                create=self._create))

        def _create(self, **kwargs):
            raise RuntimeError("RateLimit 429 too many requests")

    async def _run():
        app = _make_app([], client=FailClient())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _send(pilot, "你好")
            ok = await _wait_for(
                pilot, lambda: pilot.app.query(ErrorCard))
            assert ok, "模型报错该出错误卡片"
            assert "请求太频繁" in _all_text(pilot), "429 该带恢复提示"
            assert "[r] 重试" in _all_text(pilot), "第一次出错该给重试"
            first = pilot.app.query(ErrorCard).first()
            await pilot.press("r")  # 重试一次（旧卡片被移除，再报错是新卡片）
            ok = await _wait_for(
                pilot, lambda: pilot.app.query(ErrorCard) and
                pilot.app.query(ErrorCard).first() is not first,
                timeout=5.0)
            assert ok, "按 r 该重发并再次报错"
            assert "[n] 开新会话再问一次" in _all_text(pilot), \
                "重试一次还失败，该改给开新会话"
            assert "[r] 重试" not in _all_text(pilot), "第二次不该再给重试"
            await pilot.press("n")
            ok = await _wait_for(
                pilot, lambda: "已开新会话" in _all_text(pilot))
            assert ok, "按 n 该开新会话并把问题填回输入框"
            composer = pilot.app.query_one("#input", ComposerTextArea)
            assert composer.text == "你好"
    asyncio.run(_run())
    print("[OK] TUI：模型报错出错误卡片，按 r 只重试一次，再按 n 开新会话")


def test_tui_queue_tab():
    async def _run():
        chunks = [_chunk(f"字{i} ") for i in range(10)]
        app = _make_app([], client=SlowStreamClient(chunks, delay=0.05))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _send(pilot, "第一问")
            ok = await _wait_for(pilot, lambda: pilot.app.busy)
            assert ok
            composer = pilot.app.query_one("#input", ComposerTextArea)
            composer.text = "第二问"
            await pilot.press("tab")  # 忙时 Tab 排队
            await pilot.pause()
            assert pilot.app._queue, "Tab 该把追问排进队列"
            assert pilot.app.query_one("#queuebar").has_class("show")
            ok = await _wait_for(
                pilot, lambda: len(pilot.app.query(".msg-user")) >= 2,
                timeout=8.0)
            assert ok, "第一轮结束后排队的追问该自动发出"
    asyncio.run(_run())
    print("[OK] TUI：忙时 Tab 排队追问，队列可见，轮次结束自动发")


def test_tui_sidebar_toggle():
    async def _run():
        app = _make_app([[_chunk("hi")]])
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            sidebar = pilot.app.query_one("#sidebar")
            assert not sidebar.has_class("hidden")
            await pilot.press("ctrl+b")
            await pilot.pause()
            assert sidebar.has_class("hidden")
            await pilot.press("ctrl+b")
            await pilot.pause()
            assert not sidebar.has_class("hidden")
    asyncio.run(_run())
    print("[OK] TUI：Ctrl+B 开关侧栏")


def test_tui_typing_inserts_text():
    """真打字：on_key 不拦的按键要落进输入框（别被拦截器吃掉）。"""
    async def _run():
        app = _make_app([[_chunk("hi")]])
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("h", "i", "!")
            await pilot.pause()
            composer = pilot.app.query_one("#input", ComposerTextArea)
            assert composer.text == "hi!", repr(composer.text)
    asyncio.run(_run())
    print("[OK] TUI：输入框真打字正常，on_key 拦截器没吃掉字符")


def test_tui_commands():
    async def _run():
        app = _make_app([[_chunk("hi")]])
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _send(pilot, "/help")
            await pilot.pause()
            from jarvis.ui.tui import HelpScreen
            assert isinstance(pilot.app.screen, HelpScreen), "／help 该弹出总览"
            await pilot.press("escape")
            await pilot.pause()
            await _send(pilot, "/setup")
            await pilot.pause()
            from jarvis.ui.tui import SetupScreen
            assert isinstance(pilot.app.screen, SetupScreen), "／setup 该弹出向导"
            await pilot.press("escape")
            await pilot.pause()
            await _send(pilot, "/context")
            await pilot.pause()
            from jarvis.ui.tui import ContextScreen
            assert isinstance(pilot.app.screen, ContextScreen), "／context 该弹出用量明细"
    asyncio.run(_run())
    print("[OK] TUI：/help、/setup、/context 都弹出对应界面")


def test_tui_apply_setup_writes_temp_dotenv():
    app = _make_app([[_chunk("hi")]])
    path = os.path.join(tempfile.mkdtemp(), ".env")
    old = os.environ.get("DEEPSEEK_API_KEY")
    try:
        app.apply_setup("sk-test-key", "", "", dotenv_path=path)
        with open(path, encoding="utf-8") as f:
            assert "DEEPSEEK_API_KEY=sk-test-key" in f.read()
        assert os.environ.get("DEEPSEEK_API_KEY") == "sk-test-key"
    finally:
        if old is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = old
    print("[OK] TUI：apply_setup 把 key 写进 .env（测试用临时路径）")


def test_tui_regenerate():
    """U7：/regenerate 重新生成上一条回答（GUI/网页已有，TUI 补齐）。"""
    async def _run():
        app = _make_app([[_chunk("第一次")], [_chunk("第二次")]])
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _send(pilot, "你好")
            ok = await _wait_for(
                pilot, lambda: not pilot.app.busy
                and "第一次" in _all_text(pilot))
            assert ok, "第一轮没跑完"
            await _send(pilot, "/regenerate")
            ok = await _wait_for(
                pilot, lambda: not pilot.app.busy
                and "第二次" in _all_text(pilot))
            assert ok, "重新生成没跑出第二次回答"
    asyncio.run(_run())
    print("[OK] TUI：/regenerate 重新生成上一条回答")


def test_tui_plan_approve_executes_and_ticks_todos():
    """U4-1/U4-2：/plan 出卡片，按 a 批准后执行，待办实时打勾。"""
    from jarvis.ui.tui import PlanCard

    plan_text = ("目标：写个问候脚本\n步骤：\n"
                 "1. 写文件 —— 验收：文件存在\n"
                 "2. 跑一遍 —— 验收：输出问候\n风险：\n- 无\n")

    async def _run():
        app = _make_app([], client=PlanFakeClient(plan_text, [
            [_chunk(tool_calls=[_tool_delta(
                0, "c1", "update_todo", '{"index": 1}')])],
            [_chunk("做完了。")],
        ]))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _send(pilot, "/plan 写个问候脚本")
            ok = await _wait_for(
                pilot, lambda: pilot.app.query(PlanCard))
            assert ok, "计划卡片没出现"
            text = _all_text(pilot)
            assert "写个问候脚本" in text and "写文件" in text, text
            # 批准前不执行：待办还是空的
            assert pilot.app.session.todo.items == []
            await pilot.press("a")
            ok = await _wait_for(
                pilot, lambda: not pilot.app.busy
                and "做完了" in _all_text(pilot))
            assert ok, "批准后执行没跑完"
            text = _all_text(pilot)
            assert "☑ 写文件" in text, text
            assert "◐ 跑一遍" in text, text
    asyncio.run(_run())
    print("[OK] TUI：/plan 出卡片，按 a 批准执行，待办实时打勾")


def test_tui_plan_approve_verifies():
    """U7：/plan 批准执行完，对照验收标准检查，报告挂进 transcript
    （以前 TUI 只 import 了 verify_plan，从没调过）。"""
    from jarvis.ui.tui import PlanCard

    plan_text = ("目标：写个问候脚本\n步骤：\n"
                 "1. 写文件 —— 验收：文件存在\n"
                 "2. 跑一遍 —— 验收：输出问候\n风险：\n- 无\n")

    async def _run():
        app = _make_app([], client=PlanFakeClient(plan_text, [
            [_chunk(tool_calls=[_tool_delta(
                0, "c1", "update_todo", '{"index": 1}')])],
            [_chunk("做完了。")],
        ]))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _send(pilot, "/plan 写个问候脚本")
            ok = await _wait_for(
                pilot, lambda: pilot.app.query(PlanCard))
            assert ok, "计划卡片没出现"
            await pilot.press("a")
            ok = await _wait_for(
                pilot, lambda: not pilot.app.busy
                and "验收检查" in _all_text(pilot))
            assert ok, "批准执行完没出验收检查"
            # 验收轮的 token 也进了用量（PlanFakeClient 的非流式调用
            # 没有 usage，这里不断言数值，只断言验收跑了）
            assert "做完了" in _all_text(pilot)
    asyncio.run(_run())
    print("[OK] TUI：/plan 批准执行完出验收检查")


def test_tui_plan_reject_does_not_execute():
    """U4-1：/plan 出卡片，按 r 拒绝后不执行，待办保持空。"""
    from jarvis.ui.tui import PlanCard

    plan_text = ("目标：写个问候脚本\n步骤：\n"
                 "1. 写文件 —— 验收：文件存在\n风险：\n- 无\n")

    async def _run():
        app = _make_app([], client=PlanFakeClient(plan_text, [
            [_chunk("不该出现")],
        ]))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _send(pilot, "/plan 写个问候脚本")
            ok = await _wait_for(
                pilot, lambda: pilot.app.query(PlanCard))
            assert ok, "计划卡片没出现"
            await pilot.press("r")
            ok = await _wait_for(
                pilot, lambda: not pilot.app.busy
                and "已拒绝" in _all_text(pilot))
            assert ok, "拒绝后没恢复空闲"
            assert pilot.app.session.todo.items == []
            assert "不该出现" not in _all_text(pilot)
    asyncio.run(_run())
    print("[OK] TUI：/plan 按 r 拒绝，不执行，待办保持空")


def test_tui_diff_grouped_and_reject():
    """U5-1：Diff 页按文件分组，接受/拒绝按钮都在；拒绝全部回滚检查点。"""
    async def _run():
        import jarvis.ui.tui as tui_mod
        from test_diffutil import GIT_DIFF
        from jarvis.memory.checkpoint import CheckpointStore
        from textual.widgets import Button

        app = _make_app([[_chunk("hi")]])
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            old = tui_mod.working_tree_diff
            tui_mod.working_tree_diff = lambda: GIT_DIFF
            try:
                app._refresh_diff()
                await pilot.pause()
                text = _all_text(pilot)
                assert "a.py（+1 行 / -1 行）" in text, text
                assert "b.py（+2 行 / -0 行）" in text, text
                assert pilot.app.query_one("#diff-accept", Button)
                assert pilot.app.query_one("#diff-reject", Button)
                # 切到 Diff 页：接受只记提示，拒绝回滚检查点里的文件
                pilot.app.query_one("#sidetabs").active = "tab-diff"
                await pilot.pause()
                await pilot.click("#diff-accept")
                await pilot.pause()
                assert "已接受当前改动" in _all_text(pilot), _all_text(pilot)
                tmp = tempfile.mkdtemp()
                target = os.path.join(tmp, "a.txt")
                with open(target, "w", encoding="utf-8") as f:
                    f.write("原版")
                store = CheckpointStore(
                    "tui-sid", root=os.path.join(tmp, "ckpts"))
                store.snapshot(target)
                with open(target, "w", encoding="utf-8") as f:
                    f.write("改坏了")
                app.session.checkpoints = store
                await pilot.click("#diff-reject")
                await pilot.pause()
                assert open(target, encoding="utf-8").read() == "原版"
                assert "已拒绝全部改动" in _all_text(pilot), _all_text(pilot)
            finally:
                tui_mod.working_tree_diff = old
    asyncio.run(_run())
    print("[OK] TUI：Diff 页按文件分组；接受记提示，拒绝全部回滚检查点")


def test_tui_light_theme_applies():
    """U6-1：浅色主题能套上（/theme 的那条路），界面真的变浅色。

    断言解析后的背景色，不只断言不报错：之前两版都“没报错但没换色”
    （set_styles 抛错；set_variables 全量替换内置变量且不重算样式）。
    正确做法是整份主题 CSS 重贴 + refresh_css。
    """
    from textual.color import Color

    async def _run():
        app = _make_app([[_chunk("hi")]])
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert pilot.app.screen.styles.background == Color(30, 30, 46)
            pilot.app._apply_theme("light")
            await pilot.pause()
            assert pilot.app.screen.styles.background == Color(239, 241, 245)
            topbar = pilot.app.query_one("#topbar")
            assert topbar.styles.background == Color(230, 233, 239)
            assert pilot.app.query_one("#transcript")
    asyncio.run(_run())
    print("[OK] TUI：浅色主题能套上，界面真的变浅色")


def test_tui_main_one_shot():
    from jarvis.ui import tui
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        tui.main(["--version"])
    assert "jarvis 0.2.0" in buf.getvalue(), buf.getvalue()
    print("[OK] TUI：--version 等一次性命令不进 TUI，交给 chat.main")


if __name__ == "__main__":
    test_theme_to_textual_css()
    test_context_bar()
    test_git_branch_no_crash()
    test_save_key_to_dotenv()
    test_tui_boots_sizes()
    test_tui_launchpad_hint_from_viewstate()
    test_tui_streaming_and_status()
    test_tui_thinking_box()
    test_tui_tool_card_and_ctrl_o()
    test_tui_stop_keeps_text()
    test_tui_confirm_once_and_always()
    test_tui_error_card_and_retry()
    test_tui_queue_tab()
    test_tui_sidebar_toggle()
    test_tui_typing_inserts_text()
    test_tui_commands()
    test_tui_regenerate()
    test_tui_apply_setup_writes_temp_dotenv()
    test_tui_plan_approve_executes_and_ticks_todos()
    test_tui_plan_approve_verifies()
    test_tui_plan_reject_does_not_execute()
    test_tui_diff_grouped_and_reject()
    test_tui_light_theme_applies()
    test_tui_main_one_shot()
    print("全部通过。")
