# Jarvis Starter — 最小 Agent Loop（Python 版）

一个能跑的最小 personal agent 起手项目：用 DeepSeek 的 API，
通过 agent loop（思考 → 调工具 → 看结果 → 再思考）回答问题。

## 快速开始

```powershell
# 1. 建虚拟环境并装依赖
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. 配 key：复制 .env.example 为 .env，填入你的 DeepSeek API key
#    key 在 https://platform.deepseek.com/api_keys 创建

# 3. 先验证 key 能通
python smoke_test.py

# 4. 跑 agent
python agent.py "现在几点？12*(3+4) 等于多少？"
python agent.py "帮我读一下 README.md，告诉我这个项目是干什么的"

# 5. 离线测试循环逻辑（不用 key、不联网）
python test_loop.py
python test_memory.py

# 6. 多轮对话（带记忆）
python chat.py
# 在对话里说“记住，我最喜欢蓝色”，或者用命令：/记住 我最喜欢的颜色是蓝色
# 下次再启动 chat.py，它还记得
# 也可以试新工具：让它“列一下项目里有什么文件”“写个笔记到 notes/todo.txt”
# “帮我看看 https://wttr.in/Beijing?format=3 今天天气怎么样”

# 7. 图形界面（桌面版：pip install -r requirements-gui.txt）
python gui.py

# 8. 网页版（和桌面版共用同一个 agent 核心）
pip install -r requirements.txt   # 已含 fastapi + uvicorn
uvicorn server:app --reload
# 打开 http://127.0.0.1:8000/ 就能用；部署到 Vercel 见下面“网页版”一节
python test_server.py
```

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `agent.py` | agent loop 本体：发消息 → 模型要么回答、要么要求调工具 → 执行工具 → 把结果喂回去 → 循环 |
| `tools.py` | 工具：查时间、计算器、读文件、列目录、写文件、取网页。加工具 = 写一个函数 + 一段 schema + 注册进 `TOOL_FUNCS` |
| `test_tools.py` | 离线测试：文件工具往返与沙箱、fetch_url（本地 HTTP 服务模拟） |
| `memory.py` | 记忆层：短期记忆（多轮对话历史 + 整轮裁剪）与长期记忆（`memory.json` 事实库 + 检索 + 记忆工具） |
| `chat.py` | 多轮对话入口：持续对话，支持 `/记住` `/记忆` `/清空` `/退出` |
| `worker.py` | GUI 的后台线程：`AgentWorker` 在 QThread 里跑循环，结果用信号发回主线程 |
| `gui.py` | 图形界面：聊天区、记忆侧栏、输入框，深色 QSS 风格。跑 `python gui.py` |
| `server.py` | 网页版后端：FastAPI，`GET /` 返回聊天页，`POST /api/chat` 用 SSE 流式返回 token / 工具调用 / 最终结果 |
| `api/index.py` | Vercel 入口：只做一件事，把 `server.py` 的 app 再导出一遍 |
| `vercel.json` | Vercel 配置：`/api/*` 和 `/` 都走到 Python 函数，函数最长跑 60 秒 |
| `test_server.py` | 离线测试：聊天页、健康检查、SSE 流式、工具调用、空消息、网页版不依赖 PySide6 |
| `requirements-gui.txt` | GUI 专用依赖（PySide6），单独放：Vercel 只读 `requirements.txt`，PySide6 约 260MB 会超 serverless 上限 |
| `test_stream.py` | 离线测试：流式碎片拼接、拆段的工具调用参数、流式历史 |
| `test_gui.py` | 离线冒烟测试：Worker 信号与主窗口创建（offscreen，不用真显示器） |
| `smoke_test.py` | 一次最普通的 API 调用，验证 key 和模型名 |
| `test_loop.py` | 离线测试：用假模型验证循环逻辑，不花钱、不联网 |
| `test_memory.py` | 离线测试：验证裁剪、事实存取检索、多轮历史共享 |
| `.env.example` | 配置模板，复制为 `.env` 后填 key |
| `requirements.txt` | 依赖：`openai`（DeepSeek 兼容 OpenAI 接口）、`python-dotenv`、网页版的 `fastapi` + `uvicorn` |

## 模型名注意

DeepSeek 官方 API 上，DeepSeek-V4.1-Flash-1017 的模型 id 是
`deepseek-flash`（旧 id `deepseek-v4-flash` 也能用，会路由到同一版本）。
不要发 `deepseek-v4.1-1017`——那是权重版本号，不是 API 模型名。
默认 base URL：`https://api.deepseek.com`。

## 记忆：两层

- **短期记忆（多轮对话）**：`ConversationMemory` 把整段对话历史存在
  `messages` 列表里，每轮追加。历史太长（默认 30 条消息）时按“整轮”
  裁掉最旧的——一条 user 消息连同它后面的 assistant/tool 消息一起删，
  绝不断开 tool 调用链，否则 API 会报错。
- **长期记忆（事实）**：`FactStore` 把事实存进 `memory.json`（`{"text": ...,
  "created_at": ...}`），启动时拼进 system prompt；模型也可以主动调
  `remember_fact` / `recall_facts` 读写。检索现在是 bigram 关键词匹配，
  事实多了以后再升级成向量检索。
