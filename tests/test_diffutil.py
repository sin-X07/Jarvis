"""Diff 解析与分组渲染的离线测试：不用 key、不联网。

验证：
  1. parse_diff：git diff 与 make_diff 两种格式都按文件拆开，
     每文件 +N/-M 计数正确，元数据行不进 lines
  2. render_diff_grouped：每文件一个小节头 + 行；空 diff 给提示
  3. working_tree_diff：临时 git 仓库里已改文件 + 未跟踪文件都出现，
     非 git 目录直说

Run:  python test_diffutil.py
"""

import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis.safety.diffutil import make_diff, parse_diff, render_diff_grouped
from jarvis.tools import tools

ORIGINAL_DIR = tools.PROJECT_DIR

GIT_DIFF = """diff --git a/a.py b/a.py
index 1111111..2222222 100644
--- a/a.py
+++ b/a.py
@@ -1,3 +1,3 @@
 line1
-old
+new
 line3
diff --git a/b.py b/b.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/b.py
@@ -0,0 +1,2 @@
+hello
+world
"""


def _git(tmp, *args):
    subprocess.run(["git", "-C", tmp, *args], check=True,
                   capture_output=True, text=True, timeout=15)


def test_parse_diff_git():
    files = parse_diff(GIT_DIFF)
    assert [f["path"] for f in files] == ["a.py", "b.py"], files
    assert files[0]["added"] == 1 and files[0]["removed"] == 1, files
    assert files[0]["summary"] == "+1 行 / -1 行", files
    assert files[1]["added"] == 2 and files[1]["removed"] == 0, files
    # 元数据行不进 lines，块头与内容行在
    assert all(not l.startswith(("diff --git", "index ", "--- ", "+++ "))
               for f in files for l in f["lines"]), files
    assert files[0]["lines"][0].startswith("@@"), files
    assert "+new" in files[0]["lines"] and "-old" in files[0]["lines"], files
    print("[OK] Diff 解析：git diff 按文件拆开，计数与小节头正确")


def test_parse_diff_make_diff():
    diff = make_diff("one\ntwo\n", "one\nTWO\nthree\n", "c.py")
    files = parse_diff(diff)
    assert len(files) == 1 and files[0]["path"] == "c.py", files
    assert files[0]["added"] == 2 and files[0]["removed"] == 1, files
    assert "还没有改动" not in render_diff_grouped(diff), diff
    print("[OK] Diff 解析：make_diff 单文件格式也能拆")


def test_render_diff_grouped():
    text = render_diff_grouped(GIT_DIFF)
    assert "=== a.py（+1 行 / -1 行）===" in text, text
    assert "=== b.py（+2 行 / -0 行）===" in text, text
    assert render_diff_grouped("") == "还没有改动。"
    assert render_diff_grouped("当前目录不是一个 git 仓库。") == \
        "当前目录不是一个 git 仓库。"
    print("[OK] Diff 分组渲染：小节头 + 行；空 diff 与提示文本原样返回")


def test_working_tree_diff_temp_repo():
    with tempfile.TemporaryDirectory() as tmp:
        _git(tmp, "init", "-q")
        _git(tmp, "-c", "user.email=t@t", "-c", "user.name=t",
             "config", "user.email", "t@t")
        with open(os.path.join(tmp, "a.py"), "w", encoding="utf-8") as f:
            f.write("line1\nold\nline3\n")
        _git(tmp, "add", ".")
        _git(tmp, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-qm", "init")
        with open(os.path.join(tmp, "a.py"), "w", encoding="utf-8") as f:
            f.write("line1\nnew\nline3\n")
        with open(os.path.join(tmp, "b.py"), "w", encoding="utf-8") as f:
            f.write("hello\nworld\n")
        tools.PROJECT_DIR = tmp
        try:
            diff = tools.working_tree_diff()
            files = parse_diff(diff)
        finally:
            tools.PROJECT_DIR = ORIGINAL_DIR
        assert [f["path"] for f in files] == ["a.py", "b.py"], files
        assert files[0]["added"] == 1 and files[0]["removed"] == 1, files
        assert files[1]["added"] == 2 and files[1]["removed"] == 0, files
        print("[OK] 工作区 Diff：已改文件 + 未跟踪文件都在，分组正确")


def test_working_tree_diff_not_a_repo():
    with tempfile.TemporaryDirectory() as tmp:
        tools.PROJECT_DIR = tmp
        try:
            assert tools.working_tree_diff() == "当前目录不是一个 git 仓库。"
        finally:
            tools.PROJECT_DIR = ORIGINAL_DIR
    print("[OK] 工作区 Diff：非 git 目录直说")


if __name__ == "__main__":
    test_parse_diff_git()
    test_parse_diff_make_diff()
    test_render_diff_grouped()
    test_working_tree_diff_temp_repo()
    test_working_tree_diff_not_a_repo()
    print("全部通过。")
