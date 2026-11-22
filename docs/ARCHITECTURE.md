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
| `trajectory.py` | 轨迹回放（会话时间线；`iter_turns` 分轮、`replay_transcript` 重放） |
| `update.py` | 自更新检查（只检查 GitHub releases，不安装） |
| `events.py` | 事件模型（U0）：11 种 UI 事件表 + 工具摘要注册表 + 风险分级 |
| `tui.py` | 全屏 TUI（U2，Textual）：四层布局 + 侧栏 Tab + 确认单键 + 补全 |
| `theme.py` / `theme.json` | 主题（U0）：单一事实来源，生成 CSS 变量 / QSS / ANSI 角色表 / Textual CSS |

## import 规则

- 一律绝对导入（`from jarvis.core.config import ...`），不要相对导入。
- 唯一例外：`core/agent.py` 在文件末尾才 `from jarvis.tools.tools import TOOLS, TOOL_FUNCS`。
  这是有意的——agent 循环要读工具注册表，但工具层在依赖方向上比 core 高；
  放末尾是为了不形成 import 时的循环（`tools.py` 同样在 `TOOLS` 定义后才
  `from jarvis.tools.shell_tools import run_shell`）。不要把这两处挪到文件头。
- 函数内延迟导入只在两种情况下用：打破循环（`compaction.py` 调模型做摘要、
  `planner.py` 跑只读循环出计划），其余一律放文件头。

## 事件模型（U0，UI 优化底座）

`run_agent` 的 `on_event(kind, payload)` 只发 `jarvis/ui/events.py`
里 `EVENT_KINDS` 的 11 种事件；三个界面（网页 / TUI / 桌面）只订阅
这张表，不再直接读 agent 内部状态。核心发
`text_delta` / `thinking_delta` / `tool_start` / `tool_end` /
`status` / `turn_end` / `error`；`todo_set` / `plan_proposed` 由
计划流程发，`diff_proposed` 由 Diff 流程发，`approval_request` 由
界面确认回调发。

两套词别混：UI 事件是瞬时的（渲染用）；落盘的是会话 JSONL
（`session_store.py`），只记 user / assistant / tool_call /
tool_result / summary / todo / plan / error。`trajectory.py` 的
`replay_transcript` 把 JSONL 重放成界面条目（断线重连、恢复会话用）。

工具卡片上的一行摘要不是原始 JSON，是
`events.summarise_tool_call(name, args)`——每个内置工具都在注册表里
（25 个全覆盖 + `mcp__` 前缀 + 未知工具兜底）；`tool_risk(name)`
给卡片定颜色（read 中性 / write 琥珀 / danger 红，未知工具保守算 write）。

主题：`jarvis/ui/theme.json`（dark）是唯一事实来源；
`theme.py` 的 `to_css_vars` / `to_qss` / `to_ansi_roles` 生成三份产物。
桌面 QSS 由生成器出，视觉与旧手写 `DARK_STYLE` 一致（`test_events.py`
里有逐字比对）；三个界面不许再出现硬编码颜色。

用量（U0-5）：token 数全部来自 API 返回的真 usage。流式传
`stream_options={"include_usage": True}`，最后一个 chunk 带整次请求的
usage；拿不到时记 unknown，界面显示"—"，绝不估算。`estimate_tokens`
只供 compaction 内部触发判断用，不进入统计、不上界面。

## 全屏 TUI（U2，`jarvis/ui/tui.py`，Textual）

入口 `python -m jarvis.ui.tui`；`chat.py` 保留，是非 TTY / 脚本化入口
（`jarvis` console script 仍指 `chat:main`）。依赖在
`requirements.txt`（`textual>=8.0,<9`，TUI 是 CLI 的一部分）。

布局：顶栏（模型 · 档位 · 会话）/ 中间（transcript + 可拖分隔条 +
侧栏 Tab：会话 / 待办 / Diff / 检查点）/ 输入框（多行自增高 1–6 行）
/ 状态栏（真 token + context 条 + 分支 + 未返回用量次数）。
`<80` 列自动收起侧栏，`≥80` 恢复；`Ctrl+B` 手动开关。

