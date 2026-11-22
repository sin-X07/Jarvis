# Changelog

## 0.2.0（2026-11-21，P0–P8 全面完善）

相对 0.1.0（starter + 记忆层 + 6 个工具 + GUI + 网页版）的全部新增：

### 目录重组（2026-11-22，无行为变化）

- 单目录平铺改成彻底分包：`jarvis/core/`（配置、模型、循环、用量、日志）、`jarvis/safety/`（权限、策略、审计、diff）、`jarvis/tools/`（文件、Shell、项目感知）、`jarvis/memory/`（记忆、会话、压缩、待办、检查点、规划）、`jarvis/runtime/`（子代理、后台任务）、`jarvis/extensions/`（技能、MCP、Hooks、插件）、`jarvis/ui/`（命令行、桌面、网页）；测试全挪进 `tests/`，冒烟测试是 `tests/smoke_test.py`
- 入口改成 `python -m jarvis.ui.chat` / `python -m jarvis.ui.gui` / `uvicorn jarvis.ui.server:app`；`pip install -e .` 后的 `jarvis` 命令不变
- `PROJECT_ROOT` 只在 `jarvis/__init__.py` 里算一次，文件沙箱根、技能目录、插件目录、默认 `memory.json` 都从它派生；分层与 import 规则见 `docs/ARCHITECTURE.md`

### P0 基础
- `config.py`：统一配置（环境变量 > `~/.jarvis/config.json` > 默认值），`jarvis --version` 打印 `0.2.0`
- `model.py`：模型调用抽出来，429/超时/5xx 自动重试（指数退避），流式用量估算
- `usage.py`：token 估算、费用估算（DeepSeek 闪电价 ×7.2 折人民币）、按天汇总
- `jlog.py`：结构化日志（只记摘要，不记对话内容和 key）
- `pyproject.toml` / `.env.example` / `run_tests.py`：`jarvis` 命令入口、全量测试一键跑

### P1 安全与 Shell
- `permissions.py`：权限三档 readonly / edit / auto，写操作与危险命令需确认
- `policy.py`：危险命令黑名单（`rm -rf /`、`format`、`del /s` 等），工作区逃逸检查
- `audit.py`：审计日志 `~/.jarvis/audit.jsonl`（只记动作/目标/结果）
- `diffutil.py`：写文件前先给用户看 diff
- `shell_tools.py`：`run_shell`（策略检查 → 确认 → 超时 → 输出截断 → 审计）

### P2 文件与项目感知
- `edit_file` / `glob_files` / `grep_files` / `git_status` / `git_diff` / `git_log` / `git_commit` / `web_search`
- `project.py`：项目规则（`JARVIS.md` / `AGENTS.md` / `CLAUDE.md`）+ 项目目录索引进 system prompt

### P3 会话与上下文
- `session_store.py`：会话持久化 `~/.jarvis/sessions/*.jsonl`，创建/恢复/分叉/删除
- `compaction.py`：上下文超阈值自动压缩成摘要（整轮保留，工具链不断）
- 长期记忆升级：删/改事实、>10 条时按问题检索

### P4 规划模式
- `todo.py`：待办清单 + `update_todo` 工具
- `checkpoint.py`：写文件前自动备份，`/rewind` 一键恢复
- `planner.py`：`/plan` 先调研出计划，确认后执行，结束对照验收标准自查

### P5 并行与子代理
- 只读工具并行跑（ThreadPoolExecutor），写操作永远串行；结果顺序不变
- `subagent.py`：`delegate` 工具，子代理只做 research / code / test 其中一件事
- `jobs.py`：`run_shell_background` / `job_status` / `job_result`，长命令后台跑

### P6 技能、MCP、Hooks、插件
- `skills.py` + `skills/`：内置 4 个技能（code-review、write-tests、project-onboarding、github-trending-summary），`use_skill` 调用
- `mcp_client.py`：标准库 JSON-RPC over stdio 接 MCP 服务器，`mcp.json.example`（filesystem + github）
- `hooks.py`：before_write / before_shell / on_session_end 三个钩子点
- `plugins.py`：`plugins/` 里放一个 `.py` 就能加一个工具

### P7 界面升级
- CLI：`--print` / `--resume` / `--sessions` / `--model` / `--permission`，REPL 命令 20 个（`/help` 看全部）
- `md.py`：Markdown → ANSI（终端）/ HTML（GUI、网页）共用渲染
- GUI：会话列表、设置对话框、恢复检查点按钮、用量显示、写操作内嵌确认卡片
- 网页版：Markdown 渲染、多会话（localStorage）、用量面板、撤销上一轮、时间线

