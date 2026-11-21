"""一键跑全部测试：python run_tests.py

按顺序跑所有 test_*.py，每个文件都是独立脚本（不用 key、不联网，
用假 client / 本地 HTTP 服务 / 假 MCP 服务模拟）。任何一个失败就
停下并报非零退出码，CI 里也用它。
"""

import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    tests = sorted(glob.glob(os.path.join(HERE, "test_*.py")))
    if not tests:
        print("没有找到测试。")
        return 1
    env = dict(os.environ)
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
