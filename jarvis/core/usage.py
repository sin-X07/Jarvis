"""Token 与费用统计（P3-5，model.py 的流式估算也用它，所以提前在 P0 落地）。

两个 honest 前提，写在最前面：
1. token 数：非流式调用直接读 API 返回的 usage，是真的；流式调用
   API 不一定给 usage，这时按字符估算（中文约 1 字 1 token，英文约
   4 字符 1 token），结果标 estimated=True。
2. 费用：按 DeepSeek 官方价目表（deepseek-flash：输入 $0.15/$0.30、
   输出 $0.60/$1.20 每 1M token，峰谷价；缓存命中 $0.003/$0.006）。
   这里统一按峰时价算，是个偏高的估算；再按 7.2 折成人民币。
   真实账单以 DeepSeek 平台为准，这里只帮你心里有数。

用量只记数字（模型、输入/输出 token、费用、时间），不记任何对话内容。
"""

import json
import os
from datetime import date

from jarvis.core.config import default_config_dir

# deepseek-flash 官方价目（美元 / 1M token，峰时价；2026-11-21 核实）
FLASH_INPUT_USD_PER_M = 0.30
FLASH_OUTPUT_USD_PER_M = 1.20
USD_TO_CNY = 7.2


def estimate_tokens(text: str) -> int:
    """按字符数估算 token：中文约 1 字 1 token，英文约 4 字符 1 token。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + max(1, other // 4) if other else cjk


def cost_usd(prompt_tokens: int, completion_tokens: int,
             model: str = "deepseek-flash") -> float:
    """按峰时价估算一次调用的费用（美元）。非 flash 模型暂按同价估算。"""
    _ = model  # 现在只有 deepseek-flash 一档，留着接口以后分模型计价
    return (prompt_tokens * FLASH_INPUT_USD_PER_M
            + completion_tokens * FLASH_OUTPUT_USD_PER_M) / 1_000_000


class UsageTracker:
    """攒一轮（或一个会话）的用量：每次模型调用记一笔。"""

    def __init__(self, model: str = "deepseek-flash", persist: bool = False):
        self.model = model
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.estimated_calls = 0  # 其中有几笔是估算的
        self._persist = persist
        self._path = os.path.join(default_config_dir(), "usage.jsonl")

    def record(self, usage: dict, model: str | None = None) -> None:
        self.calls += 1
        self.prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
        self.completion_tokens += int(usage.get("completion_tokens", 0) or 0)
        if usage.get("estimated"):
            self.estimated_calls += 1
        if self._persist:
            self._append_line(model or self.model, usage)

    def _append_line(self, model, usage) -> None:
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "date": date.today().isoformat(),
                    "model": model,
                    "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                    "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                    "estimated": bool(usage.get("estimated")),
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

    def summary(self) -> str:
        """一句话小结：本次共 X token（输入 A / 输出 B），约 ¥Y。"""
        est = "（部分为估算）" if self.estimated_calls else ""
        return (f"本次共 {self.total_tokens} token"
                f"（输入 {self.prompt_tokens} / 输出 {self.completion_tokens}）"
                f"，约 ¥{self.cost_cny:.4f}{est}")


def daily_summary(days: int = 7, path: str | None = None) -> list:
    """按天汇总用量（P8-2 用量面板的数据来源）。

    返回 [{"date": "2026-11-21", "calls": n, "tokens": n,
    "cost_cny": x}]，按日期升序。文件不存在时返回空列表。
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
                pt = int(row.get("prompt_tokens", 0) or 0)
                ct = int(row.get("completion_tokens", 0) or 0)
                day["tokens"] += pt + ct
                day["cost_cny"] += cost_usd(pt, ct) * USD_TO_CNY
    except OSError:
        return []
    rows = sorted(by_day.values(), key=lambda r: r["date"])
    return rows[-days:] if days else rows
