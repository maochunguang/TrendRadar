# coding=utf-8
"""
新闻数据服务

把 TrendRadar 每日抓取的新闻（热榜 + RSS）以标准格式对外提供：
- RSS 2.0 订阅源（/feed/*.xml）：任何 RSS 阅读器/系统可直接订阅
- JSON 查询 API（/api/news/*）：程序化拉取（如 daily_stock_analysis）

数据来源：output/news/{date}.db 与 output/rss/{date}.db（只读打开）。
所有查询均为只读，不写库。
"""

import glob
import re
import sqlite3
from datetime import datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET

from trendradar.webadmin.config_io import get_output_dir

# 日期参数白名单（防路径穿越）
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 单次返回的最大条目数
MAX_ITEMS = 500
DEFAULT_LIMIT = 200

# 默认覆盖的天数（feed 与 days 参数的上限）
MAX_DAYS = 30


def _news_dir() -> Path:
    return get_output_dir() / "news"


def _rss_dir() -> Path:
    return get_output_dir() / "rss"


def _open_ro(path: Path) -> sqlite3.Connection:
    """只读打开 SQLite（mode=ro），不存在时抛 FileNotFoundError"""
    if not path.exists():
        raise FileNotFoundError(f"数据库不存在: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _safe_date(date: str) -> Optional[str]:
    return date if _DATE_RE.match(date) else None


def list_news_dates() -> Dict[str, List[str]]:
    """列出有数据的日期（热榜与 RSS 分别列出，倒序）"""
    hotlist = sorted(
        (Path(p).stem for p in glob.glob(str(_news_dir() / "*.db")) if _DATE_RE.match(Path(p).stem)),
        reverse=True,
    )
    rss = sorted(
        (Path(p).stem for p in glob.glob(str(_rss_dir() / "*.db")) if _DATE_RE.match(Path(p).stem)),
        reverse=True,
    )
    return {"hotlist": hotlist, "rss": rss}


def _recent_dates(available: List[str], days: int) -> List[str]:
    days = max(1, min(days, MAX_DAYS))
    return available[:days]


def get_hotlist(date: Optional[str] = None, days: int = 1) -> List[Dict[str, Any]]:
    """
    读取热榜条目（含平台名与排名）

    Returns:
        [{title, url, mobile_url, source, platform_id, rank, first_time, last_time, count, date}]
    """
    dates = list_news_dates()["hotlist"]
    if date:
        date = _safe_date(date)
        if not date:
            return []
        dates = [date] if date in dates else []
    else:
        dates = _recent_dates(dates, days)

    items: List[Dict[str, Any]] = []
    for d in dates:
        try:
            conn = _open_ro(_news_dir() / f"{d}.db")
        except FileNotFoundError:
            continue
        try:
            rows = conn.execute(
                """
                SELECT n.title, n.url, n.mobile_url, n.rank,
                       n.first_crawl_time, n.last_crawl_time, n.crawl_count,
                       p.id AS platform_id, p.name AS platform_name
                FROM news_items n
                LEFT JOIN platforms p ON p.id = n.platform_id
                ORDER BY n.rank ASC
                """
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        finally:
            conn.close()

        for row in rows:
            items.append({
                "title": row["title"],
                "url": row["url"] or "",
                "mobile_url": row["mobile_url"] or "",
                "source": row["platform_name"] or row["platform_id"] or "热榜",
                "platform_id": row["platform_id"] or "",
                "rank": row["rank"],
                "first_time": row["first_crawl_time"] or "",
                "last_time": row["last_crawl_time"] or "",
                "count": row["crawl_count"] or 1,
                "date": d,
            })
    return items


def get_rss_items(date: Optional[str] = None, days: int = 1, feed_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    读取 RSS 条目（含摘要）

    Returns:
        [{title, url, source, feed_id, published_at, summary, author, first_time, last_time, date}]
    """
    dates = list_news_dates()["rss"]
    if date:
        date = _safe_date(date)
        if not date:
            return []
        dates = [date] if date in dates else []
    else:
        dates = _recent_dates(dates, days)

    items: List[Dict[str, Any]] = []
    for d in dates:
        try:
            conn = _open_ro(_rss_dir() / f"{d}.db")
        except FileNotFoundError:
            continue
        try:
            if feed_id:
                rows = conn.execute(
                    """
                    SELECT title, url, feed_id, published_at, summary, author,
                           first_crawl_time, last_crawl_time
                    FROM rss_items WHERE feed_id = ?
                    ORDER BY published_at DESC
                    """,
                    (feed_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT title, url, feed_id, published_at, summary, author,
                           first_crawl_time, last_crawl_time
                    FROM rss_items
                    ORDER BY published_at DESC
                    """
                ).fetchall()
        except sqlite3.OperationalError:
            continue
        finally:
            conn.close()

        for row in rows:
            items.append({
                "title": row["title"],
                "url": row["url"] or "",
                "source": f"RSS/{row['feed_id']}",
                "feed_id": row["feed_id"] or "",
                "published_at": row["published_at"] or "",
                "summary": row["summary"] or "",
                "author": row["author"] or "",
                "date": d,
            })
    return items


def search_news(query: str, days: int = 7, limit: int = DEFAULT_LIMIT) -> List[Dict[str, Any]]:
    """
    在最近 N 天的热榜与 RSS 数据中搜索标题/摘要（LIKE 匹配）
    """
    query = (query or "").strip()
    if not query:
        return []
    like = f"%{query}%"

    results: List[Dict[str, Any]] = []
    for item in get_hotlist(days=days):
        if query.lower() in item["title"].lower():
            item["source_type"] = "hotlist"
            results.append(item)
    for item in get_rss_items(days=days):
        haystack = (item["title"] + " " + item["summary"]).lower()
        if query.lower() in haystack:
            item["source_type"] = "rss"
            results.append(item)
    return results[:max(1, min(limit, MAX_ITEMS))]


# ═══════════════════════════════════════════════════════
# RSS 2.0 订阅源构建
# ═══════════════════════════════════════════════════════


def _parse_time(value: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value[:19], fmt)
        except (ValueError, TypeError):
            continue
    return None


def build_rss_feed(
    title: str,
    link: str,
    description: str,
    items: List[Dict[str, Any]],
    max_items: int = DEFAULT_LIMIT,
) -> str:
    """
    构建 RSS 2.0 XML

    每个条目：title / link / guid / pubDate / description。
    时间取 last_time（热榜）或 published_at（RSS），统一转 RFC 822。
    """
    limit = max(1, min(max_items, MAX_ITEMS))
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = title
    ET.SubElement(channel, "link").text = link
    ET.SubElement(channel, "description").text = description
    ET.SubElement(channel, "generator").text = "TrendRadar"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now())

    for item in items[:limit]:
        entry = ET.SubElement(channel, "item")
        ET.SubElement(entry, "title").text = item.get("title", "") or "(无标题)"
        link_url = item.get("url") or item.get("mobile_url") or ""
        if link_url:
            ET.SubElement(entry, "link").text = link_url
        # guid：优先 url，退化为 平台+标题+日期 组合
        guid = link_url or f"trendradar:{item.get('source', '')}:{item.get('date', '')}:{item.get('title', '')}"
        ET.SubElement(entry, "guid").text = guid

        time_value = item.get("last_time") or item.get("published_at") or ""
        parsed = _parse_time(time_value)
        if parsed is None and item.get("date"):
            # 时间戳缺失/格式异常时，至少落到数据日期（DSA 等消费方要求带日期）
            parsed = _parse_time(item["date"])
        ET.SubElement(entry, "pubDate").text = format_datetime(parsed or datetime.now())

        description_parts = []
        if "rank" in item:
            # 热榜条目：描述里给出平台与排名语境
            rank_note = f"【{item.get('source', '')} 第{item.get('rank', '?')}名"
            count = item.get("count", 1)
            if isinstance(count, (int, float)) and count > 1:
                rank_note += f"·在榜{int(count)}次"
            rank_note += "】"
            description_parts.append(rank_note)
        summary = (item.get("summary") or "").strip()
        if summary:
            description_parts.append(summary)
        ET.SubElement(entry, "description").text = " ".join(description_parts) or link_url

    return ET.tostring(rss, encoding="unicode", xml_declaration=True)
