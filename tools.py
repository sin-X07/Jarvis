"""Jarvis starter: tools the agent can call.

Each tool has two parts:
  1. an OpenAI function-calling schema (TOOLS) -- what the model sees;
  2. a plain Python implementation -- what actually runs.

To add a tool: write a function, add its schema to TOOLS, and register
it in TOOL_FUNCS. The agent loop in agent.py does the rest.

Safety rule for every file/web tool: the agent may only touch files
inside the project directory, and fetch_url only does plain GETs.
A tool that can write files or reach the network is exactly where a
prompt-injection could do real damage, so the sandbox boundary lives
here, in the tool implementations -- not in the prompt.

Deliberately NOT here yet: running shell commands. That is the most
useful tool and the most dangerous one -- the model could run anything,
including deleting files. It needs a confirmation gate first (show the
command, ask the user, then run). That is the next step after this one.
"""

import datetime
import math
import os
import re
import urllib.error
import urllib.request

# Project root: every file tool is sandboxed to this directory.
# Tests monkeypatch this to a temp dir so they never touch the real repo.
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


def _resolve_inside_project(path: str) -> str | None:
    """Resolve path inside PROJECT_DIR. Returns None if it escapes.

    The check uses base + os.sep (not a bare startswith) so that a
    sibling directory like "<project>-evil" cannot slip through.
    """
    base = os.path.abspath(PROJECT_DIR)
    target = os.path.abspath(os.path.join(base, path))
    if target != base and not target.startswith(base + os.sep):
        return None
    return target


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
    target = _resolve_inside_project(path)
    if target is None:
        return "拒绝：只能读取本项目目录内的文件。"
    # 先判目录：Linux 上 open(目录) 报 IsADirectoryError，Windows 上报
    # PermissionError，行为不统一，所以不要靠异常来识别目录，直接先检查。
    if os.path.isdir(target):
        return f"拒绝：{path} 是一个目录。"
    try:
        with open(target, encoding="utf-8") as f:
            return f.read()[:4000]
    except FileNotFoundError:
        return f"文件不存在: {path}"
    except OSError as exc:
        return f"读取失败: {exc}"


def list_files(path: str = ".") -> str:
    """List the entries of a directory inside the project directory."""
    target = _resolve_inside_project(path)
    if target is None:
        return "拒绝：只能查看本项目目录内的内容。"
    if not os.path.isdir(target):
        return f"不是一个目录: {path}"
    try:
        entries = sorted(os.listdir(target))
    except OSError as exc:
        return f"读取失败: {exc}"
    lines = []
    for name in entries[:100]:
        full = os.path.join(target, name)
        lines.append(name + "/" if os.path.isdir(full) else name)
    if len(entries) > 100:
        lines.append(f"... 还有 {len(entries) - 100} 项未列出")
    return "\n".join(lines) if lines else "(空目录)"


def write_text_file(path: str, content: str) -> str:
    """Create or overwrite a text file inside the project directory.

    Parent directories are created as needed (still inside the project).
    Content is capped at 100000 characters so one bad call cannot
    flood the disk.
    """
    if len(content) > 100_000:
        return "拒绝：内容太长（超过 100000 字符）。"
    target = _resolve_inside_project(path)
    if target is None:
        return "拒绝：只能写入本项目目录内的文件。"
    if os.path.isdir(target):
        return f"拒绝：{path} 是一个目录。"
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
        return f"已写入 {path}（{len(content.encode('utf-8'))} 字节）。"
    except OSError as exc:
        return f"写入失败: {exc}"


def fetch_url(url: str) -> str:
    """GET a URL and return its readable text with HTML tags stripped.

    Limits: http/https only, 10s timeout, at most 200KB downloaded,
    at most 4000 characters returned. Failures come back as text
    ("请求失败：...") so the model can react, instead of raising.
    """
    if not url.startswith(("http://", "https://")):
        return "拒绝：只支持 http:// 和 https:// 链接。"
    req = urllib.request.Request(url, headers={"User-Agent": "Jarvis/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read(200_000)
    except urllib.error.HTTPError as exc:
        return f"请求失败：HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return f"请求失败：{exc}"
    text = raw.decode("utf-8", errors="replace")
    # Strip script/style blocks, then all tags, then collapse whitespace.
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:4000] if text else "(页面没有可读文本)"


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
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出本项目目录内某个文件夹下的文件和子文件夹。想知道项目里有什么、再决定读哪个文件时先调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对路径，默认 '.'（项目根目录），例如 'notes'",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_text_file",
            "description": "在本项目目录内新建或覆盖一个文本文件。用于写笔记、保存结果、生成文件。只能写项目内的文件，内容不超过 100000 字符。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对路径，例如 'notes/todo.txt'",
                    },
                    "content": {
                        "type": "string",
                        "description": "要写入的完整内容",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "访问一个 http/https 网页，返回去掉标签后的正文。用于查公开网页内容。只能 GET，不能提交表单或登录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "完整链接，例如 'https://example.com'",
                    }
                },
                "required": ["url"],
            },
        },
    },
]

TOOL_FUNCS = {
    "get_current_time": get_current_time,
    "calculator": calculator,
    "read_text_file": read_text_file,
    "list_files": list_files,
    "write_text_file": write_text_file,
    "fetch_url": fetch_url,
}
