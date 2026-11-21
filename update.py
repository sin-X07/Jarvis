"""自更新检查（P8-4）：jarvis --update 对比 GitHub releases。

只检查、不下载、不安装：去 GitHub API 拿最新 release 的 tag，
和本地 __version__ 比一比，有新版就告诉用户去哪下载。
不自动升级——装新版是用户自己的事（换文件、重跑安装步骤）。

实现说明：这里直接用 urllib 调 GitHub API，而不是 tools.fetch_url，
因为 fetch_url 会剥 HTML 标签、只留前 4000 字，JSON 可能被截断
导致解析失败。urllib 也是标准库，不引入新依赖。
"""

import json
import urllib.error
import urllib.request

from config import __version__

REPO = "sin-X07/Jarvis"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases/latest"


def _fetch(url: str) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": f"Jarvis/{__version__}",
                      "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read(200_000).decode("utf-8", errors="replace")


def _parse_version(tag: str) -> tuple:
    """v0.3.0 / 0.3.0 → (0, 3, 0)，比不出来的返回空元组。"""
    parts = tag.lstrip("vV").split(".")
    try:
        return tuple(int(p) for p in parts if p != "")
    except ValueError:
        return ()


def check_update(current_version: str | None = None,
                 fetch=_fetch) -> dict:
    """返回 {"current", "latest", "update_available", "url", "notes"/"error"}。"""
    current = current_version or __version__
    try:
        data = json.loads(fetch(RELEASES_URL))
    except (json.JSONDecodeError, urllib.error.URLError,
            TimeoutError, OSError) as exc:
        return {"current": current, "latest": None,
                "update_available": False,
                "error": f"检查失败（没联网或 GitHub 不可达）：{exc}"}
    tag = str(data.get("tag_name") or "")
    latest = _parse_version(tag)
    current_parsed = _parse_version(current)
    update_available = bool(latest and current_parsed
                            and latest > current_parsed)
    return {
        "current": current,
        "latest": tag or None,
        "update_available": update_available,
        "url": data.get("html_url") or f"https://github.com/{REPO}/releases",
        "notes": (data.get("body") or "")[:1000],
    }


def format_update(result: dict) -> str:
    """把检查结果写成人话。"""
    if result.get("error"):
        return result["error"]
    if result["update_available"]:
        lines = [f"有新版：{result['latest']}（当前 {result['current']}）",
                 f"下载：{result['url']}"]
        if result.get("notes"):
            lines.append("")
            lines.append(result["notes"])
        return "\n".join(lines)
    if result.get("latest"):
        return f"已是最新版：{result['current']}（最新 {result['latest']}）"
    return "没找到任何 release（仓库可能还没有发过版）。"
