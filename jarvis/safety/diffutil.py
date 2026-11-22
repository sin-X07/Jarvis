"""写文件前的 diff（P1-5）。

改文件之前先算一份 unified diff，让用户看到“要变成什么样”，
确认后才写。diff 本身不含敏感信息之外的东西，就是新旧文本的对比。
"""

import difflib


def make_diff(old_text: str, new_text: str, path: str) -> str:
    """算 unified diff；新文件时 old 为空，diff 就是全部新增行。

    每行去尾换行再用 \\n 连接：difflib 在 lineterm="" 时生成的
    ---/+++/@@ 头行本身不带换行，直接 "".join 会把三行粘成一行
    （U5 前一直如此，确认卡片里头行是糊的，parse_diff 也拆不开）。"""
    old_lines = old_text.splitlines(keepends=True) if old_text else []
    new_lines = new_text.splitlines(keepends=True)
    lines = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="")
    return "\n".join(line.rstrip("\n") for line in lines)


def diff_summary(diff: str) -> str:
    """diff 一句话摘要：+N 行 / -N 行。"""
    added = sum(1 for line in diff.splitlines()
                if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff.splitlines()
                  if line.startswith("-") and not line.startswith("---"))
    return f"+{added} 行 / -{removed} 行"


_META_PREFIXES = (
    "index ", "new file mode", "deleted file mode", "old mode",
    "new mode", "similarity index", "rename from", "rename to",
)


def _git_header_path(line: str) -> str:
    """diff --git a/X b/Y → Y（b/ 不存在时退回 a/）。"""
    parts = line.split()
    for token in reversed(parts):
        if token.startswith("b/"):
            return token[2:]
    for token in reversed(parts):
        if token.startswith("a/"):
            return token[2:]
    return parts[-1] if parts else ""


def parse_diff(diff: str) -> list:
    """把一份 unified diff 按文件拆开（U5-1 Diff 卡片的数据来源）。

    每项 {path, lines, added, removed, summary}；lines 只留 @@ 块头
    与内容行（+/- 与上下文），diff --git / index / --- / +++ 这些
    元数据行不留——文件身份由小节头（path + 摘要）承担。
    兼容 git diff（diff --git 开头）和 make_diff（直接 ---/+++ 开头）。
    不是 diff 的行（提示文本等）跳过；解析不到文件时返回 []。
    """
    files = []
    current = None
    for line in (diff or "").splitlines():
        if line.startswith("diff --git "):
            current = {"path": _git_header_path(line), "lines": []}
            files.append(current)
            continue
        if line.startswith("--- a/") or line.startswith("--- /dev/null"):
            path = line[4:].strip()
            if path.startswith("a/"):
                path = path[2:]
            elif path == "/dev/null":
                path = None  # 新文件：路径在后面的 +++ b/ 行
            if current is None or current["lines"]:
                # 新文件节（上一节已有内容行时才另起；紧跟 diff --git
                # 的 --- 只是同一节的文件头，不另起）
                current = {"path": path, "lines": []}
                files.append(current)
            elif path is not None:
                current["path"] = path
            continue
        if line.startswith("+++ b/") or line.startswith("+++ /dev/null"):
            if current is not None and not current.get("path"):
                p = line[4:].strip()
                current["path"] = p[2:] if p.startswith("b/") else p
            continue
        if line.startswith(_META_PREFIXES):
            continue
        if current is None:
            continue
        current["lines"].append(line)
    out = []
    for f in files:
        if not f["lines"]:
            continue
        added = sum(1 for l in f["lines"]
                    if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in f["lines"]
                      if l.startswith("-") and not l.startswith("---"))
        out.append({"path": f["path"] or "(未知文件)", "lines": f["lines"],
                    "added": added, "removed": removed,
                    "summary": f"+{added} 行 / -{removed} 行"})
    return out


def render_diff_grouped(diff: str, max_chars: int = 8000) -> str:
    """Diff 分组纯文本渲染：每文件一个小节头 + 行（CLI /diff 与 TUI 用）。

    没有可解析的改动时原样返回提示文本；空 diff 返回“还没有改动。”。
    """
    files = parse_diff(diff)
    if not files:
        return (diff or "").strip() or "还没有改动。"
    out = []
    for f in files:
        out.append(f"=== {f['path']}（{f['summary']}）===")
        out.extend(f["lines"])
    text = "\n".join(out)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…（改动太多，只显示前一部分）"
    return text
