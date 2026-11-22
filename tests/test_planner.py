"""P4 测试：任务清单、检查点、规划模式（不用 key、不联网）。

验证：
  1. TodoList：增/勾/渲染/存取往返
  2. 检查点：备份、回滚恢复、多次备份按顺序回滚、超量清理
  3. 写前钩子：装上检查点后，write_text_file 自动备份
  4. parse_plan：固定格式解析出目标/步骤/验收/风险
  5. make_plan：假 client 输出计划文本，解析成 Plan + TodoList
  6. 只读循环：规划模式里调写工具被拒，读工具正常
  7. verify_plan：假 client 返回检查报告
"""

import os
import tempfile

from jarvis.tools import tools
from jarvis.core.agent import run_agent
from jarvis.memory.checkpoint import CheckpointStore, set_current_store
from jarvis.memory.planner import Plan, make_plan, parse_plan, verify_plan
from test_loop import FakeClient, _text_reply, _tool_call
from jarvis.memory.todo import TodoList, todo_tools


def test_todo_list():
    todo = TodoList()
    todo.add("读文件", "定位到相关代码")
    todo.add("改代码")
    assert len(todo.pending()) == 2
    assert "已完成" in todo.mark_done(0)
    assert len(todo.pending()) == 1 and todo.pending()[0]["title"] == "改代码"
    assert "没有第 9 项" in todo.mark_done(8)
    assert "☑ 1. 读文件" in todo.render(), todo.render()
    assert "◐ 2. 改代码" in todo.render(), todo.render()
    path = os.path.join(tempfile.mkdtemp(), "todos.json")
    todo.save(path)
    loaded = TodoList.load(path)
    assert loaded.render() == todo.render()
    assert not todo.is_all_done()
    todo.mark_done(1)
    assert todo.is_all_done()
    print("[OK] 任务清单：增/勾/渲染/存取往返正常")


def test_todo_tools():
    todo = TodoList()
    todo.add("第一步")
    todo.add("第二步")
    schemas, funcs = todo_tools(todo)
    assert schemas[0]["function"]["name"] == "update_todo"
    assert "已完成" in funcs["update_todo"](title="第一步")
    assert "已完成" in funcs["update_todo"](index=2)
    assert todo.is_all_done()
    print("[OK] 任务清单：update_todo 工具能勾掉步骤")


def test_checkpoint_rewind():
    tmp = tempfile.mkdtemp()
    target = os.path.join(tmp, "a.txt")
    with open(target, "w") as f:
        f.write("v1\n")
    store = CheckpointStore("sess-1", root=os.path.join(tmp, "ckpt"))
    assert store.snapshot(target) is not None
    with open(target, "w") as f:
        f.write("v2\n")
    assert store.snapshot(target) is not None
    with open(target, "w") as f:
        f.write("v3\n")
    restored = store.rewind(1)
    assert restored == [os.path.abspath(target)], restored
    assert open(target).read() == "v2\n"
    restored = store.rewind(1)
    assert open(target).read() == "v1\n"
    assert store.list_checkpoints() == []
    print("[OK] 检查点：备份、回滚恢复、多次按顺序回滚")


def test_checkpoint_hook_on_write():
    tmp = tempfile.mkdtemp()
    old_proj = tools.PROJECT_DIR
    tools.PROJECT_DIR = tmp
    audit_tmp = tempfile.mkdtemp()
    from jarvis.safety import audit as audit_mod
    from jarvis.safety.audit import AuditLog
    old_audit = audit_mod._DEFAULT
    audit_mod._DEFAULT = AuditLog(os.path.join(audit_tmp, "audit.jsonl"))
    store = CheckpointStore("sess-2", root=os.path.join(tmp, "ckpt"))
    old_hooks = list(tools._BEFORE_WRITE_HOOKS)
    tools.register_before_write_hook(store.snapshot_hook)
    try:
        tools.write_text_file("b.txt", "第一版\n")
        tools.write_text_file("b.txt", "第二版\n")
        assert len(store.list_checkpoints()) == 1, store.list_checkpoints()
        tools.write_text_file("b.txt", "第三版\n")
        store.rewind(1)
        assert open(os.path.join(tmp, "b.txt"), encoding="utf-8").read() == "第二版\n"
    finally:
        tools._BEFORE_WRITE_HOOKS[:] = old_hooks
        audit_mod._DEFAULT = old_audit
        tools.PROJECT_DIR = old_proj
    print("[OK] 检查点：写文件自动备份，rewind 能恢复")


