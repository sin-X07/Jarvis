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

import tools
from agent import run_agent
from checkpoint import CheckpointStore, set_current_store
from planner import Plan, make_plan, parse_plan, verify_plan
from test_loop import FakeClient, _text_reply, _tool_call
from todo import TodoList, todo_tools


def test_todo_list():
    todo = TodoList()
    todo.add("读文件", "定位到相关代码")
    todo.add("改代码")
    assert len(todo.pending()) == 2
    assert "已完成" in todo.mark_done(0)
    assert len(todo.pending()) == 1 and todo.pending()[0]["title"] == "改代码"
    assert "没有第 9 项" in todo.mark_done(8)
    assert "[x] 1. 读文件" in todo.render(), todo.render()
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
    import audit as audit_mod
    from audit import AuditLog
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
    assert len(todos.items) == 3 and todos.render().count("[ ]") == 3
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


if __name__ == "__main__":
    test_todo_list()
    test_todo_tools()
    test_checkpoint_rewind()
    test_checkpoint_hook_on_write()
    test_parse_plan()
    test_make_plan_with_fake_client()
    test_readonly_loop()
    test_verify_plan()
    print("全部通过。")