线程模型：agent 循环跑在 Textual 工作线程（`run_worker(...,
thread=True)`），事件经 `call_from_thread` 回 UI 线程渲染。
Stop 不杀线程——事件回调里 `self._stop_requested` 为真时在下一个
token / 思考 / 工具事件处抛 `_StopRequested`，气泡保留已出文字并
标记"■ 已停止"。确认门是 `ApprovalGate(mode, confirm=self._confirm)`：
工作线程在 `threading.Event` 上等，UI 里挂一张内嵌 `ConfirmCard`，
单键 `a` 允许一次 / `A` 本会话总是允许 / `d` 拒绝 / `e` 拒绝并把
命令填回输入框。`e` 只填不重跑——gate 接口只返回 bool，改完要用户
自己按回车。

输入框：`/` 命令补全 + `@文件` 补全（弹窗，上下键选，回车补全；
输入已是完整命令名时回车直接执行）；`!` 开头走 shell escape
（只显示输出）；忙时回车排队为"督导"、Tab 排队追问，轮次结束自动发；
`Shift+Enter` 换行。`?` 看快捷键总览。

状态：context 条取最近一次真实 `prompt_tokens` / 1M；
`status` 事件的用量进 `_call_log`（`/context` 看明细）。
长 transcript 超 400 个组件时最老的 50 个折成一条提示
（消息仍在会话 JSONL 里）。

`/setup` 向导把 key 写进项目 `.env`（`save_key_to_dotenv`，有就改行
没有就追加）、模型和档位进 `~/.jarvis/config.json`，保存后重建 client。
`/theme` 在 `jarvis/ui/*.json` 里循环切换（dark / light），用
`_apply_theme` 整份重贴主题 CSS + `refresh_css()` 立即生效，并存
`~/.jarvis/ui.json`（U6-1 修过两次：`screen.set_styles()` 拒收顶层
`$变量` 声明；`stylesheet.set_variables()` 是全量替换，会抹掉
Textual 内置变量，且已算好的样式不会重算——见 U6 章节）。`/plan`、
Diff 接受/拒绝对照 U4/U5 章节（U2 时是占位，U4/U5 已接线）。每轮写文件前自动快照（`register_before_write_hook`，
文件复制兜底；git 版在 U5）——注意：chat.py / GUI 里这条线目前没接，
同一缺口，U5 统一补。

测试：`tests/test_tui.py`（17 项）用 Textual pilot 在 headless 下跑真
App：80×24 / 120×40 启动截图（`/tmp/tui-*.svg`）、流式进气泡、
工具卡片 + Ctrl+O、Esc 停止、确认单键、错误卡片 + r 重试、Tab 排队、
真打字、`/help` / `/setup` / `/context`、非 TTY fallback。
Windows Terminal 下的表现要在真机验（U2-1）。

## 桌面 GUI（U3，`jarvis/ui/gui.py`，PySide6）

入口 `python -m jarvis.ui.gui`。聊天区是一串组件（QScrollArea +
QVBoxLayout），不再是一个 QTextEdit：`MessageBubble`（气泡 +
QTextBrowser 正文 + 悬停工具栏：复制 / 重新生成）、`ToolCardWidget`
（工具卡片：标题行是摘要 + 状态 + 耗时，点开看参数和输出）、
`ThinkingWidget`（思考过程折叠）、`ConfirmCardWidget`（内嵌确认卡，
四键：允许一次 / 总是允许 / 拒绝 / 编辑，不再弹 QMessageBox）、
`ErrorCardWidget`（错误卡：重试只给一次，第二次改“复制问题，
开新会话”，U6-4）、`NoteWidget`（居中系统提示）。
右侧是 QTabWidget（会话 / 待办 / Diff / 检查点），顶栏是模型芯片 +
档位芯片（点一下 readonly → edit → auto，立即落盘）+ “📋 计划”开关，
底部状态行（context 条 + 真 token + 费用 + 未返回用量次数）。输入框是
`ComposerEdit`（QTextEdit，多行自增高 1–6 行，Enter 发送 /
Shift+Enter 换行）；发送中按钮变 Stop，忙时回车排队（FIFO，
轮次结束自动发）。

流式：token 逐个 `append_text` 进当前气泡，不整段重渲染（不闪烁）；
工具一来就断开当前气泡，卡片按流顺序插进来；轮次结束
`_finalize_bubbles` 只把含 Markdown 构造的气泡换渲染版（纯文本
不换）。代码块：`_html_with_code_headers` 给 `to_html` 出的代码块
加语言标签 + “复制”锚点（`copy-code:N`，代码原文进剪贴板，含
围栏前的末尾换行；`md.py` 的 `to_html` 已按 CommonMark 补上），
`<pre>` 背景用 `codeBg` token。气泡高度由 `_fit()` 按文档高度定：
`setHtml` 后文档布局是懒的，立刻量会坍到 1 行，所以先
`doc.setTextWidth(viewport 宽)` 强制当场排版再量。

