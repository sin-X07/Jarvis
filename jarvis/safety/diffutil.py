"""写文件前的 diff（P1-5）。

改文件之前先算一份 unified diff，让用户看到“要变成什么样”，
确认后才写。diff 本身不含敏感信息之外的东西，就是新旧文本的对比。
"""

import difflib


def make_diff(old_text: str, new_text: str, path: str) -> str:
    """算 unified diff；新文件时 old 为空，diff 就是全部新增行。"""
    old_lines = old_text.splitlines(keepends=True) if old_text else []
    new_lines = new_text.splitlines(keepends=True)
    lines = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="")
    return "".join(lines)


def diff_summary(diff: str) -> str:
    """diff 一句话摘要：+N 行 / -N 行。"""
    added = sum(1 for line in diff.splitlines()
                if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff.splitlines()
                  if line.startswith("-") and not line.startswith("---"))
    return f"+{added} 行 / -{removed} 行"
