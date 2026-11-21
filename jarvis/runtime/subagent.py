"""子代理（P5-2）：主 agent 派活，子 agent 干完回结果。

子代理就是一次独立的 run_agent：自己的历史、自己的上下文、只带
完成这件活需要的工具。主 agent 汇总结果。规则：
- 子代理拿不到 delegate 工具（不能无限套娃，最多一层）；
- 子代理不写会话日志（日志是主会话的，子代理的细节只留结果）；
- focus 决定工具集：research（只读）、code（读写）、test（读+跑命令）。

为什么不用线程/进程池：子代理是顺序的模型调用，瓶颈在 API 不在
CPU；多个子代理并行靠主循环一次派多个 delegate 调用来实现（P5-1
的并行执行会同时跑它们）。
"""

from jarvis.core.agent import run_agent
from jarvis.tools.tools import TOOLS, TOOL_FUNCS

_FOCUS_TOOLS = {
    "research": {"get_current_time", "calculator", "read_text_file",
                 "list_files", "glob_files", "grep_files", "fetch_url",
                 "web_search", "git_status", "git_diff", "git_log",
                 "recall_facts"},
    "code": {"get_current_time", "calculator", "read_text_file",
             "list_files", "write_text_file", "edit_file", "glob_files",
             "grep_files", "fetch_url", "web_search", "git_status",
             "git_diff", "git_log", "run_shell", "recall_facts",
             "remember_fact"},
    "test": {"get_current_time", "read_text_file", "list_files",
             "glob_files", "grep_files", "run_shell", "git_status",
             "git_diff", "git_log", "recall_facts"},
}


def run_subagent(client, task: str, model: str, max_steps: int = 6,
                 focus: str = "research", usage=None,
                 extra_tools=None, extra_funcs=None) -> str:
    """跑一个子代理，返回它最后的回答文本。"""
    allowed = _FOCUS_TOOLS.get(focus, _FOCUS_TOOLS["research"])
    tools = [t for t in TOOLS
             if t.get("function", {}).get("name") in allowed]
    funcs = {name: fn for name, fn in TOOL_FUNCS.items() if name in allowed}
    if extra_tools:
        tools = tools + list(extra_tools)
    if extra_funcs:
        funcs.update(extra_funcs)
    return run_agent(client, task, model=model, max_steps=max_steps,
                     verbose=False, tools_override=tools,
                     funcs_override=funcs, usage=usage)


def subagent_tools(client, model: str, usage=None, max_steps: int = 6):
    """给主循环的 delegate 工具：派一个子代理去干活。"""
    schemas = [{
        "type": "function",
        "function": {
            "name": "delegate",
            "description": "把一个子任务派给子代理去做，子代理做完把结果交回来。适合拆分出去的独立任务，比如“调研 X 的三种做法并对比”“给 Y 写测试”。不要把整个主任务派出去。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "交给子代理的任务，一句话说清目标和验收标准"},
                    "focus": {"type": "string",
                              "description": "子代理的工具范围：research（只读调研）/ code（读写代码）/ test（读+跑命令），默认 research"},
                },
                "required": ["task"],
            },
        },
    }]

    def delegate(task: str, focus: str = "research") -> str:
        if focus not in _FOCUS_TOOLS:
            return f"未知的 focus：{focus}，只能是 research / code / test。"
        result = run_subagent(client, task, model, max_steps=max_steps,
                              focus=focus, usage=usage)
        return "子代理结果：\n" + result

    return schemas, {"delegate": delegate}
