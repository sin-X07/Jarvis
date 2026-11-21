"""Jarvis starter: tools the agent can call.

Each tool has two parts:
  1. an OpenAI function-calling schema (TOOLS) -- what the model sees;
  2. a plain Python implementation -- what actually runs.

To add a tool: write a function, add its schema to TOOLS, and register
it in TOOL_FUNCS. The agent loop in agent.py does the rest.
"""

import datetime
import math
import os

# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------

def get_current_time() -> str:
    """Return the current local date and time."""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def calculator(expression: str) -> str:
    """Evaluate a simple arithmetic expression, e.g. '12 * (3 + 4)'."""
    allowed = {name: getattr(math, name) for name in
               ("sqrt", "sin", "cos", "tan", "log", "pi", "e")}
    try:
        result = eval(expression, {"__builtins__": {}}, dict(allowed))
        return str(result)
    except Exception as exc:  # noqa: BLE001 - report the error to the model
        return f"计算出错: {exc}"


def read_text_file(path: str) -> str:
    """Read a text file under the project directory and return its content.

    Sandboxed on purpose: the agent may only read files inside this repo,
    so a prompt-injection in model output cannot make it dump /etc/passwd.
    """
    base = os.path.dirname(os.path.abspath(__file__))
    target = os.path.abspath(os.path.join(base, path))
    if not target.startswith(base):
        return "拒绝：只能读取本项目目录内的文件。"
    try:
        with open(target, encoding="utf-8") as f:
            return f.read()[:4000]
    except FileNotFoundError:
        return f"文件不存在: {path}"
    except OSError as exc:
        return f"读取失败: {exc}"


# ---------------------------------------------------------------------------
# Schemas the model sees (OpenAI function-calling format)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前日期和时间。当问题涉及“现在几点”“今天是几号”时调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "计算算术表达式，支持 + - * / () 以及 sqrt/sin/cos/log/pi/e。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "要计算的表达式，例如 '12 * (3 + 4)'",
                    }
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_text_file",
            "description": "读取本项目目录内的一个文本文件，返回其内容。只能读项目内的文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对路径，例如 'README.md'",
                    }
                },
                "required": ["path"],
            },
        },
    },
]

TOOL_FUNCS = {
    "get_current_time": get_current_time,
    "calculator": calculator,
    "read_text_file": read_text_file,
}
