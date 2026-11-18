# coding=utf-8
"""
报告辅助函数模块

提供报告生成相关的通用辅助函数
"""

import html
import re
from typing import Dict, List, Optional


def clean_title(title: str) -> str:
    """清理标题中的特殊字符

    清理规则：
    - 将换行符(\n, \r)替换为空格
    - 将多个连续空白字符合并为单个空格
    - 去除首尾空白

    Args:
        title: 原始标题字符串

    Returns:
        清理后的标题字符串
    """
    if not isinstance(title, str):
        title = str(title)
    cleaned_title = title.replace("\n", " ").replace("\r", " ")
    cleaned_title = re.sub(r"\s+", " ", cleaned_title)
    cleaned_title = cleaned_title.strip()
    return cleaned_title


def html_escape(text: str) -> str:
    """HTML特殊字符转义

    转义规则（按顺序）：
    - & → &amp;
    - < → &lt;
    - > → &gt;
    - " → &quot;
    - ' → &#x27;

    Args:
        text: 原始文本

    Returns:
        转义后的文本
    """
    if not isinstance(text, str):
        text = str(text)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def html_escape_text(text: str) -> str:
    """纯文本转义（仅 & < >，保留引号原样）

    用于摘要等已清洗为纯文本、不含属性语义的场景；
    严格转义（含引号）会让 ' 显示为 &#x27;，产生视觉噪音。
    """
    if not isinstance(text, str):
        text = str(text)
    return html.escape(text, quote=False)


# AI 相关标题匹配：缩写用词边界（避免命中 said/mail 等普通英文词），
# 中文关键词直接子串匹配；大小写不敏感
AI_TITLE_RE = re.compile(
    r"(\bAI\b|\bAGI\b|\bLLM\b|\bGPT\b|GPT-\d|ChatGPT|Claude|Gemini|OpenAI|DeepSeek|"
    r"Grok|Llama|Qwen|通义|文心|Kimi|豆包|Copilot|Midjourney|Sora|Manus|"
    r"Ollama|\bMCP\b|\bRAG\b|\bAgent\b|智能体|大模型|人工智能|机器学习|深度学习|"
    r"神经网络|算力|生成式|多模态)",
    re.IGNORECASE,
)


def is_ai_related(text: str) -> bool:
    """判断标题/文本是否与 AI 相关（用于报告 AI 新闻 tab）"""
    if not isinstance(text, str) or not text:
        return False
    return bool(AI_TITLE_RE.search(text))


def calculate_rank_trend(rank_timeline=None, ranks=None):
    """根据排名时间线或排名列表计算趋势方向

    Args:
        rank_timeline: 按时间顺序的排名记录列表，如 [{"time": "10:00", "rank": 5}, ...]
        ranks: 排名列表

    Returns:
        "up" (排名上升/数值变小), "down" (排名下降/数值变大), 或 None
    """
    prev_rank = None
    curr_rank = None

    if rank_timeline:
        valid_ranks = [r["rank"] for r in rank_timeline if r.get("rank") is not None]
        if len(valid_ranks) >= 2:
            prev_rank = valid_ranks[-2]
            curr_rank = valid_ranks[-1]
    elif ranks and len(ranks) >= 2:
        prev_rank = ranks[-2]
        curr_rank = ranks[-1]

    if prev_rank is not None and curr_rank is not None:
        if curr_rank < prev_rank:
            return "up"
        elif curr_rank > prev_rank:
            return "down"
    return None


def format_rank_display(
    ranks: List[int],
    rank_threshold: int,
    format_type: str,
    rank_timeline: Optional[List[Dict]] = None,
) -> str:
    """格式化排名显示

    根据不同平台类型生成对应格式的排名字符串。
    当最小排名小于等于阈值时，使用高亮格式。

    Args:
        ranks: 排名列表（去重后的唯一值，用于范围显示）
        rank_threshold: 高亮阈值，小于等于此值的排名会高亮显示
        format_type: 平台类型，支持:
            - "html": HTML格式
            - "feishu": 飞书格式
            - "dingtalk": 钉钉格式
            - "wework": 企业微信格式
            - "telegram": Telegram格式
            - "slack": Slack格式
            - 其他: 默认markdown格式
        rank_timeline: 按时间顺序的排名记录列表（可选，用于计算趋势）

    Returns:
        格式化后的排名字符串，如 "[1]" 或 "[1 - 5]"
        如果排名列表为空，返回空字符串
    """
    if not ranks:
        return ""

    unique_ranks = sorted(set(ranks))
    min_rank = unique_ranks[0]
    max_rank = unique_ranks[-1]

    # 根据平台类型选择高亮格式
    if format_type == "html":
        highlight_start = "<font color='red'><strong>"
        highlight_end = "</strong></font>"
    elif format_type == "feishu":
        highlight_start = "<font color='red'>**"
        highlight_end = "**</font>"
    elif format_type == "dingtalk":
        highlight_start = "**"
        highlight_end = "**"
    elif format_type == "wework":
        highlight_start = "**"
        highlight_end = "**"
    elif format_type == "telegram":
        highlight_start = "<b>"
        highlight_end = "</b>"
    elif format_type == "slack":
        highlight_start = "*"
        highlight_end = "*"
    else:
        # 默认 markdown 格式
        highlight_start = "**"
        highlight_end = "**"

    # 生成排名显示
    rank_str = ""
    if min_rank <= rank_threshold:
        if min_rank == max_rank:
            rank_str = f"{highlight_start}[{min_rank}]{highlight_end}"
        else:
            rank_str = f"{highlight_start}[{min_rank} - {max_rank}]{highlight_end}"
    else:
        if min_rank == max_rank:
            rank_str = f"[{min_rank}]"
        else:
            rank_str = f"[{min_rank} - {max_rank}]"

    trend = calculate_rank_trend(rank_timeline, ranks)
    trend_arrow = {"up": "📈", "down": "📉"}.get(trend, "")

    return f"{rank_str} {trend_arrow}" if trend_arrow else rank_str
