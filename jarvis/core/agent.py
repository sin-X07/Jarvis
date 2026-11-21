"""Jarvis starter: a minimal agent loop driven by DeepSeek.

This is the whole "agent" in ~100 lines:

    user message
        -> model (with tool schemas)
        -> model either answers, or asks to call a tool
        -> run the tool, feed the result back
        -> repeat until the model answers, or MAX_STEPS runs out

That loop -- think, act, observe, repeat -- is the agent loop.
Everything else in a "personal agent" (memory, planning, a harness
around the loop) is built on top of it.

Model name note (verified 2026-11-21):
    DeepSeek's official API serves DeepSeek-V4.1-Flash-1017 under the
    model id `deepseek-flash`. The old id `deepseek-v4-flash` still works
    but routes to the same weights. Do NOT send `deepseek-v4.1-1017` --
    that is the weights release name, not an API model id.

模型调用走 model.py（重试 + 用量）；安全门走 permissions.py
（P1）；会话落盘走 session_store.py（P3）。这个文件只管循环本身。
"""

import json
import sys

from jarvis.core.config import load_config
from jarvis.core.model import BASE_URL, MODEL, build_client, call_model
from jarvis.core.model import _accumulate_stream, _message_to_dict  # noqa: F401 - 兼容旧导入

__all__ = ["SYSTEM_PROMPT", "MODEL", "BASE_URL", "MAX_STEPS",
           "build_client", "run_agent", "main"]

_cfg = load_config()
MAX_STEPS = _cfg.max_steps

SYSTEM_PROMPT = (
    "你是 Jarvis，一个个人助手。你可以用工具获取信息、做计算、读文件。"
    "遇到需要事实或计算的问题，先调用工具，不要凭空编造数字和时间。"
    "回答用中文，简洁直接。"
)


def _call_model(client, model, messages, tools, stream, on_token,
                usage=None):
    """调一次模型，返回 dict 形式的消息；用量顺手记进 tracker（如果给了）。"""
    msg, usage_dict = call_model(
        client, model, messages, tools, stream=stream, on_token=on_token)
    if usage is not None:
        usage.record(usage_dict, model=model)
    return msg


