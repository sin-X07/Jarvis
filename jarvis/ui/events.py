"""事件模型（U0-1）：agent 核心与三个界面之间的契约。

`run_agent` 的 `on_event(kind, payload)` 只发这张表里的事件；三个
界面（网页 / TUI / 桌面）只订阅这张表，不再直接读 agent 内部状态。
事件是瞬时的（UI 渲染用），落盘的是另一套东西：会话 JSONL
（`session_store.py`）只记 user / assistant / tool_call /
tool_result / summary / todo / plan / error。

谁发什么：
  核心（agent.py / model.py）：text_delta、thinking_delta、
      tool_start、tool_end、status、turn_end、error
  界面层（确认回调里）：approval_request
  计划流程（U4，planner）：todo_set、plan_proposed
  Diff 流程（U5，diffutil 接入后）：diff_proposed

另外这里放每工具的“一句话摘要”注册表（U0-4）：工具卡片上显示
的不是原始 JSON，而是 `summarise_tool_call(name, args)` 返回的
一行摘要，例如 "读 README.md"、"执行 git status"。
"""

# 事件种类表：字段是 payload 里约定有的键。
EVENT_KINDS = {
    "text_delta": {
        "fields": ["text"],
        "emitted_by": "核心",
        "description": "一片 token，追加到当前气泡，末尾闪烁光标。",
    },
    "thinking_delta": {
        "fields": ["text"],
        "emitted_by": "核心",
        "description": "一段思考流，进折叠的“思考中” disclosure，不和正文混在一起。",
    },
    "tool_start": {
        "fields": ["step", "name", "args"],
        "emitted_by": "核心",
        "description": "工具开始：新建一张工具卡片，状态“进行中”。"
                       "界面渲染时用 summarise_tool_call(name, args) 生成"
                       "一行摘要，用 tool_risk(name) 定卡片颜色。",
    },
    "tool_end": {
        "fields": ["step", "name", "result", "status", "duration_ms"],
        "emitted_by": "核心",
        "description": "工具结束：卡片结算 ✓/✗ + 耗时；界面用 short(result)"
                       " 生成输出预览，点开看全文。",
    },
    "todo_set": {
        "fields": ["todos"],
        "emitted_by": "计划流程（U4）",
        "description": "待办清单全量 [{content, status}]，界面整表重渲染。"
                       "status 是 pending / active / done，active 同时"
                       "只有一个（第一个未完成的，派生）。",
    },
    "plan_proposed": {
        "fields": ["markdown"],
        "emitted_by": "计划流程（U4）",
        "description": "计划卡片：批准 / 编辑 / 拒绝。",
    },
    "diff_proposed": {
        "fields": ["file", "diff"],
        "emitted_by": "Diff 流程（U5）",
        "description": "Diff 卡片：绿/红行，接受全部 / 拒绝全部。",
    },
    "approval_request": {
        "fields": ["tool", "command", "risk"],
        "emitted_by": "界面层（确认回调）",
        "description": "确认卡片：允许一次 / 总是允许 / 拒绝 / 编辑。",
    },
    "status": {
        "fields": ["model", "prompt_tokens", "completion_tokens",
                   "total_tokens", "context_pct", "cost_cny", "calls",
                   "unknown_calls"],
        "emitted_by": "核心",
        "description": "状态栏/状态行更新；token 数全部来自 API 真实 usage。",
    },
    "error": {
        "fields": ["message", "retryable"],
        "emitted_by": "核心",
        "description": "就地错误卡片 + 重试按钮，保留已出文字。",
    },
    "turn_end": {
        "fields": ["step", "answer"],
        "emitted_by": "核心",
        "description": "本轮结束：气泡收尾、光标消失、用量结算。",
    },
}


def new_event(kind: str, payload: dict | None = None) -> dict:
    """造一个事件 dict：{"kind": kind, "payload": {...}}。"""
    if kind not in EVENT_KINDS:
        raise ValueError(f"未知的事件种类：{kind}，见 EVENT_KINDS")
    return {"kind": kind, "payload": dict(payload or {})}


def short(text, limit: int = 60) -> str:
    """一句话截断：去空白、超长加 …。工具卡片的摘要和参数预览都用它。"""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _first(args: dict, *keys: str, default: str = "") -> str:
    for key in keys:
        if key in args and args[key] not in (None, ""):
            return str(args[key])
    return default


