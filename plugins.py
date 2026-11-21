"""自定义工具插件（P6-5）：放一个 .py 文件就能加一个工具，不用改 tools.py。

约定：plugins/ 目录下每个 .py 文件定义两个东西——
  TOOL_SCHEMA：OpenAI function-calling 格式的 schema（dict）；
  TOOL_FUNC：真正干活的函数（callable）。
加载时按文件名排序，名字冲突时后面的覆盖前面的并打印警告。

插件在独立模块里跑，import 失败的文件会被跳过（记在 skipped 里），
不影响别的插件和主流程。插件也能访问 tools.py 的 PROJECT_DIR
沙箱——想读文件用 tools.read_text_file，不要自己 open 任意路径。
"""

import importlib.util
import os

PLUGINS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "plugins")


def load_plugin_tools(directory: str | None = None):
    """加载一个目录下的全部插件，返回 (schemas, funcs, skipped)。

    skipped = [(文件名, 原因)]，加载失败的插件都在这里。
    """
    directory = directory or PLUGINS_DIR
    schemas, funcs, skipped = [], {}, []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return schemas, funcs, skipped
    for filename in names:
        if not filename.endswith(".py") or filename.startswith("_"):
            continue
        path = os.path.join(directory, filename)
        module_name = "_jarvis_plugin_" + filename[:-3]
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                skipped.append((filename, "加载失败"))
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            schema = getattr(module, "TOOL_SCHEMA", None)
            func = getattr(module, "TOOL_FUNC", None)
            if not isinstance(schema, dict) or not callable(func):
                skipped.append((filename, "缺少 TOOL_SCHEMA 或 TOOL_FUNC"))
                continue
            name = schema.get("function", {}).get("name", "")
            if not name:
                skipped.append((filename, "schema 里没有工具名"))
                continue
            if name in funcs:
                print(f"警告：插件工具名冲突，{filename} 覆盖了已有的 {name}")
            schemas.append(schema)
            funcs[name] = func
        except Exception as exc:  # noqa: BLE001 - 插件炸了只跳过这个文件
            skipped.append((filename, f"{type(exc).__name__}: {exc}"))
    return schemas, funcs, skipped
