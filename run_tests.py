"""一键跑全部测试：python run_tests.py

按顺序跑 tests/ 下所有 test_*.py，每个文件都是独立脚本（不用 key、
不联网，用假 client / 本地 HTTP 服务 / 假 MCP 服务模拟）。任何一个
失败就停下并报非零退出码，CI 里也用它。

测试单独跑也可以，但要从项目根目录加 PYTHONPATH，例如：
    PYTHONPATH=. python tests/test_shell.py
"""

import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS_DIR = os.path.join(HERE, "tests")


def main() -> int:
    tests = sorted(glob.glob(os.path.join(TESTS_DIR, "test_*.py")))
    if not tests:
        print("没有找到测试。")
        return 1
    env = dict(os.environ)
    # Windows 控制台默认 GBK（cp936），测试打印的 ✓ / ☐ / → 等字符会
    # 报 UnicodeEncodeError（2026-11-22 真机：test_gui.py 在这一项上
    # 崩）。强制子进程用 UTF-8 输出，与沙箱行为一致。
    env["PYTHONUTF8"] = "1"
    # 不缓冲子进程输出：某个测试崩掉（段错误）时，缓冲里的 [OK] 行会
    # 跟着进程一起丢，查不到崩在哪一项。强制不缓冲，崩点可见。
    env["PYTHONUNBUFFERED"] = "1"
    # 子进程要能 import jarvis.*（项目根目录）和 test_loop 这类
    # 测试间互相 import 的模块（tests/ 目录）。
    pythonpath = os.pathsep.join(
        [HERE, TESTS_DIR] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    env["PYTHONPATH"] = pythonpath
    qt_libs = "/tmp/qtlibs/usr/lib/x86_64-linux-gnu"
    env["LD_LIBRARY_PATH"] = (
        qt_libs + os.pathsep + env["LD_LIBRARY_PATH"]
        if env.get("LD_LIBRARY_PATH") else qt_libs)
    failed = []
    for path in tests:
        name = os.path.basename(path)
        print(f"\n=== {name} ===")
        proc = subprocess.run(
            [sys.executable, path], cwd=HERE, env=env)
        if proc.returncode != 0:
            failed.append(name)
            print(f"!!! {name} 失败（退出码 {proc.returncode}），停下。")
            break
    if failed:
        print(f"\n失败：{', '.join(failed)}")
        return 1
    print(f"\n全部通过：{len(tests)} 个测试文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
