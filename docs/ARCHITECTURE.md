# Jarvis 架构说明（0.2.0，分包后）

## 目录结构

```
python-version/
  jarvis/                  # 全部源码，一个包
    __init__.py            # PROJECT_ROOT（只在这里算一次）+ __version__ 重导出
    core/                  # 底座：不依赖任何其他包
    safety/                # 安全：只依赖 core
    tools/                 # 工具：依赖 core + safety
    memory/                # 记忆：依赖 core（+ tools 的写前钩子注册表）
    runtime/               # 运行：依赖 core + tools + safety
    extensions/            # 扩展：依赖 core + tools + safety
    ui/                    # 界面：依赖上面所有包
  skills/                  # 内置技能（数据目录，不在包里）
  plugins/                 # 插件（数据目录，不在包里）
  docs/                    # 文档（含本文件）
  api/                     # Vercel 入口
  tests/                   # 19 个 test_*.py + test_fixtures/ + smoke_test.py
  run_tests.py  pyproject.toml  jarvis.spec  build-exe.ps1  ...
```

## 分层图

```
┌─────────────────────────────────────────────────┐
│ ui        chat / gui / worker / server / md /   │
│           trajectory / update                   │
├─────────────────────────────────────────────────┤
│ extensions  skills / mcp_client / hooks /       │
│             plugins                             │
│ runtime     subagent / jobs                     │
├─────────────────────────────────────────────────┤
│ memory      memory / session_store / compaction │
│             todo / checkpoint / planner         │
│ tools       tools / shell_tools / project       │
├─────────────────────────────────────────────────┤
│ safety      permissions / policy / audit /      │
│             diffutil                            │
├─────────────────────────────────────────────────┤
│ core        config / model / agent / usage /    │
│             jlog                                │
└─────────────────────────────────────────────────┘
```

依赖方向只能从上往下：`ui → extensions / runtime → memory / tools → safety → core`。
上层 import 下层，下层绝不 import 上层。

## 每包模块清单

### core（底座）
| 模块 | 干什么 |
| --- | --- |
| `config.py` | 统一配置（环境变量 > `~/.jarvis/config.json` > 默认值）+ `__version__ = "0.2.0"` |
| `model.py` | 调 DeepSeek：重试（429/超时/5xx 指数退避）、流式、用量估算 |
| `agent.py` | agent loop 本体：发消息 → 模型回答或要求调工具 → 执行 → 喂回结果 → 循环 |
| `usage.py` | token/费用估算、按天汇总 |
| `jlog.py` | 结构化日志（一行一个 JSON 事件，只记摘要） |

### safety（安全）
| 模块 | 干什么 |
| --- | --- |
| `permissions.py` | 权限三档 readonly / edit / auto + 确认门 |
| `policy.py` | 危险命令黑名单、读写分类、工作区逃逸检查 |
| `audit.py` | 审计日志 `~/.jarvis/audit.jsonl`（只记动作和结果） |
| `diffutil.py` | 写文件前的 unified diff |

### tools（工具）
| 模块 | 干什么 |
| --- | --- |
| `tools.py` | 基础工具：时间、计算器、读/写/查文件、取网页、Git、搜索；文件沙箱根 `PROJECT_DIR` |
| `shell_tools.py` | `run_shell`（策略检查 → 确认 → 超时 → 输出截断 → 审计） |
| `project.py` | 项目规则（`JARVIS.md` / `AGENTS.md` / `CLAUDE.md`）+ 项目目录索引 |

### memory（记忆）
| 模块 | 干什么 |
| --- | --- |
| `memory.py` | 短期记忆（整轮裁剪）+ 长期记忆 `FactStore`（默认 `memory.json`） |
| `session_store.py` | 会话持久化 `~/.jarvis/sessions/*.jsonl` |
| `compaction.py` | 上下文超阈值自动压缩成摘要（整轮保留，工具链不断） |
| `todo.py` | 待办清单 |
| `checkpoint.py` | 文件检查点（写前备份 + `/rewind` 恢复） |
| `planner.py` | 规划模式：调研出计划 → 确认 → 执行 → 对照验收标准自查 |

### runtime（运行）
| 模块 | 干什么 |
| --- | --- |
| `subagent.py` | 子代理委派（`delegate` 工具，一层） |
| `jobs.py` | 后台任务（启动/查状态/取结果/超时杀整棵进程树） |

