"""Token 与费用统计（U0-5 修订版：全部用 API 返回的真数）。

用量契约（2026-11-22 修订，之前是“流式按字符估算”）：
- token 数：全部来自 DeepSeek 官方 API 返回的 usage。非流式直接读
  `response.usage`；流式传 `stream_options={"include_usage": True}`，
  最后一个 chunk 自带整次请求的 usage（见 model.py）。
- 拿不到 usage 时（服务端异常、旧版 SDK）：该笔记为 unknown，界面
  显示"—"/"未知"，不混入合计，**绝不拿估算顶替**。
- 费用：真 token 数 × DeepSeek 官方价目表（deepseek-flash：输入
  $0.15/$0.30、输出 $0.60/$1.20 每 1M token，峰谷价；缓存命中
  $0.003/$0.006）。这里统一按峰时价算，是个偏高的数；再按 7.2
  折成人民币。真实账单以 DeepSeek 平台为准，这里只帮你心里有数。
- context 用量条：最近一次调用的真实 prompt_tokens / 上下文窗口
  （deepseek-flash 为 1M token）。一次调用的 prompt_tokens 就是
  当次发出去的完整上下文大小，所以这个比值是真实的。

`estimate_tokens` 还在，但只供 `compaction.py` 内部触发判断用
（发请求前先估一下上下文够不够触发压缩，是调用前判断，不是
显示），其结果不进入任何用量统计、不出现在任何界面上。

用量只记数字（模型、输入/输出 token、费用、时间），不记任何对话内容。
"""

import json
import os
from datetime import date

from jarvis.core.config import DEFAULT_CONTEXT_LIMIT, default_config_dir

# deepseek-flash 官方价目（美元 / 1M token，峰时价；2026-11-21 核实）
FLASH_INPUT_USD_PER_M = 0.30
FLASH_OUTPUT_USD_PER_M = 1.20
USD_TO_CNY = 7.2


def estimate_tokens(text: str) -> int:
    """按字符数估算 token：中文约 1 字 1 token，英文约 4 字符 1 token。

    注意：这只是内部启发式，专供 compaction 在发请求前判断“上下文
    够不够触发压缩”。估算值绝不进入用量统计、绝不显示在界面上。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + max(1, other // 4) if other else cjk


def cost_usd(prompt_tokens: int, completion_tokens: int,
             model: str = "deepseek-flash") -> float:
    """按峰时价算一次调用的费用（美元）。非 flash 模型暂按同价算。"""
    _ = model  # 现在只有 deepseek-flash 一档，留着接口以后分模型计价
    return (prompt_tokens * FLASH_INPUT_USD_PER_M
            + completion_tokens * FLASH_OUTPUT_USD_PER_M) / 1_000_000


class UsageTracker:
    """攒一轮（或一个会话）的用量：每次模型调用记一笔（只记真数）。

    usage dict 形状：{"prompt_tokens": int | None,
    "completion_tokens": int | None, "unknown": bool}。
    unknown=True 的调用只计次数，不混入 token 合计。
    """

    def __init__(self, model: str = "deepseek-flash", persist: bool = False):
        self.model = model
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.unknown_calls = 0  # 没返回用量的调用次数（不混入合计）
        self.last_prompt_tokens: int | None = None  # 最近一次真 prompt 数
        self._persist = persist
        self._path = os.path.join(default_config_dir(), "usage.jsonl")

    def record(self, usage: dict, model: str | None = None) -> None:
        self.calls += 1
        if usage.get("unknown"):
            self.unknown_calls += 1
        else:
            self.prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
            self.completion_tokens += int(usage.get("completion_tokens", 0) or 0)
            self.last_prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        if self._persist:
            self._append_line(model or self.model, usage)

    def _append_line(self, model, usage) -> None:
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "date": date.today().isoformat(),
                    "model": model,
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "unknown": bool(usage.get("unknown")),
                }, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 用量记不下来不拦主流程

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cost_usd(self) -> float:
        return cost_usd(self.prompt_tokens, self.completion_tokens, self.model)

    @property
    def cost_cny(self) -> float:
        return self.cost_usd * USD_TO_CNY

    def context_pct(self, limit: int | None = None) -> float | None:
        """最近一次真实 prompt_tokens / 上下文窗口（0-1）。

        没拿到过真数时返回 None（界面显示"—"）。limit 默认读配置
        （deepseek-flash 为 1_000_000）。
        """
        if self.last_prompt_tokens is None:
            return None
        limit = limit or DEFAULT_CONTEXT_LIMIT
        if not limit:
            return None
        return self.last_prompt_tokens / limit

    def summary(self) -> str:
        """一句话小结：本次共 X token（输入 A / 输出 B），约 ¥Y。"""
        text = (f"本次共 {self.total_tokens} token"
                f"（输入 {self.prompt_tokens} / 输出 {self.completion_tokens}）"
                f"，约 ¥{self.cost_cny:.4f}")
        if self.unknown_calls:
            text += f"（另有 {self.unknown_calls} 次调用未返回用量）"
        return text


def daily_summary(days: int = 7, path: str | None = None) -> list:
    """按天汇总用量（P8-2 用量面板的数据来源）。

    返回 [{"date": "2026-11-21", "calls": n, "tokens": n,
    "cost_cny": x}]，按日期升序。文件不存在时返回空列表。
    未返回用量的调用只计 calls，不计 tokens。
    """
    path = path or os.path.join(default_config_dir(), "usage.jsonl")
    by_day: dict = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                day = by_day.setdefault(row.get("date", "?"), {
                    "date": row.get("date", "?"), "calls": 0,
                    "tokens": 0, "cost_cny": 0.0})
                day["calls"] += 1
                pt = int(row.get("prompt_tokens") or 0)
                ct = int(row.get("completion_tokens") or 0)
                day["tokens"] += pt + ct
                day["cost_cny"] += cost_usd(pt, ct) * USD_TO_CNY
    except OSError:
        return []
    rows = sorted(by_day.values(), key=lambda r: r["date"])
    return rows[-days:] if days else rows