线程模型：每轮一个 `AgentWorker` + `QThread`（`worker.py` 的信号
直通事件：token / thinking / tool_started / tool_finished /
status / confirm_requested / finished / stopped / error）。
**收尾是显式同步的**：`_on_finished` / `_on_stopped` / `_on_error`
里先 `_stop_turn_thread()`（`quit()` + `wait(3000)`，确认线程
停下再丢引用），再 `_drain_queue()`（排队的下一轮）。不要接
worker / thread 的 `deleteLater`——Qt 删了 C++ 对象后，MainWindow
手里的 Python 包装再被释放时 Shiboken 会二次删除，曾导致段错误
（野指针）与 “QThread destroyed while running” abort；`closeEvent`
也走 `_stop_turn_thread()`。测试侧同样：`_pump_until` 用
`processEvents` 轮询（不用嵌套 QEventLoop / QTimer——局部循环对象
在函数返回后还可能被事件派发器碰到），worker 测试里 `quit()` 要
显式叫（`wait()` 不处理事件，等信号里的 quit 排队会死锁 3 秒），
跑完的 worker/thread 全程保活，窗口测试关窗后 pump + gc 再测下一项
（MainWindow 销毁攒下的 Shiboken 延迟删除要就地处理，别攒到后面的
pump）。

窗口尺寸与分隔条位置记在 config（`window_width` / `window_height` /
`window_x` / `window_y` / `splitter_sizes`），关窗时保存。设置对话框
加主题下拉（`jarvis/ui/*.json`，dark / light）与 key 字段（密码框，
写进项目 `.env`，只存在本机）；没 key 启动时自动弹首次启动向导
（U6-3，`_open_first_run_wizard`，QTimer 延时 200ms 弹）。
QSS 从 `theme.json` 生成（`to_qss`，U0 的生成器）；芯片 / 卡片 /
工具栏等组件样式由 `_component_style()` 按 token 现拼，不在
`to_qss` 里，避免改动 U0 的 QSS==旧 DARK_STYLE 回归断言。

窗口尺寸与分隔条位置记在 config（`window_width` / `window_height` /
`window_x` / `window_y` / `splitter_sizes`），关窗时保存。设置对话框
加主题下拉（`jarvis/ui/*.json`，dark / light）与 key 字段（密码框，
写进项目 `.env`，只存在本机）；没 key 启动时自动弹首次启动向导
（U6-3，`_open_first_run_wizard`，QTimer 延时 200ms 弹）。
QSS 从 `theme.json` 生成（`to_qss`，U0 的生成器）；芯片 / 卡片 /
工具栏等组件样式由 `_component_style()` 按 token 现拼，不在
`to_qss` 里，避免改动 U0 的 QSS==旧 DARK_STYLE 回归断言。

测试：`tests/test_gui.py`（26 项，offscreen）：流式气泡、工具卡片、
思考折叠、Stop 保留文字、内嵌确认卡、队列 FIFO、重新生成、代码块
复制、Markdown 只在需要时渲染、Diff/检查点 Tab、档位芯片循环、
关窗保存尺寸、worker 信号与确认流程、计划卡片批准/拒绝（U4）、
没 key 的“去填 key”卡片、错误重试一次、设置对话框 key 字段、
长 transcript 折叠（U6-5）、帮助菜单与快捷键对话框（U6-2）。
截图见 `~/workspace/goals/jarvis-personal-agent/files/gui-main.png`
（另有 gui-diff.png / gui-checkpoints.png / gui-main-light.png /
gui-shortcuts.png）。

## 计划与待办（U4，`jarvis/memory/planner.py` + `jarvis/memory/todo.py`）