def test_parse_plan():
    text = ("目标：给 Jarvis 加一个新工具\n"
            "步骤：\n"
            "1. 读 tools.py —— 验收：说出加工具的三步\n"
            "2. 写工具和测试 —— 验收：测试通过\n"
            "3. 跑全量测试 —— 验收：全部通过\n"
            "风险：\n"
            "- 工具名冲突\n")
    plan = parse_plan(text)
    assert plan.goal == "给 Jarvis 加一个新工具", plan.goal
    assert len(plan.steps) == 3, plan.steps
    assert plan.steps[0]["acceptance"] == "说出加工具的三步", plan.steps
    assert plan.risks == ["工具名冲突"], plan.risks
    todos = plan.to_todos()
    assert len(todos.items) == 3 and todos.render().count("☐") == 2 \
        and todos.render().count("◐") == 1, todos.render()
    print("[OK] 规划：固定格式解析出目标/步骤/验收/风险")


def test_make_plan_with_fake_client():
    plan_text = ("目标：写一个计算器工具\n"
                 "步骤：\n"
                 "1. 读 tools.py —— 验收：说出注册位置\n"
                 "2. 写测试 —— 验收：测试通过\n"
                 "风险：\n"
                 "- 无\n")
    client = FakeClient([_text_reply(plan_text)])
    plan = make_plan(client, "deepseek-flash", "写一个计算器工具")
    assert plan.goal == "写一个计算器工具", plan
    assert len(plan.steps) == 2, plan.steps
    print("[OK] 规划：假 client 输出计划文本，解析成 Plan")


def test_readonly_loop():
    # 模型硬要调写工具：被拒；读工具：正常
    client = FakeClient([
        _tool_call("c1", "write_text_file",
                   '{"path": "x.txt", "content": "hi"}'),
        _text_reply("规划模式不能写，我先调研。"),
    ])
    answer = run_agent(client, "写个文件", verbose=False, read_only=True)
    assert "规划模式" in answer or "不能写" in answer, answer
    client2 = FakeClient([
        _tool_call("c1", "get_current_time", '{}'),
        _text_reply("现在是 2026 年。"),
    ])
    answer2 = run_agent(client2, "现在几点", verbose=False, read_only=True)
    assert "2026" in answer2, answer2
    print("[OK] 规划：只读循环里写工具被拒，读工具正常")


def test_verify_plan():
    plan = Plan(goal="测试", steps=[
        {"title": "写测试", "acceptance": "测试通过", "done": False}])
    client = FakeClient([_text_reply("1. 写测试：通过")])
    report = verify_plan(client, "deepseek-flash", plan, "测试都过了")
    assert "通过" in report, report
    print("[OK] 规划：verify_plan 返回逐项检查报告")


def test_todo_event_and_on_change():
    """U4-2：to_event 三态（pending/active/done，同时只有一个进行中），
    每次变动调 on_change。"""
    from jarvis.memory.todo import attach_todo_tools
    seen = []
    todo = TodoList(on_change=lambda: seen.append(todo.to_event()))
    todo.add("第一步")
    todo.add("第二步")
    todo.add("第三步")
    assert [e["status"] for e in todo.to_event()] == [
        "active", "pending", "pending"], todo.to_event()
    assert len(seen) == 3, seen  # 每次 add 都通知一次
    todo.mark_done(0)
    assert [e["status"] for e in todo.to_event()] == [
        "done", "active", "pending"], todo.to_event()
    todo.mark_done_by_title("第二步")
    assert [e["status"] for e in todo.to_event()] == [
        "done", "done", "active"], todo.to_event()
    todo.mark_done(2)
    assert [e["status"] for e in todo.to_event()] == [
        "done", "done", "done"], todo.to_event()
    assert todo.active_index() is None
    # 换清单后 update_todo 指向新清单（旧闭包不再勾旧表）
    tools2, funcs2 = [], {}
    old = TodoList()
    old.add("旧第一步")
    old.add("旧第二步")
    attach_todo_tools(old, tools2, funcs2)
    assert tools2[0]["function"]["name"] == "update_todo"
    todo2 = TodoList()
    todo2.add("新第一步")
    todo2.add("新第二步")
    attach_todo_tools(todo2, tools2, funcs2)
    assert len(tools2) == 1, tools2  # 旧的 update_todo 没留两份
    assert "已完成" in funcs2["update_todo"](title="新第一步")
    assert len(todo2.pending()) == 1
    assert len(old.pending()) == 2, "新调用不该动旧表"
    print("[OK] U4-2：待办三态 + on_change + 换清单重挂 update_todo")


if __name__ == "__main__":
    test_todo_list()
    test_todo_tools()
    test_checkpoint_rewind()
    test_checkpoint_hook_on_write()
    test_parse_plan()
    test_make_plan_with_fake_client()
    test_readonly_loop()
    test_verify_plan()
    test_todo_event_and_on_change()
    print("全部通过。")
