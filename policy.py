"""命令安全策略（P1-4 / P1-7）：危险命令黑名单 + 读写分类。

这只是启发式检查，不是沙箱——真正的隔离要靠操作系统级沙箱
（Codex 那种），个人项目先做到“明显危险的命令进不来”。
规则写在这里集中管理，想加规则改这一个文件。

两类检查：
1. check_command(cmd)：黑名单，命中直接拒绝，并给模型一句话原因。
2. classify_command(cmd)：读命令还是写命令，决定要不要过确认门。
3. escapes_workspace(cmd)：想跳出项目目录（cd .. 这类）直接拒绝。
"""

import re

# (正则, 拒绝原因) —— 命中即拒绝。Windows / Linux 的危险写法都列了。
DANGEROUS_PATTERNS = [
    (r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r)\s+(/|/\*|~|%USERPROFILE%)", "rm -rf 根目录/家目录"),
    (r"\brm\s+-rf\s+/", "rm -rf 根目录"),
    (r"\bformat\s+[a-zA-Z]:", "格式化磁盘"),
    (r"\bmkfs\b", "格式化文件系统"),
    (r"\bdd\b.*\bof=/dev/", "dd 写设备"),
    (r"\bshutdown\b|\breboot\b", "关机/重启"),
    (r"\bdel\s+/s\s+/q\s+[a-zA-Z]:\\", "del /s /q 整盘删除"),
    (r"\brd\s+/s\s+/q\s+[a-zA-Z]:\\", "rd /s /q 整盘删除"),
    (r"Remove-Item\s+.*-Recurse.*[a-zA-Z]:\\", "Remove-Item 递归删整盘"),
    (r"\breg\s+delete\b", "删除注册表项"),
    (r">\s*/dev/", "重定向覆盖设备文件"),
    (r"(curl|wget)\b[^|]*\|\s*(sh|bash|powershell)\b", "管道下载远程脚本直接执行"),
    (r":\(\)\s*\{", "fork 炸弹"),
]

# 写命令的特征：命中即“写”，否则按“读”处理（读命令直接放行）。
WRITE_PATTERNS = [
    r"\brm\b", r"\bdel\b", r"\bremove-item\b", r"\bmkdir\b",
    r"(^|[;&|]\s*)md(\s|$)",  # mkdir 的简写 md：只认独立命令词，不认文件名里的 md
    r"\bmv\b", r"\bmove\b", r"\bcp\b", r"\bcopy\b", r"\bxcopy\b",
    r"\b robocopy\b", r"\bpip\s+install\b", r"\bnpm\s+install\b",
    r"\bgit\s+(commit|push|checkout|reset|clean)\b",
    r"\btouch\b", r">", r">>", r"\battrib\b",
]
_WRITE_RES = [re.compile(p, re.I) for p in WRITE_PATTERNS]

_ESCAPE_RES = [
    re.compile(r"\bcd\s+\.\.", re.I),
    re.compile(r"\bcd\s+/", re.I),
    re.compile(r"\bcd\s+[a-zA-Z]:\\", re.I),
]


def check_command(cmd: str):
    """黑名单检查：返回 (allowed, reason)。"""
    if not cmd or not cmd.strip():
        return False, "命令是空的。"
    for pattern, reason in DANGEROUS_PATTERNS:
        if re.search(pattern, cmd, re.I):
            return False, f"这条命令被安全策略拦下了（{reason}），换一种安全的做法。"
    return True, ""


def classify_command(cmd: str) -> str:
    """读命令返回 "read"，写命令返回 "write"。"""
    for rx in _WRITE_RES:
        if rx.search(cmd):
            return "write"
    return "read"


def escapes_workspace(cmd: str) -> bool:
    """命令是否想跳出项目目录（cd .. 这类）。"""
    return any(rx.search(cmd) for rx in _ESCAPE_RES)
