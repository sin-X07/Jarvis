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

from audit import audit
from diffutil import diff_summary, make_diff
from permissions import current_gate

# 写文件前钩子注册表：checkpoint.py（P4）和 hooks.py（P6）往这里挂
# 回调，签名 hook(path) -> (ok, message)；ok=False 时阻止写入。
_BEFORE_WRITE_HOOKS = []


def register_before_write_hook(func) -> None:
    """注册一个写前钩子（检查点备份、格式化检查都走这里）。"""
    _BEFORE_WRITE_HOOKS.append(func)


def _run_before_write_hooks(path: str):
    """跑所有写前钩子，返回 (ok, message)；第一个失败就停。"""
    for hook in _BEFORE_WRITE_HOOKS:
        try:
            ok, message = hook(path)
        except Exception as exc:  # noqa: BLE001 - 钩子炸了按失败算，不让写
            return False, f"写前检查出错：{exc}"
        if not ok:
            return False, message or "写前检查未通过。"
    return True, ""

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


def _guarded_write(path: str, target: str, content: str, old_text: str) -> str:
    """写文件的三道关（P1，供 write_text_file / edit_file 共用）：
    权限门看 diff 确认 → 写前钩子（检查点备份等）→ 写完记审计。"""
    gate = current_gate()
    if gate.mode == "readonly":
        audit("write_file", path, ok=False, detail="只读模式")
        return "拒绝：当前是只读模式（readonly），不允许写文件。"
    diff = make_diff(old_text, content, path)
    if not gate.review_diff(path, diff):
        audit("write_file", path, ok=False, detail="用户拒绝")
        return "未写入：用户拒绝了这次修改。"
    ok, message = _run_before_write_hooks(target)
    if not ok:
        audit("write_file", path, ok=False, detail=message[:100])
        return f"未写入：{message}"
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as exc:
        audit("write_file", path, ok=False, detail=str(exc)[:100])
        return f"写入失败: {exc}"
    audit("write_file", path, ok=True,
          detail=f"{len(content.encode('utf-8'))} 字节，{diff_summary(diff)}")
    return f"已写入 {path}（{len(content.encode('utf-8'))} 字节）。"


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
    old_text = ""
    if os.path.exists(target):
        try:
            with open(target, encoding="utf-8") as f:
                old_text = f.read()
        except OSError:
            old_text = ""
    return _guarded_write(path, target, content, old_text)


def edit_file(path: str, old_text: str, new_text: str) -> str:
    """精确编辑：把文件里唯一一处 old_text 换成 new_text。

    找不到、或找到多处时都不写，返回原因让模型自己纠正（多给上下文
    让匹配唯一）。写入前同样过 diff 确认 + 写前钩子 + 审计。
    """
    target = _resolve_inside_project(path)
    if target is None:
        return "拒绝：只能编辑本项目目录内的文件。"
    if not os.path.isfile(target):
        return f"文件不存在: {path}"
    try:
        with open(target, encoding="utf-8") as f:
            content = f.read()
    except OSError as exc:
        return f"读取失败: {exc}"
    count = content.count(old_text)
    if count == 0:
        return f"找不到要替换的文本：{old_text[:80]!r}，请先读一遍文件确认原文。"
    if count > 1:
        return (f"找到 {count} 处匹配，无法确定改哪一处：请把 old_text 写长一点，"
                "带上周围的上下文，让它在文件里唯一。")
    return _guarded_write(path, target, content.replace(old_text, new_text), content)


def glob_files(pattern: str) -> str:
    """按 glob 找文件，如 'src/**/*.py'。只找项目目录内的，返回相对路径。"""
    import fnmatch

    matches = []
    for root, dirs, files in os.walk(PROJECT_DIR):
        dirs[:] = [d for d in dirs
                   if d not in (".git", "__pycache__", "node_modules", ".venv")
                   and not d.startswith(".")]
        for name in files:
            full = os.path.join(root, name)
            # 相对路径统一用 /：Windows 上 os.path 会出反斜杠，模型和
            # 测试都按 / 写路径，保持两平台输出一致。
            rel = os.path.relpath(full, PROJECT_DIR).replace(os.sep, "/")
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
                matches.append(rel)
        if len(matches) >= 200:
            break
    matches.sort()
    if not matches:
        return f"没有匹配 {pattern!r} 的文件。"
    lines = matches[:100]
    if len(matches) > 100:
        lines.append(f"... 还有 {len(matches) - 100} 个未列出")
    return "\n".join(lines)


def grep_files(pattern: str, path: str = ".") -> str:
    """按正则搜文件内容，返回“文件:行号: 行内容”。只搜文本文件。"""
    target = _resolve_inside_project(path)
    if target is None:
        return "拒绝：只能搜索本项目目录内的文件。"
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return f"正则写错了：{exc}"
    hits = []
    if os.path.isfile(target):
        files = [target]
    elif os.path.isdir(target):
        files = []
        for root, dirs, names in os.walk(target):
            dirs[:] = [d for d in dirs
                       if d not in (".git", "__pycache__", "node_modules", ".venv")
                       and not d.startswith(".")]
            for name in names:
                files.append(os.path.join(root, name))
    else:
        return f"路径不存在: {path}"
    for full in files:
        try:
            with open(full, encoding="utf-8", errors="strict") as f:
                for lineno, line in enumerate(f, 1):
                    if rx.search(line):
                        rel = os.path.relpath(full, PROJECT_DIR).replace(
                            os.sep, "/")
                        hits.append(f"{rel}:{lineno}: {line.rstrip()[:200]}")
                        if len(hits) >= 50:
                            return "\n".join(hits) + "\n... 还有更多匹配，只显示前 50 条"
        except (OSError, UnicodeDecodeError):
            continue  # 二进制文件、读不了的文件直接跳过
    if not hits:
        return f"没有匹配 {pattern!r} 的内容。"
    return "\n".join(hits)


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


