# coding=utf-8
"""
RSS 全文提取模块

当 RSS 源未提供摘要时，抓取文章页面提取正文首段作为摘要兜底
（rss.summary.fulltext_fallback=true 时启用）。

设计约束：
- 每次运行最多抓取 MAX_PER_RUN 篇，避免拖慢任务
- 同一 URL 结果缓存，raw/new 两次转换不重复抓取
- 任何失败静默返回空串，不影响主流程
"""

import re
from html.parser import HTMLParser
from typing import Optional

import requests

from trendradar.utils.text import clean_summary_text

# 每次运行最多抓取的篇数
MAX_PER_RUN = 20

# 请求超时（秒）
DEFAULT_TIMEOUT = 10

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# 跳过整块的标签
_SKIP_TAGS = {"script", "style", "noscript", "svg", "template"}
# 段落级标签（触发换段）
_BLOCK_TAGS = {"p", "div", "article", "li", "h1", "h2", "h3", "h4", "section", "blockquote"}


class _TextExtractor(HTMLParser):
    """从 HTML 中提取段落文本（跳过 script/style 等整块）"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._buf: list = []
        self.paragraphs: list = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self._buf.append(data)

    def _flush(self):
        text = "".join(self._buf).strip()
        if text:
            self.paragraphs.append(text)
        self._buf = []

    def get_text(self) -> str:
        self._flush()
        return " ".join(self.paragraphs)


class FulltextFetcher:
    """带缓存与数量上限的原文摘要抓取器"""

    def __init__(self, timeout: int = DEFAULT_TIMEOUT, proxy_url: Optional[str] = None):
        self.timeout = timeout
        self.proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        self._cache: dict = {}
        self._fetched = 0

    def get_summary(self, url: str, max_length: int = 100) -> str:
        """
        获取文章摘要：优先命中缓存；超过运行上限或抓取失败返回空串

        Args:
            url: 文章链接
            max_length: 摘要最大长度（字符）
        """
        if not url:
            return ""
        # 缓存命中先返回（不消耗每轮预算，raw/主区两次转换复用同一结果）
        if url in self._cache:
            return self._cache[url]
        if self._fetched >= MAX_PER_RUN:
            return ""

        self._fetched += 1
        text = ""
        try:
            resp = requests.get(
                url,
                timeout=self.timeout,
                headers={"User-Agent": _USER_AGENT},
                proxies=self.proxies,
                stream=True,
            )
            # 只读前 64KB，避免下载大页面
            content = b""
            for chunk in resp.iter_content(chunk_size=8192):
                content += chunk
                if len(content) >= 65536:
                    break
            resp.close()

            encoding = resp.encoding or "utf-8"
            html_text = content.decode(encoding, errors="replace")
            extractor = _TextExtractor()
            extractor.feed(html_text)
            text = extractor.get_text()
            # 压缩空白（段落以空格连接）
            text = re.sub(r"\s+", " ", text).strip()
        except Exception:
            text = ""

        summary = clean_summary_text(text, max_length) if text else ""
        self._cache[url] = summary
        return summary
