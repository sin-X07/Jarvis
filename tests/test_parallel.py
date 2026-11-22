"""P5 测试：并行调用、子代理、后台任务、预算上限（不用 key、不联网）。

验证：
  1. 并行：两个只读工具同时跑（用 Barrier 证明真的并行了），结果按原顺序拼回去
  2. 写工具不并行：两个写文件调用串行执行，确认各弹一次
  3. 子代理：delegate 派出去，子代理用自己的上下文做完回结果
  4. 后台任务：启动、查状态、取结果、超时杀掉、危险命令拒绝
  5. 预算：费用超上限就停下并报数
"""

import os
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

from jarvis.tools import tools
from jarvis.core.agent import run_agent
from jarvis.runtime.jobs import JobManager, jobs_tools
from jarvis.runtime.subagent import run_subagent, subagent_tools
from test_loop import FakeClient, _text_reply, _tool_call
from jarvis.core.usage import UsageTracker


def _schema(name):
    return {"type": "function", "function": {
        "name": name, "description": name,
        "parameters": {"type": "object", "properties": {}}}}


def test_parallel_read_tools():
    barrier = threading.Barrier(2)
    seen = []

    def slow_a(expression=""):
        seen.append("a")
        barrier.wait(timeout=5)
        return "A 结果"

    def slow_b():
        seen.append("b")
        barrier.wait(timeout=5)
        return "B 结果"

    # 模型一次要调两个工具：两个都走到 Barrier 才放行——串行跑的话
    # 第一个会超时，只两个并行才都通过。直接测 _run_tool_calls。
    # 注意：并行只对 READ_ACTIONS 里的只读工具生效，所以这里用
    # calculator / get_current_time 这两个只读工具名挂慢函数。
    from jarvis.core.agent import _run_tool_calls
    calls = [
        {"id": "c1", "function": {"name": "calculator", "arguments": "{}"}},
        {"id": "c2", "function": {"name": "get_current_time", "arguments": "{}"}},
    ]
    funcs = {"calculator": slow_a, "get_current_time": slow_b}
    events = []
    results = _run_tool_calls(
        calls, funcs, 1,
        lambda kind, payload: events.append((kind, payload)),
        gate=None, parallel=True,
        record=lambda kind, payload: None)
    assert results == ["A 结果", "B 结果"], results
    assert seen == ["a", "b"] or seen == ["b", "a"], seen
    print("[OK] 并行：两个只读工具真的同时跑，结果按原顺序返回")


def test_write_tools_stay_serial():
    tmp = tempfile.mkdtemp()
    old = tools.PROJECT_DIR
    tools.PROJECT_DIR = tmp
    from jarvis.safety import audit as audit_mod
    from jarvis.safety.audit import AuditLog
    old_audit = audit_mod._DEFAULT
    audit_mod._DEFAULT = AuditLog(os.path.join(tmp, "audit.jsonl"))
    try:
        from jarvis.core.agent import _run_tool_calls
        confirms = []

        from jarvis.safety import permissions
        permissions.set_current_gate(permissions.ApprovalGate(
            mode="edit", confirm=lambda a: confirms.append(a["target"]) or True))
        try:
            calls = [
                {"id": "c1", "function": {"name": "write_text_file",
                                          "arguments": '{"path": "1.txt", "content": "one"}'}},
                {"id": "c2", "function": {"name": "write_text_file",
                                          "arguments": '{"path": "2.txt", "content": "two"}'}},
            ]
            results = _run_tool_calls(
                calls, tools.TOOL_FUNCS, 1,
                lambda kind, payload: None,
                gate=None, parallel=True,
                record=lambda kind, payload: None)
            assert len(confirms) == 2, confirms
            assert open(os.path.join(tmp, "1.txt")).read() == "one"
            assert open(os.path.join(tmp, "2.txt")).read() == "two"
            assert all("已写入" in r for r in results), results
        finally:
            permissions.set_current_gate(None)
    finally:
        tools.PROJECT_DIR = old
        audit_mod._DEFAULT = old_audit
    print("[OK] 并行：写工具不并行，确认各弹一次，都写成功")


