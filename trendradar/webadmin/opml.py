# coding=utf-8
"""
OPML 导入支持

解析标准 OPML 格式的 RSS 订阅列表（Inoreader/Feedly/Reeder 等导出格式），
转换为 TrendRadar 的 rss.feeds 配置项。
"""

import re
import xml.etree.ElementTree as ET
from typing import Dict, List
from urllib.parse import urlparse


class OpmlError(ValueError):
    """OPML 解析失败"""


def parse_opml(content: str) -> List[Dict[str, str]]:
    """
    解析 OPML 内容，返回 [{"name": 显示名, "url": 订阅地址}]

    - 仅收录带 xmlUrl 的 outline（嵌套分组自动展开）
    - 名称取 title/text 属性，缺失时用域名兜底
    - url 必须是 http(s)

    Raises:
        OpmlError: XML 非法或没有可导入的订阅
    """
    if not content or not content.strip():
        raise OpmlError("OPML 内容为空")

    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        raise OpmlError(f"OPML 格式错误: {e}")

    feeds: List[Dict[str, str]] = []
    seen_urls: set = set()

    body = root.find("body")
    if body is None:
        raise OpmlError("OPML 缺少 body 节点")

    def walk(node):
        for outline in node.findall("outline"):
            xml_url = (outline.get("xmlUrl") or "").strip()
            if xml_url:
                if not re.match(r"^https?://", xml_url, re.IGNORECASE):
                    xml_url = None
            if xml_url and xml_url not in seen_urls:
                seen_urls.add(xml_url)
                name = (outline.get("title") or outline.get("text") or "").strip()
                if not name:
                    parsed = urlparse(xml_url)
                    name = parsed.netloc or xml_url
                feeds.append({"name": name, "url": xml_url})
            # 嵌套分组（无 xmlUrl 的 outline）继续下钻
            walk(outline)

    walk(body)

    if not feeds:
        raise OpmlError("OPML 中没有找到带订阅地址的条目（xmlUrl）")

    return feeds


def make_feed_id(name: str, existing_ids: List[str]) -> str:
    """由名称生成唯一且合法的 feed id（小写字母数字连字符）"""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    # 中文名等无法生成可读 slug 时，退化为 feed-N
    if len(slug) < 2:
        slug = f"feed-{len(existing_ids) + 1}"
    if not slug[0].isalpha():
        slug = "rss-" + slug
    if len(slug) > 40:
        slug = slug[:40].rstrip("-")

    taken = set(existing_ids)
    candidate = slug
    suffix = 2
    while candidate in taken:
        candidate = f"{slug}-{suffix}"
        suffix += 1
    return candidate
