"""极简 Markdown 渲染（P7）：CLI 与 GUI 共用同一套规则。

只支持日常够用的子集：代码块、行内代码、粗体、斜体、标题、
无序列表、引用。不是完整 Markdown 解析器，复杂语法原样输出。
两个出口：
- to_html：给 GUI 的 QTextEdit（它认 HTML）和网页版参考；
- to_ansi：给 CLI 终端（粗体/反白），非 tty 时原样返回纯文本。
"""

import html
import re


def _escape(text: str) -> str:
    return html.escape(text)


def _inline(text: str) -> str:
    """行内：`代码`、**粗体**、*斜体*（先转义，再套标签）。"""
    text = _escape(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", text)
    return text


def to_html(text: str) -> str:
    """Markdown → HTML 片段（不包 <html>，直接塞进 QTextEdit）。"""
    if not text:
        return ""
    out = []
    lines = text.split("\n")
    i = 0
    in_code = False
    code_lang = ""
    code_buf = []
    list_buf = []
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            if in_code:
                # 围栏之间的每行原文（含收围栏前的末尾换行），
                # 复制代码块时要原样拿回；空块为空串。
                code = _escape("\n".join(code_buf) + "\n") if code_buf else ""
                lang = f" class=\"language-{code_lang}\"" if code_lang else ""
                out.append(f"<pre><code{lang}>{code}</code></pre>")
                code_buf = []
                in_code = False
            else:
                if list_buf:
                    out.append("<ul>" + "".join(list_buf) + "</ul>")
                    list_buf = []
                in_code = True
                code_lang = line.strip()[3:].strip()
            i += 1
            continue
        if in_code:
            code_buf.append(line)
            i += 1
            continue
        stripped = line.strip()
        if stripped.startswith("- ") or stripped.startswith("* "):
            list_buf.append(f"<li>{_inline(stripped[2:])}</li>")
        elif stripped.startswith("# "):
            if list_buf:
                out.append("<ul>" + "".join(list_buf) + "</ul>")
                list_buf = []
            out.append(f"<h3>{_inline(stripped[2:])}</h3>")
        elif stripped.startswith("## "):
            if list_buf:
                out.append("<ul>" + "".join(list_buf) + "</ul>")
                list_buf = []
            out.append(f"<h4>{_inline(stripped[3:])}</h4>")
        elif stripped.startswith("> "):
            if list_buf:
                out.append("<ul>" + "".join(list_buf) + "</ul>")
                list_buf = []
            out.append(f"<blockquote>{_inline(stripped[2:])}</blockquote>")
        elif stripped == "":
            if list_buf:
                out.append("<ul>" + "".join(list_buf) + "</ul>")
                list_buf = []
            out.append("<p></p>")
        else:
            if list_buf:
                out.append("<ul>" + "".join(list_buf) + "</ul>")
                list_buf = []
            out.append(f"<p>{_inline(line)}</p>")
        i += 1
    if in_code:
        out.append(f"<pre><code>{_escape(chr(10).join(code_buf))}</code></pre>")
    if list_buf:
        out.append("<ul>" + "".join(list_buf) + "</ul>")
    return "".join(out)


def to_ansi(text: str, use_color: bool = True) -> str:
    """Markdown → 带 ANSI 样式的终端文本；use_color=False 时原样返回。"""
    if not text or not use_color:
        return text
    out = []
    for line in text.split("\n"):
        if line.startswith("# "):
            out.append("\x1b[1m" + line[2:] + "\x1b[0m")
        elif line.startswith("## "):
            out.append("\x1b[1m" + line[3:] + "\x1b[0m")
        elif line.startswith("```"):
            out.append(line)
        else:
            line = re.sub(r"`([^`]+)`", "\x1b[7m\\1\x1b[0m", line)
            line = re.sub(r"\*\*([^*]+)\*\*", "\x1b[1m\\1\x1b[0m", line)
            out.append(line)
    return "\n".join(out)