### extensions（扩展）
| 模块 | 干什么 |
| --- | --- |
| `skills.py` | 技能加载（项目 `skills/` + `~/.jarvis/skills/`） |
| `mcp_client.py` | MCP 客户端（标准库 JSON-RPC over stdio） |
| `hooks.py` | Hooks（`before_write` / `before_shell` / `on_session_end`） |
| `plugins.py` | 插件加载（`plugins/` 下每个 `.py` 定义 `TOOL_SCHEMA` + `TOOL_FUNC`） |

### ui（界面）
| 模块 | 干什么 |
| --- | --- |
| `chat.py` | 命令行入口（REPL + 一次问答 + 会话/用量/轨迹/更新命令） |
| `gui.py` | 桌面 GUI（PySide6） |
| `worker.py` | GUI 后台线程（QThread + 信号，绝不碰界面控件） |
| `server.py` | 网页版后端（FastAPI + SSE，无状态，BYOK） |
| `md.py` | 极简 Markdown → ANSI / HTML |
| `trajectory.py` | 轨迹回放（会话时间线） |
| `update.py` | 自更新检查（只检查 GitHub releases，不安装） |

## import 规则

- 一律绝对导入（`from jarvis.core.config import ...`），不要相对导入。
- 唯一例外：`core/agent.py` 在文件末尾才 `from jarvis.tools.tools import TOOLS, TOOL_FUNCS`。
  这是有意的——agent 循环要读工具注册表，但工具层在依赖方向上比 core 高；
  放末尾是为了不形成 import 时的循环（`tools.py` 同样在 `TOOLS` 定义后才
  `from jarvis.tools.shell_tools import run_shell`）。不要把这两处挪到文件头。
- 函数内延迟导入只在两种情况下用：打破循环（`compaction.py` 调模型做摘要、
  `planner.py` 跑只读循环出计划、`model.py` 估算 token），其余一律放文件头。

## PROJECT_ROOT（最关键的一条）

项目根目录（`python-version/`）只在 `jarvis/__init__.py` 里算一次：

```python
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
```

以下五处一律从它派生，绝不许各自用 `__file__` 现算——模块搬进子包后
`__file__` 变了，这些路径必须继续指向项目根目录：

| 位置 | 派生方式 |
| --- | --- |
| `tools/tools.py` 的 `PROJECT_DIR`（文件沙箱根） | `PROJECT_DIR = PROJECT_ROOT` |
| `tools/shell_tools.py` 的 `PROJECT_DIR`（shell 的 cwd） | `PROJECT_DIR = PROJECT_ROOT` |
| `extensions/skills.py` 的 `SHIPPED_SKILLS_DIR` | `os.path.join(PROJECT_ROOT, "skills")` |
| `extensions/plugins.py` 的 `PLUGINS_DIR` | `os.path.join(PROJECT_ROOT, "plugins")` |
| `memory/memory.py` 的 `FactStore` 默认路径 | `os.path.join(PROJECT_ROOT, "memory.json")` |

用户级目录（`~/.jarvis`：会话、审计、用量、配置）走 `config.default_config_dir()`，
不受分包影响。

## 测试

- 测试全在 `tests/`：19 个 `test_*.py`（117 项检查）+ `test_fixtures/`（假 MCP 服务器）
  + `smoke_test.py`（只用真 key 的冒烟测试，不进全量测试）。
- 跑法：项目根目录下 `python run_tests.py`。它会把项目根目录和 `tests/` 都
  加进子进程的 `PYTHONPATH`（测试要 `import jarvis.*`，测试之间也要互相
  `import test_loop` / `test_stream`）。单个测试文件直接跑会报
  `No module named 'jarvis'`，要加 `PYTHONPATH=.`，例如
  `PYTHONPATH=. python tests/test_shell.py`。
- `tests/` 不是包（没有 `__init__.py`），打包时不包含。

## 入口与打包

| 入口 | 命令 |
| --- | --- |
| 命令行 | `python -m jarvis.ui.chat`（或装完后 `jarvis`） |
| 桌面 | `python -m jarvis.ui.gui`（或双击 `dist/jarvis-gui.exe`） |
| 网页 | `uvicorn jarvis.ui.server:app` |
| Vercel | `api/index.py` 从 `jarvis.ui.server` import app |
| 打包 | `jarvis.spec` 两个 Analysis 入口是 `jarvis/ui/chat.py` 与 `jarvis/ui/gui.py`，`pathex=[SPECPATH]`；CI 用 `python -m PyInstaller`（裸 `pyinstaller` 命令依赖 Scripts 在 PATH 里，不可靠） |
| 安装 | `pip install -e .`，`pyproject.toml` 用 `packages.find(include=["jarvis*"])`，入口 `jarvis = "jarvis.ui.chat:main"` |
