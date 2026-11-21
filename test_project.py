"""P2 测试：精确编辑、搜索、git 工具、项目规则、网页搜索。

用临时目录当 PROJECT_DIR，绝不碰真实项目。
验证：
  1. edit_file：唯一匹配替换成功；找不到不写；多处匹配不写
  2. glob_files：按 pattern 找到文件，跳过 .git 等目录
  3. grep_files：返回“文件:行号: 内容”，正则错误有提示
  4. git 工具：临时 git 仓库里 status/diff/log/commit 都正常
  5. JARVIS.md：全局 + 项目级叠加读；AGENTS.md 也兼容
  6. 项目索引：跳过隐藏目录，有数量上限
  7. web_search：本地假服务返回结果页，解析出标题+链接
"""

import http.server
import os
import subprocess
import tempfile
import threading

import project
import tools


def _setup_tmp_project(files):
    tmp = tempfile.mkdtemp()
    for rel, content in files.items():
        full = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
    return tmp


class _Ctx:
    def __init__(self, files):
        self.tmp = _setup_tmp_project(files)
        self.old = tools.PROJECT_DIR

    def __enter__(self):
        tools.PROJECT_DIR = self.tmp
        return self.tmp

    def __exit__(self, *exc):
        tools.PROJECT_DIR = self.old
        return False


def test_edit_file():
    files = {"a.py": "x = 1\ny = 2\nx = 1\n"}
    with _Ctx(files):
        # 多处匹配：不写
        msg = tools.edit_file("a.py", "x = 1", "x = 10")
        assert "2 处" in msg, msg
        # 找不到：不写
        msg = tools.edit_file("a.py", "zzz = 9", "zzz = 10")
        assert "找不到" in msg, msg
        # 唯一匹配：写成功
        msg = tools.edit_file("a.py", "y = 2", "y = 20")
        assert "已写入" in msg, msg
        assert tools.read_text_file("a.py") == "x = 1\ny = 20\nx = 1\n"
    print("[OK] 精确编辑：唯一替换成功，多处/找不到都不写")


def test_glob_grep():
    files = {
        "src/main.py": "def run_agent():\n    pass\n",
        "src/util.py": "def helper():\n    pass\n",
        "src/main_test.py": "def test_x():\n    pass\n",
        "notes/todo.txt": "run_agent 待优化\n",
    }
    with _Ctx(files):
        got = tools.glob_files("src/*.py")
        assert "src/main.py" in got and "src/util.py" in got, got
        assert "notes/todo.txt" not in got, got
        got = tools.glob_files("**/*.py")
        assert "src/main.py" in got, got
        hits = tools.grep_files("def run_agent")
        assert "src/main.py:1:" in hits, hits
        hits = tools.grep_files("run_agent", path="notes")
        assert "notes/todo.txt:1:" in hits, hits
        bad = tools.grep_files("[invalid")
        assert "正则" in bad, bad
    print("[OK] 搜索：glob 找文件，grep 返回文件:行号:内容")


def _git(*args, cwd):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    # 显式 UTF-8：git 输出里可能有中文路径（用户名），默认按 GBK 解会炸。
    return subprocess.run(["git"] + list(args), cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=30,
                          encoding="utf-8", errors="replace")


def test_git_tools():
    with _Ctx({"hello.py": "print('hi')\n"}) as tmp:
        assert _git("init", cwd=tmp).returncode == 0
        assert _git("config", "user.email", "t@t", cwd=tmp).returncode == 0
        assert _git("config", "user.name", "t", cwd=tmp).returncode == 0
        assert _git("add", ".", cwd=tmp).returncode == 0
        assert _git("commit", "-m", "init", cwd=tmp).returncode == 0
        assert "init" in tools.git_log(), tools.git_log()
        assert tools.git_status() in ("", "(没有输出)"), tools.git_status()
        # 改一个文件：status/diff 能看到
        with open(os.path.join(tmp, "hello.py"), "a") as f:
            f.write("print('bye')\n")
        assert "hello.py" in tools.git_status(), tools.git_status()
        assert "bye" in tools.git_diff(), tools.git_diff()
        # git_commit：确认后提交
        import permissions
        permissions.set_current_gate(
            permissions.ApprovalGate(mode="edit", confirm=lambda a: True))
        try:
            out = tools.git_commit("second", paths=["hello.py"])
        finally:
            permissions.set_current_gate(None)
        assert "second" in tools.git_log(), out
    print("[OK] git 工具：status/diff/log/commit 都正常")


def test_project_rules_and_index():
    tmp = tempfile.mkdtemp()
    home = tempfile.mkdtemp()
    os.makedirs(os.path.join(home, ".jarvis"), exist_ok=True)
    with open(os.path.join(home, ".jarvis", "JARVIS.md"), "w", encoding="utf-8") as f:
        f.write("全局：回答用中文。")
    with open(os.path.join(tmp, "JARVIS.md"), "w", encoding="utf-8") as f:
        f.write("项目：跑测试用 python test_loop.py。")
    with open(os.path.join(tmp, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write("项目：不要删文件。")
    os.makedirs(os.path.join(tmp, ".git"), exist_ok=True)
    with open(os.path.join(tmp, ".git", "config"), "w") as f:
        f.write("x")
    os.makedirs(os.path.join(tmp, "src"), exist_ok=True)
    with open(os.path.join(tmp, "src", "a.py"), "w") as f:
        f.write("x = 1\n")
    old_home = os.environ.get("HOME")
    old_profile = os.environ.get("USERPROFILE")
    os.environ["HOME"] = home
    # Windows 上 expanduser("~") 看 USERPROFILE 不看 HOME，两个都得换。
    os.environ["USERPROFILE"] = home
    try:
        rules = project.load_project_rules(tmp)
        assert "全局：回答用中文" in rules, rules
        assert "跑测试用" in rules, rules
        assert "不要删文件" in rules, rules  # AGENTS.md 也兼容
        index = project.build_project_index(tmp)
        assert "src/" in index and "a.py" in index, index
        assert ".git" not in index, index
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        if old_profile is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = old_profile
    print("[OK] 项目规则：全局+项目级叠加，AGENTS.md 兼容；索引跳过 .git")


class _DDGHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = ('<html><body>'
                '<a class="result__a" href="https://example.com/a">第一条结果</a>'
                '<a class="result__a" href="https://example.com/b">第二条结果</a>'
                '</body></html>').encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_web_search_local():
    server = http.server.HTTPServer(("127.0.0.1", 0), _DDGHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        out = tools.web_search("test query", base_url=f"http://127.0.0.1:{port}")
        assert "第一条结果" in out and "https://example.com/a" in out, out
        assert "第二条结果" in out, out
    finally:
        server.shutdown()
    print("[OK] 网页搜索：本地假服务解析出标题+链接")


if __name__ == "__main__":
    test_edit_file()
    test_glob_grep()
    test_git_tools()
    test_project_rules_and_index()
    test_web_search_local()
    print("全部通过。")