`/plan` 的流程是“先出计划卡片，批准后才执行”：`make_plan` 用只读
档（`read_only=True`，工具表只剩 `READ_ACTIONS`；写调用进
`_run_tool_calls` 时被当场拒绝，提示先完成调研和计划）跑一轮调研，
产出 `Plan`（goal + steps + risks，`parse_plan` 从 Markdown 解析）。
四个界面各挂一张计划卡片（CLI 打印 + `[y 批准 / e 编辑 / n 拒绝]`，
空输入算拒绝；TUI `PlanCard` 单键 a/e/r；GUI `PlanCardWidget`，计划
Markdown 可直接在卡片里改；网页版 `showPlanCard`，批准时把编辑后的
Markdown 发回 `/api/chat` 的 `plan_markdown` 字段）。批准后
`plan.to_todos()` 生成 `TodoList`，`attach_todo_tools` 把
`update_todo` 工具重新绑到这份新清单上（旧闭包指着旧清单，必须重绑），
执行轮的提示词是“按顺序执行下面的计划，每完成一步调 update_todo 更新
进度。”，结束时 `verify_plan` 对照验收标准自查（目前只有 CLI 接了
verify，TUI/GUI/网页版跳过——见交付报告）。

`TodoList` 只有三种状态：☐ 待做 / ◐ 进行中 / ☑ 已完成；进行中由
`active_index()` 派生（第一个未完成项），构造上只能有一个。每次变更
调 `on_change`，`todo_set` 事件全量重渲染（三个界面同一份清单）。
清单持久化在会话目录的 `<会话id>-todos.json`，恢复会话时一起读回。

`make_plan` 多了一个可选的 `on_event=None`（只做透传，行为不变）：
计划轮里工具事件（比如写调用被拒）也要能到界面上，四个界面都把自己
的事件回调传进去。

## Diff 与检查点（U5，`jarvis/safety/diffutil.py` + `jarvis/memory/checkpoint.py`）

Diff 分两层：`working_tree_diff()`（只给界面用，不是模型工具）跑
`git diff HEAD`（没提交时退到 `git diff`）+ 未跟踪文件（`git status
--porcelain` 的 `?? ` 项，文本文件 <100KB 才进 diff，目录/二进制/
超大只给一行提示），总长封顶 20000 字符；`parse_diff` 把统一 diff
切成按文件分组的 `[{path, lines, added, removed, summary}]`（git diff
和 `make_diff` 的输出都认；`--- /dev/null` 不会盖掉路径，新文件路径
取下一行的 `+++ b/...`），`render_diff_grouped` 印成纯文本分组。
接受全部 = 什么都不做（改动留在工作区）；拒绝全部 = 按检查点把文件
全部恢复。

检查点是“每次写文件前自动备份”（`snapshot_if_active` 挂在
`register_before_write_hook` 上，三个界面在建会话时注册；钩子按身份
去重，同一进程开第二个会话不会重复备份；备份失败永不拦写入——钩子
返回 `(True, "")`，这个约定有测试守着）。`rewind_to(cutoff_iso)` 按
时间戳恢复（`ts >= cutoff` 的行全弹，文件落到“第一个被弹出的检查点
备份的内容”，也就是这条消息出现之前的样子——`>` 还是 `>=` 是按
悬停恢复的用例抠过的）。Git stash 方案没做：每轮 stash 有冲突风险、
untracked 默认不进 stash、还会污染用户的 stash 列表；文件复制兜底
对 git 和非 git 项目行为一致，git 只用在 Diff 视图里。

悬停恢复：GUI 的 `MessageBubble` 带 `mounted_at`（挂上去那一刻的
UTC ISO），点“恢复检查点”调 `rewind_to(mounted_at)`，只回滚文件，
对话内容不动。CLI 是 `/diff` + `/rewind`。网页版 Diff 抽屉只读查看：
Vercel 的 serverless 文件系统是临时的、按实例走，检查点仓库放上去
行为不一致，所以网页版不做回滚（拒绝全部只给一句说明）。

## 打磨（U6：主题、快捷键、空状态、错误恢复、性能）

**主题（U6-1）**：`jarvis/ui/light.json`（Catppuccin Latte 对位，
和 `theme.json` 同一套 token 键）。`theme.py` 四个生成器里
`to_textual_css` 是从 `to_textual_vars`（dict）拼出来的。TUI 运行时
换主题走 `_apply_theme`：整份主题 CSS 重贴到 `JarvisTUI.CSS` 那个源上
（保留原来的 tie_breaker/scope）再 `refresh_css()` 重算样式——
这是实测出来的唯一走得通的做法：`$变量` 声明只在同源解析时生效，
`screen.set_styles(变量声明)` 会被 Textual 拒收（顶层 `$变量` 不是
合法声明，`/theme` 从 U2 起就是坏的），`stylesheet.set_variables`
是全量替换（会抹掉 Textual 内置的 `$primary` / `$background`…）
且已算好的样式不会因此重算（第一版修法就栽在这里：不报错、不换色，
截图和深色一模一样才发现；测试现断言解析后的背景色本身）。
网页版同时注入两份变量块（`:root` 深色 + `html[data-theme="light"]` 浅色），
`localStorage['jarvis-theme']` 记住选择。