def test_delegate_subagent():
    # 剧本：主循环先派 delegate；子代理用同一 client 的下一条回复回答；
    # 主循环再用最后一条回复收尾。
    client = FakeClient([
        _tool_call("c1", "delegate",
                   '{"task": "调研一下", "focus": "research"}'),
        _text_reply("子代理：调研结论是 X。"),
        _text_reply("主代理：结论是 X。"),
    ])
    schemas, funcs = subagent_tools(client, "deepseek-flash")
    answer = run_agent(client, "调研一下", verbose=False,
                       extra_tools=schemas, extra_funcs=funcs)
    assert "结论是 X" in answer, answer
    print("[OK] 子代理：delegate 派出去，子代理做完回结果，主代理汇总")


def test_run_subagent_focus():
    client = FakeClient([
        _tool_call("c1", "read_text_file", '{"path": "README.md"}'),
        _text_reply("读到了。"),
    ])
    from jarvis.tools import tools as tools_mod
    old = tools_mod.PROJECT_DIR
    tmp = tempfile.mkdtemp()
    with open(os.path.join(tmp, "README.md"), "w", encoding="utf-8") as f:
        f.write("项目说明\n")
    tools_mod.PROJECT_DIR = tmp
    try:
        answer = run_subagent(client, "读一下 README", "deepseek-flash",
                              focus="research")
        assert "读到了" in answer, answer
    finally:
        tools_mod.PROJECT_DIR = old
    print("[OK] 子代理：research focus 下能读文件")


def test_jobs():
    manager = JobManager()
    job_id, msg = manager.start("echo job-hello")
    assert job_id and "已启动" in msg, msg
    deadline = time.time() + 5
    while manager.status(job_id).startswith(job_id + "：running"):
        if time.time() > deadline:
            break
        time.sleep(0.05)
    result = manager.result(job_id)
    assert "done" in result and "job-hello" in result, result
    assert "job-1" in manager.list_jobs()
    print("[OK] 后台任务：启动、查状态、取结果、列表都正常")


def test_jobs_timeout_and_danger():
    manager = JobManager()
    # 跨平台睡 10 秒：cmd.exe 没有 sleep 命令，用当前 Python 解释器睡。
    sleep_cmd = f'"{sys.executable}" -c "import time; time.sleep(10)"'
    job_id, _ = manager.start(sleep_cmd, timeout=0.3)
    deadline = time.time() + 5
    while "running" in manager.status(job_id):
        if time.time() > deadline:
            break
        time.sleep(0.05)
    assert "timeout" in manager.status(job_id), manager.status(job_id)
    job_id2, msg2 = manager.start("rm -rf /")
    assert job_id2 is None and "拒绝" in msg2, msg2
    assert "没有这个任务" in manager.status("job-999")
    print("[OK] 后台任务：超时杀掉，危险命令拒绝，查不到的有提示")


def test_jobs_tools_wiring():
    manager = JobManager()
    schemas, funcs = jobs_tools(manager)
    names = [s["function"]["name"] for s in schemas]
    assert names == ["run_shell_background", "job_status", "job_result"], names
    msg = funcs["run_shell_background"]("echo hi")
    assert "已启动" in msg, msg
    print("[OK] 后台任务：三个工具注册正常")


def test_budget_stops_loop():
    tracker = UsageTracker()
    # 第一次调用带真实 usage（10 万 prompt token ≈ $0.03），
    # 第二轮开始前费用已超上限，循环停下。没 usage 时费用是未知，
    # 预算拦不住——这是 U0-5 的约定，不是 bug。
    client = FakeClient([
        _tool_call("c1", "calculator", '{"expression": "1 + 1"}',
                   usage=SimpleNamespace(prompt_tokens=100000,
                                         completion_tokens=10)),
        _text_reply("等于 2。"),
    ])
    answer = run_agent(client, "1+1 等于几？", verbose=False,
                       usage=tracker, max_budget_usd=0.000001)
    assert "费用上限" in answer, answer
    assert tracker.calls >= 1
    print("[OK] 预算：费用超上限就停下并报数")


if __name__ == "__main__":
    test_parallel_read_tools()
    test_write_tools_stay_serial()
    test_delegate_subagent()
    test_run_subagent_focus()
    test_jobs()
    test_jobs_timeout_and_danger()
    test_jobs_tools_wiring()
    test_budget_stops_loop()
    print("全部通过。")