def run_agent(client, user_message: str, model: str = MODEL,
              max_steps: int = MAX_STEPS, verbose: bool = True,
              history: list | None = None,
              extra_tools: list | None = None,
              extra_funcs: dict | None = None,
              on_event=None, stream: bool = False,
              usage=None, max_budget_usd: float | None = None,
              gate=None, session_store=None, session_id=None,
              parallel: bool = True, read_only: bool = False,
              tools_override: list | None = None,
              funcs_override: dict | None = None) -> str:
    """Run one agent loop. Returns the model's final answer as text.

    history: 传一个 ConversationMemory.messages 列表进来，就变成多轮对话
        —— 本轮的问题追加在历史末尾，循环里产生的消息直接写进这个列表，
        下次调用还接着用。不传则和原来一样：每次都是全新的一问一答。
    extra_tools / extra_funcs: 追加的工具（比如记忆工具），和 tools.py
        里原有的合并后一起交给模型。
    on_event: 可选回调 on_event(kind, payload)，循环里有进展就调它：
        ("token", text) 流式吐出的每段文字；("tool_call", {...}) 调工具前；
        ("tool_result", {...}) 工具返回后。不传时退化成 verbose 打印。
    stream: True 时用流式调用，回答边生成边通过 ("token", ...) 事件发出。
        历史里存的消息统一是 dict，两种模式都一样。
    usage: UsageTracker（可选），每次模型调用的 token/费用记进去；
        传了它，调用方才能在结束时打出费用小结。
    max_budget_usd: 费用上限（美元），超了就停下并告诉用户花了多少。
    gate: ApprovalGate（P1），没传时工具按默认规则跑（直接调库的调用
        视为可信，确认门只在 CLI/GUI/网页这些界面层装上）。
    session_store / session_id: 传了就把每一步追加进会话 JSONL（P3），
        关掉再开能接着干。
    parallel: 一轮里多个无依赖的只读工具调用是否并行跑（P5）。
    read_only: 规划模式（P4）：只允许读动作，写工具不交给模型，
        模型硬要调也只会得到一句拒绝。
    """
    if history is None:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]
    else:
        messages = history
        messages.append({"role": "user", "content": user_message})
    tools = list(TOOLS) + list(extra_tools or [])
    funcs = dict(TOOL_FUNCS)
    funcs.update(extra_funcs or {})
    if tools_override is not None:
        # 子代理专用：工具集由调用方完全指定，不再叠加默认 TOOLS。
        tools = list(tools_override)
    if funcs_override is not None:
        funcs = dict(funcs_override)
    if read_only:
        # 只读模式：工具表里只留读动作，权限门也强制只读。
        from jarvis.safety.permissions import READ_ACTIONS, ApprovalGate
        tools = [t for t in tools
                 if t.get("function", {}).get("name") in READ_ACTIONS]
        funcs = {name: fn for name, fn in funcs.items()
                 if name in READ_ACTIONS}
        gate = ApprovalGate(mode="readonly")
    allowed_names = {t.get("function", {}).get("name") for t in tools}

    def record(event_type, payload):
        if session_store is not None and session_id is not None:
            session_store.append_event(session_id, event_type, payload)

    record("user", {"content": user_message})

    # 装上本次运行的权限门（P1）：工具执行时按它来确认/拒绝。
    from jarvis.safety.permissions import current_gate, set_current_gate
    _prev_gate = current_gate()
    if gate is not None:
        set_current_gate(gate)
    try:
        return _run_loop(client, user_message, model, max_steps, verbose,
                         messages, tools, funcs, on_event, stream,
                         usage, max_budget_usd, session_store, session_id,
                         parallel, gate, record, allowed_names)
    finally:
        set_current_gate(_prev_gate)


def _run_loop(client, user_message, model, max_steps, verbose,
              messages, tools, funcs, on_event, stream,
              usage, max_budget_usd, session_store, session_id,
              parallel, gate, record, allowed_names=None):
    """循环本体（从 run_agent 里拆出来，方便 gate 的 try/finally 包裹）。"""

    def report(kind, payload=None):
        if on_event is not None:
            on_event(kind, payload)
        elif verbose:
            if kind == "tool_call":
                print(f"[step {payload['step']}] 调用工具 {payload['name']}({payload['args']})")
            elif kind == "tool_result":
                print(f"[step {payload['step']}] 工具返回: {str(payload['result'])[:200]}")
            elif kind == "direct":
                print(f"[step {payload['step']}] 模型直接回答，循环结束")

    for step in range(1, max_steps + 1):
        if usage is not None and max_budget_usd:
            if usage.cost_usd >= max_budget_usd:
                answer = (f"已达到费用上限 ${max_budget_usd:.2f}"
                          f"（{usage.summary()}），先停下来。")
                record("assistant", {"content": answer})
                messages.append({"role": "assistant", "content": answer})
                return answer
        msg = _call_model(client, model, messages, tools, stream,
                          lambda text: report("token", text), usage=usage)
        if not msg.get("tool_calls"):
            report("direct", {"step": step})
            # 多轮对话时，模型的回答也要留进历史，否则下次它就看不到自己说过什么
            messages.append(msg)
            record("assistant", {"content": msg.get("content")})
            return msg.get("content") or "(空回答)"

        # The model wants to act: run each requested tool, feed results back.
        messages.append(msg)
        record("assistant", {"content": msg.get("content"),
                             "tool_calls": msg.get("tool_calls")})
        calls = msg["tool_calls"]
        results = _run_tool_calls(
            calls, funcs, step, report, gate, parallel, record, allowed_names)
        for call, result in zip(calls, results):
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": str(result),
            })

    answer = "达到最大步数，任务未完成。"
    record("assistant", {"content": answer})
    messages.append({"role": "assistant", "content": answer})
    return answer