**key 的归属（U6-3）**：`save_key_to_dotenv` 从 `tui.py` 挪到
`jarvis/core/config.py`（GUI 也要用；`tui.py` 留了一个 re-export，
旧导入路径不断）。`has_api_key()` 看当前进程里有没有 key。GUI
`main()` 没 key 不再弹致命框退出：`client=None` 进窗口，首次启动
向导随后自动弹；`_start_turn` 没 client 时不发请求，就地给一张
“去填 key”卡片。注意：测试里 `save_key_to_dotenv` 的调用参数不能
直接写 `DEEPSEEK_API_KEY={key}` 这类字面量（会被管线当密钥脱敏写坏，
见 test_server.py 里的旧坑），用 `save_key_to_dotenv(key, path)`
的调用形式，或像搬运函数时那样从文件里读原文。

**错误恢复（U6-4）**：`events.py` 的 `friendly_error(message)` 是
共享的已知错误映射（429 / 401 / 网络 / 上下文超长 / 额度不足，
没认出来的回 None，界面只显示原文）。重试只给一次：GUI
`_error_streak`（新问题清零、成功清零、停止清零；第二次出错改给
“复制问题，开新会话”，点一下把问题进剪贴板、开新会话、填回输入框）、
TUI 同名计数（第二次按 `n` 开新会话、问题填回输入框）、网页版
`errorStreak`（`done` 时清零）、CLI 的 REPL 直接 try/except（报错不
打死循环，提示“直接再发一次可重试”）。

**快捷键（U6-2）**：四个界面同一张表，入口各不同——CLI 输 `?`（REPL
里裸 `?` 拦截，不发给模型）、TUI 按 `?`（`App.on_key` + HELP_TEXT）、
GUI 帮助菜单（`ShortcutsDialog`，最小宽 560px 免得断句太碎）、网页版
工具栏“⌨ 快捷键”按钮（`#shortcuts` 面板）。表的内容只列本界面真的
能用的，改键要四处一起对。

**性能（U6-5）**：GUI 的 `_trim_transcript` 按批折叠——组件超 200 才
折到 150（`_folded` 计数），不是每次都折；TUI 同款做法是 400 封顶。
DPI 与字体（Sun 的 2560×1600、150% 缩放）、CJK 断行只能在真机上验，
沙箱不宣称。

测试：`tests/test_theme.py`（3 项：深浅 token 对齐、生成器出浅色、
网页版注入两份主题）；`test_gui.py` +5（没 key 卡片、重试一次、
key 字段、折叠、快捷键对话框）；`test_tui.py` 的 error 测试改成
重试一次 + `n` 开新会话；`test_cli.py` +2（`?`、REPL 报错不崩）；
`test_server.py` +1（U6 标记）+ `test_index_uses_theme_vars` 改成
剥两份变量块再查硬编码颜色。

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

- 测试全在 `tests/`：24 个 `test_*.py`（192 项检查）+ `test_fixtures/`（假 MCP 服务器）
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
| 全屏终端 | `python -m jarvis.ui.tui`（Textual；非 TTY 时回落到 `chat:main`） |
| 桌面 | `python -m jarvis.ui.gui`（或双击 `dist/jarvis-gui.exe`） |
| 网页 | `uvicorn jarvis.ui.server:app` |
| Vercel | `api/index.py` 从 `jarvis.ui.server` import app |
| 打包 | `jarvis.spec` 两个 Analysis 入口是 `jarvis/ui/chat.py` 与 `jarvis/ui/gui.py`，`pathex=[SPECPATH]`；CI 用 `python -m PyInstaller`（裸 `pyinstaller` 命令依赖 Scripts 在 PATH 里，不可靠） |
| 安装 | `pip install -e .`，`pyproject.toml` 用 `packages.find(include=["jarvis*"])`，入口 `jarvis = "jarvis.ui.chat:main"` |
