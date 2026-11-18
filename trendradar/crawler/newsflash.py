# coding=utf-8
"""
同花顺 7×24 财经快讯采集模块

从同花顺公开快讯接口拉取实时财经快讯，转换为 RSSItem
（feed_id 固定为 "newsflash"），并入 RSS 数据流复用词组过滤/
存储/订阅源链路。每条快讯自带 digest 摘要，适合直接阅读。

接口无鉴权；同花顺页面本身有动态 token 风控，但该快讯接口可直连。
"""

from datetime import datetime, timezone as dt_timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

from trendradar.storage.base import RSSItem

FEED_ID = "newsflash"
FEED_NAME = "同花顺快讯"

NEWSFLASH_API = "https://news.10jqka.com.cn/tapp/news/push/stock/"
# 单页上限（接口实测支持 100）
MAX_PAGE = 100

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}


def fetch_newsflash(
    cfg: Dict[str, Any],
    timezone: str = "Asia/Shanghai",
    timeout: int = 15,
    proxy_url: str = "",
) -> Tuple[List[RSSItem], List[str]]:
    """
    拉取同花顺 7×24 快讯并转换为 RSSItem 列表

    Args:
        cfg: config["NEWSFLASH"] 配置（ENABLED/MAX_ITEMS）
        timezone: IANA 时区名，用于快讯时间显示
        timeout: 请求超时（秒）
        proxy_url: 代理地址（调用方已按 USE_PROXY 门控）

    Returns:
        (快讯条目列表, 错误信息列表)
    """
    max_items = _to_int(cfg.get("MAX_ITEMS"), 30)
    if max_items <= 0:
        return [], []

    params = {
        "page": 1,
        "pagesize": max(1, min(max_items, MAX_PAGE)),
        "tag": "",
        "track": "website",
        "datatype": "page",
    }
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    try:
        resp = requests.get(
            NEWSFLASH_API,
            params=params,
            timeout=timeout,
            headers=_HEADERS,
            proxies=proxies,
        )
        resp.raise_for_status()
        items, err = _parse_payload(resp.json(), timezone, params["pagesize"])
        return items, ([err] if err else [])
    except Exception as e:
        return [], [f"抓取失败: {e}"]


def _parse_payload(
    payload: Dict[str, Any],
    timezone: str,
    max_items: int,
) -> Tuple[List[RSSItem], Optional[str]]:
    """解析同花顺快讯响应（纯函数，便于离线测试）"""
    data = (payload or {}).get("data") or {}
    raw_list = data.get("list") or []
    if not raw_list:
        return [], "同花顺快讯接口返回空列表"

    items: List[RSSItem] = []
    for raw in raw_list[:max_items]:
        title = _clean_text(raw.get("title"))
        if not title:
            continue

        # digest 是快讯正文摘要（长文本），原样入库，展示层按 rss.summary 处理
        digest = _clean_text(raw.get("digest"))
        url = _clean_text(raw.get("url"))
        tags = raw.get("tags") or []
        author = "、".join(
            _clean_text(t.get("name")) for t in tags if _clean_text(t.get("name"))
        )

        items.append(RSSItem(
            title=title,
            feed_id=FEED_ID,
            feed_name=FEED_NAME,
            url=url,
            guid=_clean_text(raw.get("id")),
            published_at=_iso_from_epoch(raw.get("ctime"), timezone),
            summary=digest,
            author=author,
        ))

    if not items:
        return [], "同花顺快讯接口未解析出有效条目"
    return items, None


def _iso_from_epoch(raw_ts: Any, timezone: str) -> str:
    """epoch 秒（同花顺 ctime）→ 配置时区 ISO 字符串"""
    try:
        ts = int(raw_ts)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    dt = datetime.fromtimestamp(ts, tz=dt_timezone.utc)
    try:
        tz = ZoneInfo(timezone)
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    return dt.astimezone(tz).isoformat(timespec="seconds")


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()