- **隐私**：`memory.json` 是你的个人事实，`.gitignore` 已排除，不会进公开仓库。

## 工具：现在有 6 个

| 工具 | 能干什么 | 限制 |
| --- | --- | --- |
| `get_current_time` | 查现在的时间 | — |
| `calculator` | 算算术表达式 | 只认数字、括号和几个数学函数 |
| `read_text_file` | 读项目内的文本文件 | 只能读项目内，最多 4000 字 |
| `list_files` | 列出项目内某个文件夹 | 只能看项目内，一次最多 100 项 |
| `write_text_file` | 在项目内新建或覆盖文件 | 只能写项目内，内容不超 100000 字 |
| `fetch_url` | 取一个网页的正文 | 只 GET、10 秒超时、最多 200KB、返回最多 4000 字 |

安全边界：文件工具的沙箱在 `tools.py` 的 `_resolve_inside_project`
里，`../` 这种越狱路径会被拦下；`fetch_url` 只认 http/https，
失败时返回文字而不是抛异常，模型可以接着处理。

**故意没做的：跑 shell 命令**。这是最有用、也最危险的工具——模型
能跑任何命令，包括删文件。真要加，得先做确认关卡（先把命令亮出来，
你点头了再跑）。那是下一步的候选。

## 图形界面：怎么跑起来的

- `gui.py` 是主窗口：左侧聊天区、右侧记忆侧栏、底部输入框。
- 线程规矩：点发送后，`AgentWorker` 在 QThread 里跑 `run_agent`，
  工作线程绝不碰界面控件，结果只通过信号（`token` / `tool_call` /
  `status` / `finished` / `error`）传回主线程更新。
- 流式输出：`run_agent(..., stream=True)`，回答边生成边冒字；
  工具调用的参数在流式模式下是碎片到的，`agent.py` 按 index 拼起来再执行。
- `run_agent` 多了 `on_event` 回调：循环里的进展（调工具、工具返回、
  token）都走事件，命令行默认还是打印，GUI 接自己的处理函数。
  历史里存的消息统一是 dict，流式和非流式一样。

## 网页版：怎么跑起来的

- 和桌面版是同一个核心（`agent.py` / `tools.py` / `memory.py`），
  只是壳不同：桌面版进程内直接调 `run_agent`，网页版在外面包一层
  HTTP。聊天页（`server.py` 里内嵌的 HTML，不单独放文件）用
  `fetch` 发 `POST /api/chat`，再用 SSE（Server-Sent Events）
  把回答一个字一个字接回来，工具调用显示成一行小灰字。
- 状态在浏览器那边：对话历史和长期事实存在 localStorage，每次
  请求一起发过来，服务器跑完一轮，把更新过的历史和事实放在最后
  的 `done` 事件里还回去。这样服务器两次请求之间不留状态，才能
  放到 Vercel 这种无状态的 serverless 上；冷启动丢了事实也不怕，
  因为浏览器下次请求会重新带过来。
- API key 是各用各的（BYOK）：页面上方有个 key 输入框，访客填自己
  的 DeepSeek key，存在自己浏览器的 localStorage 里，每次请求一起
  发过来；服务器只拿它调这一次 DeepSeek，不存、不缓存、不记日志。
  请求没带 key 时，服务器才用环境变量里的 `DEEPSEEK_API_KEY` 兜底
  （本地开发就靠这个）；两个都没有就报 400，提示填 key。
- `run_agent` 是同步的，SSE 要边跑边发，所以循环跑在一个线程里，
  事件先进队列，`StreamingResponse` 再从队列里一个个吐出去。
  和 GUI 的 `worker.py` 是同一个思路：跑循环的线程不直接碰界面，
  只发事件。
- 网页版默认最多 6 步（`WEB_MAX_STEPS`）：Vercel 免费版函数最长
  跑 60 秒，步数太多容易超时。

### 部署到 Vercel

1. 把仓库推到 GitHub，Vercel 里 New Project → Import 这个仓库。
2. 环境变量：**不要**在 Vercel 上设 `DEEPSEEK_API_KEY`——设了它，
   不填 key 的访客就会用你的 key。每个访客在页面上填自己的 key。
   `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` 不填就用默认值。
3. Deploy。部署完打开给你的域名就能用。
4. 限制要知道：免费版函数最长 60 秒（一轮对话如果调很多次工具
   可能超时）；`memory.json` 在 Vercel 上写不进磁盘，长期记忆靠
   浏览器 localStorage 带着走；key 是访客自己的，但请求要经过你
   的服务器转发——服务器不存 key，不过理论上服务器能看到它，这是
   BYOK 模式的固有前提，真要完全不经过你，就得在浏览器里直调
   DeepSeek（那是下一步的改法）。

## 下一步

- 设置页：界面里改 key、改模型、改最大步数
- 打包：PyInstaller 打成 .exe，不用装 Python 也能双击运行
- 给 shell 工具加确认关卡：亮出命令 → 你确认 → 再执行
- 详见学习指南：《Personal Agent 学习指南：从概念到第一个 Agent Loop》
