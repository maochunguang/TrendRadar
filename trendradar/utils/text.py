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
# 匹配纯链接行（如 HN RSS 的 "Article URL: https://..."），无正文价值
_LINK_ONLY_LINE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 ]{0,20}:\s*https?://\S+\s*$")
# 匹配整行只有一个 URL 的行（HTML 标签剥离后链接常独占一行）
_BARE_URL_LINE_RE = re.compile(r"^https?://\S+\s*$")
# 匹配纯数字元数据行（如 HN RSS 的 "Points: 128"、"# Comments: 30"）
_METADATA_LINE_RE = re.compile(r"^#?\s*(points|score|votes|comments)\s*:\s*\d+\s*$", re.IGNORECASE)
# 匹配悬空标签行（URL 行被过滤后剩下的 "Article URL:" 之类）
_DANGLING_LABEL_RE = re.compile(r"^[A-Za-z#][A-Za-z0-9 #]{0,20}:$")
_PUNCT_ONLY_RE = re.compile(r"^[\s|·，,、;；.。]*$")


def clean_summary_text(text: Optional[str], max_length: int = 100) -> str:
    """
    清洗摘要文本：去除 HTML 标签/脚本样式、反转义实体、
    丢弃纯链接行（HN RSS 的 Article URL: ... 等）、压缩空白、截断

    Args:
        text: 原始摘要（RSS description 等，可能含 HTML）
        max_length: 最大字符数，0 表示不截断

    Returns:
        清洗后的纯文本摘要（可能为空字符串）
    """
    if not text:
        return ""

    # 去掉 script/style 整块，避免残留 JS/CSS 文本；
    # 标签替换为换行以保留块级结构（供逐行过滤），空白随后统一压缩
    cleaned = _SCRIPT_STYLE_RE.sub(" ", text)
    cleaned = _TAG_RE.sub("\n", cleaned)
    cleaned = html.unescape(cleaned)

    # 逐行过滤纯链接行与纯数字元数据行，再压缩空白
    kept_lines = []
    for line in cleaned.splitlines():
        line = line.strip()
        if not line or _PUNCT_ONLY_RE.match(line):
            continue
        if _LINK_ONLY_LINE_RE.match(line):
            continue
        if _BARE_URL_LINE_RE.match(line):
            continue
        if _METADATA_LINE_RE.match(line):
            continue
        if _DANGLING_LABEL_RE.match(line):
            continue
        kept_lines.append(line)
    cleaned = _WHITESPACE_RE.sub(" ", " ".join(kept_lines)).strip()

    if max_length > 0:
        if not cleaned:
            return ""
        if len(cleaned) > max_length:
            cleaned = cleaned[:max_length].rstrip() + "…"

    return cleaned
