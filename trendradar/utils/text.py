# coding=utf-8
"""
文本清洗工具

用于 RSS 摘要等富文本字段的净化与截断。
"""

import html
import re
from typing import Optional

# 匹配 HTML 标签（含脚本/样式整块）
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_WHITESPACE_RE = re.compile(r"\s+")


def clean_summary_text(text: Optional[str], max_length: int = 100) -> str:
    """
    清洗摘要文本：去除 HTML 标签/脚本样式、反转义实体、压缩空白、截断

    Args:
        text: 原始摘要（RSS description 等，可能含 HTML）
        max_length: 最大字符数，0 表示不截断

    Returns:
        清洗后的纯文本摘要（可能为空字符串）
    """
    if not text:
        return ""

    # 去掉 script/style 整块，避免残留 JS/CSS 文本
    cleaned = _SCRIPT_STYLE_RE.sub(" ", text)
    cleaned = _TAG_RE.sub(" ", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()

    if max_length > 0 and len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip() + "…"

    return cleaned