def web_search(query: str, base_url: str = "https://html.duckduckgo.com") -> str:
    """网页搜索：查报错、查文档不用离开循环。

    默认走 DuckDuckGo 的 html 端点（不用 key），解析结果页的标题+链接，
    返回前 5 条。base_url 是测试用的注入点（测试起本地服务冒充它）。
    搜不到或解析失败时返回提示，不抛异常。
    """
    import urllib.parse

    url = base_url.rstrip("/") + "/html/?q=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={"User-Agent": "Jarvis/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read(200_000).decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return f"搜索失败：{exc}"
    # DuckDuckGo html 结果：<a class="result__a" href="链接">标题</a>
    found = re.findall(
        r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        raw, flags=re.S | re.I)
    results = []
    for href, title in found[:5]:
        title = re.sub(r"<[^>]+>", "", title).strip()
        title = re.sub(r"\s+", " ", title)
        if title and href:
            results.append(f"- {title}\n  {href}")
    if not results:
        return f"没搜到和 {query!r} 相关的结果。"
    return "\n".join(results)


def _run_git(args: list) -> str:
    """跑一条只读 git 命令（不用 shell，直接调 git 二进制）。"""
    import subprocess

    try:
        proc = subprocess.run(
            ["git"] + args, cwd=PROJECT_DIR, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=15)
    except FileNotFoundError:
        return "这台机器上没有装 git。"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"git 执行失败：{exc}"
    output = (proc.stdout or "") + (proc.stderr or "")
    if "not a git repository" in output or "不是一个 git 仓库" in output:
        return "当前目录不是一个 git 仓库。"
    return output.strip()[:4000] or "(没有输出)"


def git_status() -> str:
    """git status --short：工作区里改了什么，一眼看清。"""
    return _run_git(["status", "--short"])


def git_diff() -> str:
    """git diff：未暂存的改动内容（截断到 4000 字符）。"""
    return _run_git(["diff"])


def git_log(count: int = 10) -> str:
    """git log：一行一条最近的提交记录。"""
    return _run_git(["log", f"-{max(1, min(count, 50))}", "--oneline"])


def git_commit(message: str, paths: list | None = None) -> str:
    """提交指定的文件。提交前过确认门；只提交 paths 里列的文件，
    不该提交的不会被顺带提交。"""
    gate = current_gate()
    targets = paths or []
    action = {"kind": "git_commit", "target": ", ".join(targets) or "全部改动",
              "detail": f"提交信息：{message}"}
    ok, why = gate.approve(action)
    if not ok:
        audit("git_commit", message[:100], ok=False, detail="用户拒绝")
        return f"未提交：{why}"
    import subprocess

    try:
        if targets:
            add = subprocess.run(["git", "add"] + targets, cwd=PROJECT_DIR,
                                 capture_output=True, text=True, timeout=15)
            if add.returncode != 0:
                return f"git add 失败：{(add.stderr or '').strip()[:200]}"
        proc = subprocess.run(["git", "commit", "-m", message], cwd=PROJECT_DIR,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"git commit 失败：{exc}"
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()[:1000]
    audit("git_commit", message[:100], ok=proc.returncode == 0)
    return output or "(没有输出)"


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
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "在项目目录里跑一条 shell 命令，返回退出码和输出。只读命令直接跑，写命令（删/改/装包等）要先经过用户确认，危险命令直接拒绝。命令超时默认 30 秒，输出过长会自动截断。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要跑的命令，例如 'git status' 或 'python test_loop.py'",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "超时秒数，不填用默认 30 秒",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "精确编辑文件：把文件里唯一一处 old_text 换成 new_text。找不到或找到多处时不写，返回原因。改之前会先给用户看 diff 确认。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径，例如 'agent.py'"},
                    "old_text": {"type": "string", "description": "要替换的原文（带上周围上下文，让它在文件里唯一）"},
                    "new_text": {"type": "string", "description": "替换成的新文本"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob_files",
            "description": "按 glob 找文件，例如 'src/**/*.py'。想知道项目里有哪些文件时先调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "glob pattern，例如 '**/*.py'"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_files",
            "description": "按正则搜文件内容，返回“文件:行号: 行内容”。找某个函数、某个错误出现在哪时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "正则，例如 'def run_agent'"},
                    "path": {"type": "string", "description": "搜索范围，默认 '.'（整个项目）"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "git status：工作区里改了什么文件。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "git diff：未暂存的改动内容。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": "git log：最近的提交记录，一行一条。",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "看几条，默认 10"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": "提交指定的文件。提交前要经过用户确认，只提交 paths 里列的文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "提交信息"},
                    "paths": {"type": "array", "items": {"type": "string"},
                              "description": "要提交的文件相对路径，不填则只提交已暂存的"},
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "网页搜索：查报错、查文档、查最新信息。返回前 5 条结果的标题和链接。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词，例如 'Python asyncio 超时'"},
                },
                "required": ["query"],
            },
        },
    },
]

from shell_tools import run_shell  # noqa: E402 - 放在 TOOLS 之后，避免循环导入

TOOL_FUNCS = {
    "get_current_time": get_current_time,
    "calculator": calculator,
    "read_text_file": read_text_file,
    "list_files": list_files,
    "write_text_file": write_text_file,
    "fetch_url": fetch_url,
    "run_shell": run_shell,
    "edit_file": edit_file,
    "glob_files": glob_files,
    "grep_files": grep_files,
    "git_status": git_status,
    "git_diff": git_diff,
    "git_log": git_log,
    "git_commit": git_commit,
    "web_search": web_search,
}
