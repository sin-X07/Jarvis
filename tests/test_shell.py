"""P1 测试：run_shell 与写文件确认、审计（不用 key、不联网）。

用临时目录当 PROJECT_DIR、临时审计文件，绝不碰真实项目与 ~/.jarvis。
验证：
  1. 读命令直接跑，返回退出码 + 输出
  2. 危险命令被拒，审计里有记录
  3. 写命令：确认通过才跑，拒绝就不跑；只读模式不跑
  4. 超时返回超时提示
  5. 超长输出被截断（有“省略”字样）
  6. 子进程看不到 DEEPSEEK_API_KEY
  7. 写文件：确认框里能看到 diff；拒绝后文件不动；只读模式不写
  8. 审计日志只记动作不记内容
"""

import os
import sys
import tempfile

from jarvis.safety import audit as audit_mod
from jarvis.safety import permissions
from jarvis.tools import shell_tools
from jarvis.tools import tools
from jarvis.safety.audit import AuditLog
from jarvis.safety.permissions import ApprovalGate


class _Ctx:
    """临时隔离：PROJECT_DIR、审计、gate，跑完恢复。"""

    def __init__(self):
        self.tmp = tempfile.mkdtemp()
        self.audit_path = os.path.join(self.tmp, "audit.jsonl")
        self.old_proj_tools = tools.PROJECT_DIR
        self.old_proj_shell = shell_tools.PROJECT_DIR
        self.old_audit = audit_mod._DEFAULT
        self.old_gate = permissions._CURRENT

    def __enter__(self):
        tools.PROJECT_DIR = self.tmp
        shell_tools.PROJECT_DIR = self.tmp
        audit_mod._DEFAULT = AuditLog(self.audit_path)
        return self

    def __exit__(self, *exc):
        tools.PROJECT_DIR = self.old_proj_tools
        shell_tools.PROJECT_DIR = self.old_proj_shell
        audit_mod._DEFAULT = self.old_audit
        permissions._CURRENT = self.old_gate
        return False


def test_shell_read_command():
    with _Ctx():
        out = shell_tools.run_shell("echo hello-jarvis")
        assert "退出码 0" in out and "hello-jarvis" in out, out
    print("[OK] shell：读命令直接跑，返回退出码+输出")


def test_shell_dangerous_blocked():
    with _Ctx() as ctx:
        out = shell_tools.run_shell("rm -rf /")
        assert "拒绝" in out, out
        rows = AuditLog(ctx.audit_path).recent()
        assert rows and rows[-1]["action"] == "shell" and not rows[-1]["ok"], rows
    print("[OK] shell：危险命令被拒，审计有记录")


def test_shell_write_needs_confirm():
    with _Ctx():
        permissions.set_current_gate(
            ApprovalGate(mode="edit", confirm=lambda a: False))
        # 跨平台写文件：echo + 重定向，cmd.exe 和 sh 都支持（touch 只在 POSIX 有）。
        out = shell_tools.run_shell("echo x > should-not-exist.txt")
        assert "未执行" in out, out
        assert not os.path.exists(
            os.path.join(shell_tools.PROJECT_DIR, "should-not-exist.txt"))
        permissions.set_current_gate(
            ApprovalGate(mode="edit", confirm=lambda a: True))
        out = shell_tools.run_shell("echo x > created.txt")
        assert "退出码 0" in out, out
        assert os.path.exists(
            os.path.join(shell_tools.PROJECT_DIR, "created.txt"))
    print("[OK] shell：写命令确认通过才跑，拒绝就不跑")


def test_shell_readonly_blocks():
    with _Ctx():
        permissions.set_current_gate(ApprovalGate(mode="readonly"))
        out = shell_tools.run_shell("echo hi")
        assert "未执行" in out and "只读" in out, out
    print("[OK] shell：只读模式连读命令也不跑")


def test_shell_timeout():
    with _Ctx():
        # 跨平台睡 5 秒：cmd.exe 没有 sleep 命令，用当前 Python 解释器睡。
        sleep_cmd = f'"{sys.executable}" -c "import time; time.sleep(5)"'
        out = shell_tools.run_shell(sleep_cmd, timeout=0.2)
        assert "超时" in out, out
    print("[OK] shell：超时返回提示并终止")


def test_shell_truncates_long_output():
    with _Ctx():
        out = shell_tools.run_shell(
            f'"{sys.executable}" -c "print(\'x\' * 10000)"')
        assert "省略" in out, out
        assert len(out) < 6000, len(out)
    print("[OK] shell：超长输出被截断")


def test_shell_env_has_no_key():
    with _Ctx():
        os.environ["DEEPSEEK_API_KEY"] = "sk-should-not-leak"
        try:
            out = shell_tools.run_shell(
                f'"{sys.executable}" -c "import os; print(\'DEEPSEEK_API_KEY\' in os.environ)"')
        finally:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        assert "False" in out, out
    print("[OK] shell：子进程看不到 DEEPSEEK_API_KEY")


def test_write_diff_confirm():
    with _Ctx() as ctx:
        seen = {}
        permissions.set_current_gate(ApprovalGate(
            mode="edit",
            confirm=lambda a: seen.setdefault("diff", a.get("detail")) or True))
        msg = tools.write_text_file("note.txt", "hello\nworld\n")
        assert "已写入" in msg, msg
        assert "hello" in (seen.get("diff") or ""), seen
        # 拒绝后文件不动
        permissions.set_current_gate(
            ApprovalGate(mode="edit", confirm=lambda a: False))
        msg = tools.write_text_file("note.txt", "changed\n")
        assert "未写入" in msg, msg
        assert tools.read_text_file("note.txt") == "hello\nworld\n"
        # 只读模式不写
        permissions.set_current_gate(ApprovalGate(mode="readonly"))
        msg = tools.write_text_file("note.txt", "changed\n")
        assert "只读" in msg, msg
        rows = AuditLog(ctx.audit_path).recent()
        writes = [r for r in rows if r["action"] == "write_file"]
        assert writes and all("hello" not in r.get("detail", "") for r in writes), writes
    print("[OK] 写文件：确认框能看到 diff；拒绝/只读后文件不动；审计不记内容")


if __name__ == "__main__":
    test_shell_read_command()
    test_shell_dangerous_blocked()
    test_shell_write_needs_confirm()
    test_shell_readonly_blocks()
    test_shell_timeout()
    test_shell_truncates_long_output()
    test_shell_env_has_no_key()
    test_write_diff_confirm()
    print("全部通过。")
