# coding=utf-8
"""
AI 条目摘要模块

为进入推送的热榜条目批量生成一句话摘要（ai_summary.enabled 时启用）。

设计：
- 复用 AIClient（与 ai_analysis/ai_translation 共享模型配置）
- 编号行格式输入输出（与翻译器相同的解析方式，比 JSON 更抗格式漂移）
- 失败静默跳过：摘要属于增强信息，失败不影响推送
"""

import re
import time
from typing import Dict, List, Optional

from trendradar.ai.client import AIClient
from trendradar.utils.text import clean_summary_text

# 提取编号行: [1] 摘要文字 或 1. 摘要文字
_NUMBERED_LINE_RE = re.compile(r"^\s*[\[【]?(?P<idx>\d+)[\]】]?\s*[\.、:：]?\s*(?P<text>.+)$")

# 每次运行最多抓取的条目安全上限（防止配置过大）
HARD_MAX_ITEMS = 100


class AIItemSummarizer:
    """热榜条目一句话摘要生成器"""

    def __init__(self, ai_config: Dict, summary_config: Dict):
        """
        Args:
            ai_config: config["AI"]（LiteLLM 模型配置）
            summary_config: config["AI_SUMMARY"]（ENABLED/MAX_ITEMS/MAX_CHARS/BATCH_SIZE/BATCH_INTERVAL）
        """
        self.client = AIClient(ai_config)
        self.enabled = bool(summary_config.get("ENABLED", False))
        try:
            self.max_items = int(summary_config.get("MAX_ITEMS", 20))
        except (TypeError, ValueError):
            self.max_items = 20
        self.max_items = max(0, min(self.max_items, HARD_MAX_ITEMS))
        try:
            self.max_chars = int(summary_config.get("MAX_CHARS", 60))
        except (TypeError, ValueError):
            self.max_chars = 60
        try:
            self.batch_size = int(summary_config.get("BATCH_SIZE", 10))
        except (TypeError, ValueError):
            self.batch_size = 10
        self.batch_size = max(1, self.batch_size)
        try:
            self.batch_interval = int(summary_config.get("BATCH_INTERVAL", 2))
        except (TypeError, ValueError):
            self.batch_interval = 2

    def summarize_batch(self, titles: List[str]) -> Dict[int, str]:
        """
        为一批标题生成一句话摘要

        Args:
            titles: 标题列表（顺序即编号）

        Returns:
            {序号(从1开始): 摘要文本}，失败的条目不存在
        """
        if not titles:
            return {}

        numbered = "\n".join(f"[{i}] {t}" for i, t in enumerate(titles, start=1))
        system_prompt = (
            "你是新闻摘要助手。为每条新闻标题写一句话摘要（不超过指定字数），"
            "补充这条新闻的关键信息（谁/什么事/为什么重要）。"
            "只依据标题字面信息，不要编造细节。严格按编号逐行输出，格式：[编号] 摘要"
        )
        user_prompt = f"以下共 {len(titles)} 条新闻标题，每条写不超过 {self.max_chars} 字的一句话摘要：\n\n{numbered}"

        try:
            response = self.client.chat(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
            )
        except Exception as e:
            print(f"[AI摘要] 生成失败: {e}")
            return {}

        return self._parse_numbered_response(response, len(titles))

    def _parse_numbered_response(self, response: str, expected: int) -> Dict[int, str]:
        """解析编号行响应，返回 {序号: 摘要}"""
        result: Dict[int, str] = {}
        if not response:
            return result

        for line in response.splitlines():
            match = _NUMBERED_LINE_RE.match(line.strip())
            if not match:
                continue
            idx = int(match.group("idx"))
            text = match.group("text").strip()
            if not text or idx < 1 or idx > expected or idx in result:
                continue
            result[idx] = clean_summary_text(text, self.max_chars)

        return result

    def attach_to_stats(self, stats: List[Dict], quiet: bool = False) -> int:
        """
        为热榜统计条目附加 ai_summary（就地修改 title_data）

        Args:
            stats: 热榜统计结构 [{"word": ..., "titles": [title_data, ...]}, ...]
            quiet: 是否静默

        Returns:
            成功附加摘要的条数
        """
        if not self.enabled or self.max_items <= 0 or not stats:
            return 0

        # 收集待摘要条目（按显示顺序，截断到 max_items）
        targets: List[Dict] = []
        for group in stats:
            for title_data in group.get("titles", []):
                if len(targets) >= self.max_items:
                    break
                if not title_data.get("ai_summary"):
                    targets.append(title_data)
            if len(targets) >= self.max_items:
                break

        if not targets:
            return 0

        total = len(targets)
        attached = 0
        for i in range(0, total, self.batch_size):
            if i > 0 and self.batch_interval > 0:
                time.sleep(self.batch_interval)
            batch = targets[i:i + self.batch_size]
            summaries = self.summarize_batch([t.get("title", "") for t in batch])
            for j, title_data in enumerate(batch, start=1):
                summary = summaries.get(j, "")
                if summary:
                    title_data["ai_summary"] = summary
                    attached += 1

        if not quiet:
            print(f"[AI摘要] 已为 {attached}/{total} 条生成一句话摘要")
        return attached


def create_item_summarizer(
    config: Dict, quiet: bool = False
) -> Optional[AIItemSummarizer]:
    """
    从完整配置创建 AIItemSummarizer；未启用或缺 API Key 时返回 None

    Args:
        config: 完整配置（config["AI"] + config["AI_SUMMARY"]）
        quiet: 是否静默
    """
    summary_config = config.get("AI_SUMMARY", {})
    if not summary_config.get("ENABLED", False):
        return None

    ai_config = config.get("AI", {})
    if not ai_config.get("API_KEY", ""):
        if not quiet:
            print("[AI摘要] 未配置 AI API Key，跳过条目摘要")
        return None

    return AIItemSummarizer(ai_config, summary_config)