# 工具名 -> 摘要函数（args 是解析后的 dict）。没列出的工具走兜底。
_SUMMARISERS = {
    "get_current_time": lambda a: "看现在几点",
    "calculator": lambda a: "算 " + short(_first(a, "expression"), 40),
    "read_text_file": lambda a: "读 " + _first(a, "path"),
    "list_files": lambda a: "列出 " + (_first(a, "path") or "."),
    "write_text_file": lambda a: "写 " + _first(a, "path"),
    "edit_file": lambda a: "改 " + _first(a, "path"),
    "fetch_url": lambda a: "访问 " + short(_first(a, "url"), 50),
    "run_shell": lambda a: "执行 " + short(_first(a, "command"), 50),
    "run_shell_background": lambda a: "后台跑 " + short(_first(a, "command"), 50),
    "glob_files": lambda a: "找文件 " + _first(a, "pattern"),
    "grep_files": lambda a: "搜 " + _first(a, "pattern") + "（"
                            + (_first(a, "path") or ".") + "）",
    "git_status": lambda a: "git status",
    "git_diff": lambda a: "git diff",
    "git_log": lambda a: "git log（" + str(_first(a, "count") or 10) + " 条）",
    "git_commit": lambda a: "提交：" + short(_first(a, "message"), 40),
    "web_search": lambda a: "搜索 " + short(_first(a, "query"), 40),
    "remember_fact": lambda a: "记住：" + short(_first(a, "fact"), 40),
    "recall_facts": lambda a: "回忆：" + short(_first(a, "query"), 40),
    "delete_fact": lambda a: "忘掉：" + short(_first(a, "fact"), 40),
    "update_fact": lambda a: ("改记忆：" + short(_first(a, "old_fact"), 20)
                              + " → " + short(_first(a, "new_fact"), 20)),
    "update_todo": lambda a: ("更新待办：" + (_first(a, "title") or
                              ("第 " + str(_first(a, "index")) + " 步"
                               if _first(a, "index") else ""))),
    "job_status": lambda a: "查任务 " + _first(a, "job_id"),
    "job_result": lambda a: "取任务结果 " + _first(a, "job_id"),
    "delegate": lambda a: "派子代理：" + short(_first(a, "task"), 40),
    "use_skill": lambda a: "用技能 " + _first(a, "name"),
}


def summarise_tool_call(name: str, args) -> str:
    """工具调用的一行摘要。args 可以是 dict 或 JSON 字符串；不知道的
    工具走兜底（名字 + 第一个参数值），绝不直接甩原始 JSON。"""
    if isinstance(args, str):
        try:
            import json
            args = json.loads(args or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
    if not isinstance(args, dict):
        args = {}
    fn = _SUMMARISERS.get(name)
    if fn is not None:
        try:
            return fn(args)
        except Exception:  # noqa: BLE001 - 摘要函数炸了就走兜底，不拦主流程
            pass
    if name.startswith("mcp__"):
        return "MCP 工具 " + name[len("mcp__"):]
    for value in args.values():
        if value not in (None, ""):
            return name + " " + short(value, 40)
    return name


# 工具风险等级：卡片颜色用它（read 中性 / write 琥珀 / danger 红）。
# 没列出的工具默认按 write 算（保守：不知道的先当成会改东西，
# 包括动态的 mcp__ 工具——不知道它干什么，先当写操作）。
_READ_TOOLS = {
    "get_current_time", "calculator", "read_text_file", "list_files",
    "glob_files", "grep_files", "git_status", "git_diff", "git_log",
    "fetch_url", "web_search", "recall_facts", "job_status", "job_result",
    "use_skill",
}
_DANGER_TOOLS = {"run_shell", "run_shell_background"}


def tool_risk(name: str) -> str:
    """工具风险等级：read / write / danger。"""
    if name in _DANGER_TOOLS:
        return "danger"
    if name in _READ_TOOLS:
        return "read"
    return "write"


def friendly_error(message: str) -> str | None:
    """已知错误的恢复提示（U6-4）：按错误文本给一句话人话。

    三个界面的错误卡片都在原文后面缀这句；没认出来的返回 None，
    界面只显示原文，不瞎猜。"""
    text = (message or "").lower()
    if "429" in text or "rate limit" in text or "ratelimit" in text:
        return "请求太频繁（429）：模型端在限流，等一会儿再试。"
    if ("401" in text or "unauthorized" in text
            or "incorrect api key" in text or "invalid api key" in text
            or "authentication" in text):
        return "key 无效或过期：去设置里重新填 key。"
    if ("connect" in text or "connection" in text or "timed out" in text
            or "timeout" in text or "network" in text
            or "unreachable" in text or "name resolution" in text
            or "temporary failure" in text):
        return "网络不通：检查网络或代理，稍后再试。"
    if ("context_length_exceeded" in text or "context length" in text
            or "maximum context" in text):
        return "上下文超长：开新会话，或等自动压缩。"
    if "insufficient_quota" in text or "insufficient quota" in text:
        return "账户额度不足：去 DeepSeek 平台看余额。"
    return None