### P8 可观测性与分发
- `trajectory.py`：`jarvis --trajectory <id>` / GUI“轨迹”按钮 / 网页时间线
- 用量面板：`jarvis --usage`、`/usage`、日预算提醒（超 80% / 超限）
- `jarvis.spec` + `build-exe.ps1` + `.github/workflows/build.yml`：打 tag 自动出 Windows exe
- `update.py`：`jarvis --update` 检查 GitHub releases
- 本文件 + `docs/usage-manual.md`（日常使用手册）

### UI 优化 U0–U6（2026-11-22，成品化）

- U0 底座：一个事件核心（`events.py`：text_delta / thinking_delta / tool_start / tool_end / status / turn_end / error）+ 三个薄渲染层；`theme.json` 单一颜色源，生成 CSS 变量 / QSS / Textual CSS 三份产物；用量数字全部改真实（`stream_options={"include_usage": True}`，最后一个 chunk 的真 usage，拿不到记“未知”、界面显示“—”，不再估算）
- U1 网页版：侧栏会话（搜索/改名/删除）、模式与模型芯片、工具卡片（状态+耗时+输出预览）、思考折叠、消息悬停操作（复制/重新生成）、代码块复制、空状态提示画布、错误卡片+重试、SSE 断线横幅、停止、忙时 Tab 排队追问
- U2 全屏 TUI（Textual，`python -m jarvis.ui.tui`）：四层布局 + 侧栏 Tab（会话/待办/Diff/检查点）+ 可拖分隔条、流式光标、工具卡片 Ctrl+O、确认单键 a/A/d/e、计划卡片、Diff 窗格、检查点时间线、状态栏真 token、launchpad 空状态、`/setup` 向导、`?` 快捷键总览、非 TTY 时回落到 CLI
- U3 桌面 GUI：聊天区气泡 + 悬停操作 + 工具卡片 + 思考折叠、多行自增高输入框、发送中变 Stop、忙时排队、确认改内嵌卡片（不再弹框）、右侧 Tab 页（会话/待办/Diff/检查点）、顶栏模式/模型芯片、底部状态行（context 条 + 真 token/费用）、窗口尺寸记忆
- U4 计划与待办：`/plan` + 计划模式（三个界面同一规则：规划档不提供写/执行工具）、待办清单 ☐/◐/☑ 持久化（`todo_set` 事件全量重渲染，三个界面同一份）
- U5 Diff 与检查点：Diff 卡片按文件分组（+N/-M）、接受全部/拒绝全部；每轮写前自动快照（文件复制兜底）；悬停消息“恢复检查点”（只回滚文件）；CLI `/diff`（含未跟踪文件）；网页版 Diff 抽屉（只读查看）
- U6 打磨：深色/浅色主题（`light.json`，三个界面都能切）；快捷键总览统一（CLI `?`、GUI 帮助菜单、网页版 ⌨ 快捷键、TUI `?`）；空状态统一（“能记住事情、读写文件、跑命令。”）；首次启动向导（没 key 时 GUI 自动弹设置填 key，TUI `/setup`）；错误恢复统一（已知错误一句话提示，重试只给一次，第二次改“复制问题，开新会话”）；长 transcript 折叠（GUI 200 / TUI 400）
- 测试：24 个文件 192 项检查全绿（含 Textual pilot 测试、Qt offscreen 测试、主题生成器测试）

### 已知限制（诚实写）
- 打包脚本只在 Windows 上验证过用法，Linux 沙箱里没有实际跑过 PyInstaller
- 网页版仍是无状态的：会话存在浏览器 localStorage，不会同步到别的设备
- `jarvis --update` 只检查不安装；release 需要仓库先打过 tag 才会返回结果
- 用量数字是真值（API 返回的 usage），但拿不到 usage 的调用记“未知”、显示“—”；费用按 DeepSeek 闪电版峰值价算
- TUI 在 Windows Terminal 下的表现、真 key 跑 `include_usage`、GUI exe 重新打包、Vercel 下次 push 后的网页版，都还需要在真机上复核
- 网页版 Diff 是只读查看（serverless 文件系统是临时的，没法放检查点仓库）；计划轮的 token 不进用量统计

## 0.1.0（2026-11-21 之前）
- starter：agent 循环、6 个工具（list_files、write_text_file、fetch_url 等）
- 记忆层：ConversationMemory（30 条整轮裁剪）+ FactStore（memory.json）
- PySide6 桌面 GUI、FastAPI + SSE 网页版（BYOK）、Vercel 部署
