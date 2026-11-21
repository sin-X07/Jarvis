"""权限门（P1-3）：三档权限 + 确认门。

档位（抄 Claude Code 的模式 + Codex 的沙箱分档，取最小可用版）：
- readonly：只能读。写文件、跑命令、git 提交一律拒绝。
- edit（默认）：可改工作区；写操作与危险命令要先经过确认。
- auto：工作区内的写操作自动放行，工作区外仍要确认。

确认门怎么工作：界面层（CLI/GUI/网页）给 gate 一个 confirm 回调，
工具执行前调 `approve(action)`；没有回调时（比如直接调库、跑测试）
视为可信调用，自动放行——确认是 harness 行为，不是库行为。
这和文件沙箱的思路一样：边界在执行处，不在 prompt 里。
"""

# 动作种类：读动作永远放行，只有写动作才过确认门。
READ_ACTIONS = {"read_file", "list_files", "glob_files", "grep_files",
                "git_status", "git_diff", "git_log", "fetch_url",
                "web_search", "get_current_time", "calculator",
                "recall_facts", "job_status", "job_result"}
WRITE_ACTIONS = {"write_file", "edit_file", "shell", "git_commit"}


class ApprovalGate:
    """一次运行的权限门：档位 + 确认回调。"""

    def __init__(self, mode: str = "edit", confirm=None):
        if mode not in ("readonly", "edit", "auto"):
            raise ValueError(f"权限档位只能是 readonly / edit / auto，现在是 {mode!r}")
        self.mode = mode
        # confirm(action) -> bool；action = {"kind", "target", "detail"}
        self.confirm = confirm

    def needs_confirm(self, action: dict) -> bool:
        """这个动作要不要先问用户。"""
        kind = action.get("kind", "")
        if kind in READ_ACTIONS:
            return False
        if self.mode == "readonly":
            return False  # 只读模式是直接拒绝，不是确认
        if self.mode == "auto":
            return False
        return True  # edit 模式：写动作都要确认

    def approve(self, action: dict):
        """返回 (allowed, reason)。reason 是给模型看的一句话。"""
        kind = action.get("kind", "")
        if kind in READ_ACTIONS:
            return True, ""
        if self.mode == "readonly":
            return False, "当前是只读模式（readonly），不允许写文件或跑命令。"
        if self.needs_confirm(action) and self.confirm is not None:
            try:
                ok = bool(self.confirm(action))
            except Exception:
                ok = False
            if not ok:
                return False, "用户拒绝了这次操作。"
        return True, ""

    def review_diff(self, path: str, diff: str) -> bool:
        """写文件前给用户看 diff：返回 True 才写。

        没有 confirm 回调时直接放行（测试、直接调库的场景）；
        只读模式直接拒绝。
        """
        if self.mode == "readonly":
            return False
        if self.confirm is None:
            return True
        action = {"kind": "write_file", "target": path,
                  "detail": diff or "(新文件)"}
        return bool(self.confirm(action))


# -- 进程内当前 gate：agent 循环每次运行时装上，跑完恢复 ----------

_CURRENT = None


def set_current_gate(gate) -> None:
    global _CURRENT
    _CURRENT = gate


def current_gate() -> ApprovalGate:
    """当前 gate；没装过时返回默认（edit 档、无确认回调）。"""
    if _CURRENT is None:
        return ApprovalGate(mode="edit", confirm=None)
    return _CURRENT


def cli_confirm(action: dict) -> bool:
    """CLI 的确认回调：在终端显示动作，等用户敲 y/n。"""
    kind = action.get("kind", "")
    target = action.get("target", "")
    detail = action.get("detail", "")
    print(f"\n[Jarvis 想执行] {kind}：{target}")
    if detail:
        print(detail[:2000])
    try:
        answer = input("执行吗？[y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes", "是")