def _run_tool_calls(calls, funcs, step, report, gate, parallel, record,
                    allowed_names=None):
    """执行一轮里的工具调用，返回和 calls 一一对应的结果列表。

    三段式：先串行解析参数并上报 tool_call；再执行；最后按原顺序
    上报 tool_result。执行时：多个只读调用（P5-1）并行跑，需要确认
    的调用（写文件、跑命令）必须串行——确认框不能同时弹两个。
    allowed_names 不为 None 时，不在名单里的工具直接拒绝（规划模式）。
    """
    from concurrent.futures import ThreadPoolExecutor

    from jarvis.safety.permissions import READ_ACTIONS

    prepared = []   # (call, name, func, args) 待执行
    results = [None] * len(calls)
    for i, call in enumerate(calls):
        call_id = call["id"]
        name = call["function"]["name"]
        if allowed_names is not None and name not in allowed_names:
            result = (f"当前是规划模式（只读），不能执行 {name}。"
                      "先完成调研和计划，确认后再执行。")
            report("tool_call", {"step": step, "name": name, "args": {}})
            report("tool_result", {"step": step, "name": name, "result": result})
            record("tool_result", {"name": name, "result": result,
                                   "tool_call_id": call_id})
            results[i] = result
            continue
        try:
            args = json.loads(call["function"]["arguments"] or "{}")
        except (json.JSONDecodeError, TypeError):
            # 参数不是合法 JSON：别崩，把错误喂回模型让它自己纠正。
            result = f"工具参数解析失败：{name} 的 arguments 不是合法 JSON，请重试。"
            report("tool_call", {"step": step, "name": name, "args": {}})
            report("tool_result", {"step": step, "name": name, "result": result})
            record("tool_result", {"name": name, "result": result,
                                   "tool_call_id": call_id})
            results[i] = result
            continue
        func = funcs.get(name)
        if func is None:
            result = f"未知工具: {name}"
            report("tool_call", {"step": step, "name": name, "args": args})
            report("tool_result", {"step": step, "name": name, "result": result})
            record("tool_result", {"name": name, "result": result,
                                   "tool_call_id": call_id})
            results[i] = result
            continue
        report("tool_call", {"step": step, "name": name, "args": args})
        record("tool_call", {"name": name, "args": args, "tool_call_id": call_id})
        prepared.append((i, call, name, func, args))

    def _execute(item):
        _i, _call, name, func, args = item
        try:
            return func(**args)
        except Exception as exc:  # noqa: BLE001 - 工具炸了也要喂回模型，不崩循环
            return f"工具 {name} 执行出错：{type(exc).__name__}: {exc}"

    can_parallel = (parallel and len(prepared) > 1
                    and all(name in READ_ACTIONS for (_, _, name, _, _) in prepared))
    if can_parallel:
        with ThreadPoolExecutor(max_workers=3) as pool:
            executed = list(pool.map(_execute, prepared))
    else:
        executed = [_execute(item) for item in prepared]
    for (i, call, name, _func, _args), result in zip(prepared, executed):
        report("tool_result", {"step": step, "name": name, "result": str(result)})
        record("tool_result", {"name": name, "result": str(result)[:2000],
                               "tool_call_id": call["id"]})
        results[i] = result
    return results


def main() -> None:
    if len(sys.argv) < 2:
        print('用法: python -m jarvis.core.agent "你的问题"')
        print('示例: python -m jarvis.core.agent "现在几点？12*(3+4) 等于多少？"')
        raise SystemExit(1)
    client = build_client()
    print(f"模型: {MODEL} @ {BASE_URL}")
    answer = run_agent(client, " ".join(sys.argv[1:]))
    print("\n--- Jarvis ---")
    print(answer)


from jarvis.tools.tools import TOOLS, TOOL_FUNCS  # noqa: E402 - 放在末尾避免循环导入

if __name__ == "__main__":
    main()
