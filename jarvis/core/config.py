"""Jarvis 配置集中化（P0-1）。

为什么要有一个 config.py：以前模型名、base_url、步数上限、超时这些
散在各个文件里（agent.py、server.py 各一份），改一处漏一处。现在
所有可配项只在这里定义，来源优先级是：

    显式传入的 overrides > 环境变量 > 默认值

另外这里还放一份 `~/.jarvis/config.json` 的支持：文件里有的键同样
生效，优先级低于环境变量。启动时 `validate()` 会把配错的值拦下来，
给一句话人话提示，而不是让程序在半路抛 traceback。

配置项一览见 .env.example，每个键都有一句话说明。
"""

import json
import os

__version__ = "0.2.0"

DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MAX_STEPS = 10
DEFAULT_TIMEOUT = 60.0          # 单次 API 调用超时（秒）
DEFAULT_RETRY_TIMES = 3         # 429/超时/5xx 时最多重试几次
DEFAULT_SHELL_TIMEOUT = 30.0    # run_shell 默认超时（秒）
DEFAULT_PERMISSION = "edit"     # readonly / edit / auto
DEFAULT_MAX_BUDGET_USD = 0.0    # 0 = 不限预算
DEFAULT_CONTEXT_LIMIT = 1_000_000   # deepseek-flash 上下文窗口
DEFAULT_COMPACTION_THRESHOLD = 0.7  # 用到 70% 时触发压缩
DEFAULT_DAILY_BUDGET_USD = 0.0  # 0 = 不设日预算提醒

# 环境变量名 → 配置键
_ENV_MAP = {
    "DEEPSEEK_MODEL": "model",
    "DEEPSEEK_BASE_URL": "base_url",
    "AGENT_MAX_STEPS": "max_steps",
    "JARVIS_TIMEOUT": "timeout",
    "JARVIS_RETRY_TIMES": "retry_times",
    "JARVIS_SHELL_TIMEOUT": "shell_timeout",
    "JARVIS_PERMISSION": "permission",
    "JARVIS_MAX_BUDGET_USD": "max_budget_usd",
    "JARVIS_DAILY_BUDGET_USD": "daily_budget_usd",
    "JARVIS_CONTEXT_LIMIT": "context_limit",
}

_INT_KEYS = {"max_steps", "retry_times", "context_limit"}
_FLOAT_KEYS = {"timeout", "shell_timeout", "max_budget_usd", "daily_budget_usd"}


def _coerce(key, raw):
    """把环境变量的字符串转成配置需要的类型，转不成时报错给人看。"""
    if key in _INT_KEYS:
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"配置 {key} 应该是整数，现在是 {raw!r}")
    if key in _FLOAT_KEYS:
        try:
            return float(raw)
        except ValueError:
            raise ValueError(f"配置 {key} 应该是数字，现在是 {raw!r}")
    return raw


class JarvisConfig:
    """一份 Jarvis 配置。字段都有默认值，改哪个都行。"""

    def __init__(self, **overrides):
        self.model = DEFAULT_MODEL
        self.base_url = DEFAULT_BASE_URL
        self.max_steps = DEFAULT_MAX_STEPS
        self.timeout = DEFAULT_TIMEOUT
        self.retry_times = DEFAULT_RETRY_TIMES
        self.shell_timeout = DEFAULT_SHELL_TIMEOUT
        self.permission = DEFAULT_PERMISSION
        self.max_budget_usd = DEFAULT_MAX_BUDGET_USD
        self.daily_budget_usd = DEFAULT_DAILY_BUDGET_USD
        self.context_limit = DEFAULT_CONTEXT_LIMIT
        self.compaction_threshold = DEFAULT_COMPACTION_THRESHOLD
        for key, value in overrides.items():
            if not hasattr(self, key):
                raise ValueError(f"没有这个配置项：{key}")
            setattr(self, key, value)

    def validate(self) -> list:
        """检查配置是否合法，返回问题清单（空 = 没问题）。"""
        problems = []
        if not self.model:
            problems.append("模型名是空的：请设置 DEEPSEEK_MODEL 或用默认值 deepseek-flash")
        if not self.base_url.startswith(("http://", "https://")):
            problems.append(f"base_url 不像个网址：{self.base_url}")
        if not 1 <= self.max_steps <= 100:
            problems.append(f"步数上限要在 1–100 之间，现在是 {self.max_steps}")
        if self.timeout <= 0:
            problems.append(f"超时时间要是正数，现在是 {self.timeout}")
        if not 0 <= self.retry_times <= 10:
            problems.append(f"重试次数要在 0–10 之间，现在是 {self.retry_times}")
        if self.permission not in ("readonly", "edit", "auto"):
            problems.append(
                f"权限档位只能是 readonly / edit / auto，现在是 {self.permission!r}")
        if self.max_budget_usd < 0 or self.daily_budget_usd < 0:
            problems.append("预算不能是负数")
        if not 0 < self.compaction_threshold < 1:
            problems.append("压缩阈值要在 0–1 之间")
        return problems

    def check_or_exit(self) -> None:
        """校验不通过时打印问题并退出（CLI 入口用）。"""
        problems = self.validate()
        if problems:
            raise SystemExit("配置有问题：\n" + "\n".join(f"- {p}" for p in problems))


def default_config_dir() -> str:
    """用户级配置目录：~/.jarvis（会话、审计、用量、skills 都放这里）。"""
    return os.path.join(os.path.expanduser("~"), ".jarvis")


def save_config(config: JarvisConfig) -> str:
    """把配置写回 ~/.jarvis/config.json（GUI 设置对话框用）。

    只写 JarvisConfig 的公开字段；写坏了不拦调用方，返回实际路径。
    """
    path = os.path.join(default_config_dir(), "config.json")
    os.makedirs(default_config_dir(), exist_ok=True)
    data = {key: getattr(config, key)
            for key in ("model", "base_url", "max_steps", "timeout", "retry",
                        "shell_timeout", "permission", "max_budget_usd",
                        "daily_budget_usd", "context_limit")
            if hasattr(config, key)}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def load_config(overrides=None, env=None) -> JarvisConfig:
    """按优先级拼一份配置：overrides > 环境变量 > ~/.jarvis/config.json > 默认值。"""
    env = os.environ if env is None else env
    cfg = JarvisConfig()
    # 1) 用户配置文件（最低优先级）
    path = os.path.join(default_config_dir(), "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for key, value in data.items():
                if hasattr(cfg, key):
                    setattr(cfg, key, value)
    except (OSError, json.JSONDecodeError):
        pass  # 没有配置文件或写坏了，都按默认值走，不拦启动
    # 2) 环境变量
    for env_name, key in _ENV_MAP.items():
        if env_name in env and env[env_name] != "":
            setattr(cfg, key, _coerce(key, env[env_name]))
    # 3) 显式传入
    for key, value in (overrides or {}).items():
        if not hasattr(cfg, key):
            raise ValueError(f"没有这个配置项：{key}")
        setattr(cfg, key, value)
    return cfg
