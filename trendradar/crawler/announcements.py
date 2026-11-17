"""
上市公司公告采集模块

从公开数据接口拉取沪深京全市场最新公告，转换为 RSSItem（feed_id 固定为
"announcements"），并入 RSS 数据流复用词组过滤/存储/订阅源链路。

数据源（source 配置）：
- eastmoney（默认）：东方财富公告接口，一次拉取全市场最新公告，
  支持 stock_pool 按股票代码过滤（服务端过滤）
- cninfo：巨潮资讯（证监会指定披露平台），支持 categories 公告分类过滤，
  附带 PDF 原文链接；不支持股票代码过滤（需 orgId 映射，未实现）
- both：两个源都拉取，合并后按时间倒序截断

公开数据接口返回 JSON，无需鉴权；同花顺等行情站的公告页有动态 token 风控，
不作为数据源（其公告数据源头即巨潮/交易所）。
"""

from datetime import datetime, timezone as dt_timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

from trendradar.storage.base import RSSItem

FEED_ID = "announcements"           # 公告在 RSS 链路中的源 ID
FEED_NAME = "公司公告"               # 显示名称

EASTMONEY_API = "https://np-anotice-stock.eastmoney.com/api/security/ann"
EASTMONEY_DETAIL_URL = "https://data.eastmoney.com/notices/detail/{code}/{art_code}.html"

CNINFO_API = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_PDF_URL = "http://static.cninfo.com.cn/{path}"

# 东财接口单页上限 50；巨潮单页上限 30
EASTMONEY_MAX_PAGE = 50
CNINFO_MAX_PAGE = 30

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}
CNINFO_HEADERS = {
    **_HEADERS,
    "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}


def fetch_announcements(
    ann_config: Dict[str, Any],
    timezone: str = "Asia/Shanghai",
    timeout: int = 15,
    proxy_url: str = "",
) -> Tuple[List[RSSItem], List[str]]:
    """
    拉取上市公司公告并转换为 RSSItem 列表

    Args:
        ann_config: config["ANNOUNCEMENTS"] 配置
            （ENABLED/SOURCE/MAX_ITEMS/STOCK_POOL/CATEGORIES）
        timezone: IANA 时区名，用于公告时间显示
        timeout: 请求超时（秒）
        proxy_url: 代理地址（留空直连）

    Returns:
        (公告条目列表, 错误/警告信息列表)
    """
    source = str(ann_config.get("SOURCE", "eastmoney") or "eastmoney")
    max_items = _to_int(ann_config.get("MAX_ITEMS"), 30)
    stock_pool = list(ann_config.get("STOCK_POOL") or [])
    categories = list(ann_config.get("CATEGORIES") or [])

    if max_items <= 0:
        return [], []

    items: List[RSSItem] = []
    errors: List[str] = []

    sources = ["eastmoney", "cninfo"] if source == "both" else [source]
    for src in sources:
        try:
            if src == "eastmoney":
                src_items, err = _fetch_eastmoney(
                    max_items=max_items,
                    stock_pool=stock_pool,
                    timezone=timezone,
                    timeout=timeout,
                    proxy_url=proxy_url,
                )
            elif src == "cninfo":
                if stock_pool:
                    errors.append(
                        "cninfo 源不支持 stock_pool（需 orgId 映射），"
                        "如需按股票代码过滤请使用 eastmoney"
                    )
                src_items, err = _fetch_cninfo(
                    max_items=max_items,
                    categories=categories,
                    timezone=timezone,
                    timeout=timeout,
                    proxy_url=proxy_url,
                )
            else:
                src_items, err = [], f"未知数据源: {src}"
        except Exception as e:  # 单个源失败不影响另一个源
            src_items, err = [], f"抓取失败: {e}"

        if err:
            errors.append(err)
        items.extend(src_items)

    if len(sources) > 1:
        items = _sort_and_cap(items, max_items)

    return items, errors


def _fetch_eastmoney(
    max_items: int,
    stock_pool: List[str],
    timezone: str,
    timeout: int,
    proxy_url: str,
) -> Tuple[List[RSSItem], Optional[str]]:
    """东方财富公告接口：全市场最新公告，可按股票代码过滤"""
    params = {
        "sr": -1,
        "page_size": max(1, min(max_items, EASTMONEY_MAX_PAGE)),
        "page_index": 1,
        "ann_type": "A",
        "client_source": "web",
        "f_node": 0,
        "s_node": 0,
    }
    if stock_pool:
        params["stock_list"] = ",".join(stock_pool)

    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    resp = requests.get(
        EASTMONEY_API,
        params=params,
        timeout=timeout,
        headers=_HEADERS,
        proxies=proxies,
    )
    resp.raise_for_status()
    return _parse_eastmoney_payload(resp.json(), timezone, params["page_size"])


def _parse_eastmoney_payload(
    payload: Dict[str, Any],
    timezone: str,
    max_items: int,
) -> Tuple[List[RSSItem], Optional[str]]:
    """解析东财公告响应（纯函数，便于离线测试）"""
    data = (payload or {}).get("data") or {}
    raw_list = data.get("list") or []
    if not raw_list:
        return [], "东财公告接口返回空列表"

    items: List[RSSItem] = []
    for raw in raw_list[:max_items]:
        title = _clean_text(raw.get("title") or raw.get("title_ch"))
        if not title:
            continue

        codes = raw.get("codes") or []
        companies = []
        stock_code = ""
        for code_info in codes:
            name = _clean_text(code_info.get("short_name"))
            if name and name not in companies:
                companies.append(name)
            if not stock_code:
                stock_code = _clean_text(code_info.get("stock_code"))

        # 公告类型（如"股东大会资料"）作为摘要，公司名作为作者
        columns = raw.get("columns") or []
        ann_types = []
        for col in columns:
            col_name = _clean_text(col.get("column_name"))
            if col_name and col_name not in ann_types:
                ann_types.append(col_name)

        art_code = _clean_text(raw.get("art_code"))
        published_at = _iso_from_beijing(raw.get("display_time"), timezone)

        items.append(RSSItem(
            title=title,
            feed_id=FEED_ID,
            feed_name=FEED_NAME,
            url=EASTMONEY_DETAIL_URL.format(code=stock_code, art_code=art_code)
            if stock_code and art_code else "",
            guid=art_code,
            published_at=published_at,
            summary="/".join(ann_types),
            author="、".join(companies),
        ))

    if not items:
        return [], "东财公告接口未解析出有效条目"
    return items, None


def _fetch_cninfo(
    max_items: int,
    categories: List[str],
    timezone: str,
    timeout: int,
    proxy_url: str,
) -> Tuple[List[RSSItem], Optional[str]]:
    """巨潮资讯公告接口（沪深京全量，column=szse 覆盖全部市场）"""
    form = {
        "pageNum": 1,
        "pageSize": max(1, min(max_items, CNINFO_MAX_PAGE)),
        "column": "szse",
        "tabName": "fulltext",
        "seDate": "",
        "category": ";".join(categories) if categories else "",
        "isHLtitle": "true",
    }
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    resp = requests.post(
        CNINFO_API,
        data=form,
        timeout=timeout,
        headers=CNINFO_HEADERS,
        proxies=proxies,
    )
    resp.raise_for_status()
    return _parse_cninfo_payload(resp.json(), timezone, form["pageSize"])


def _parse_cninfo_payload(
    payload: Dict[str, Any],
    timezone: str,
    max_items: int,
) -> Tuple[List[RSSItem], Optional[str]]:
    """解析巨潮公告响应（纯函数，便于离线测试）"""
    raw_list = (payload or {}).get("announcements") or []
    if not raw_list:
        return [], "巨潮公告接口返回空列表"

    items: List[RSSItem] = []
    for raw in raw_list[:max_items]:
        title = _clean_text(raw.get("announcementTitle"))
        if not title:
            continue

        # 标题缺公司名时补前缀，避免推送里看不出所属公司
        sec_name = _clean_text(raw.get("secName"))
        if sec_name and sec_name not in title:
            title = f"{sec_name}：{title}"

        announcement_id = _clean_text(raw.get("announcementId"))
        adjunct = _clean_text(raw.get("adjunctUrl"))
        published_at = _iso_from_epoch_ms(raw.get("announcementTime"), timezone)

        items.append(RSSItem(
            title=title,
            feed_id=FEED_ID,
            feed_name=FEED_NAME,
            url=CNINFO_PDF_URL.format(path=adjunct) if adjunct else "",
            guid=announcement_id,
            published_at=published_at,
            summary="",
            author=sec_name,
        ))

    if not items:
        return [], "巨潮公告接口未解析出有效条目"
    return items, None


def _sort_and_cap(items: List[RSSItem], max_items: int) -> List[RSSItem]:
    """多源合并时按发布时间倒序并截断（ISO 字符串可直接比较）"""
    dated = [item for item in items if item.published_at]
    undated = [item for item in items if not item.published_at]
    dated.sort(key=lambda item: item.published_at, reverse=True)
    return (dated + undated)[:max_items]


def _iso_from_beijing(raw_time: Any, timezone: str) -> str:
    """
    东财时间（北京时间，如 "2026-11-16 17:11:48:404"）→ 配置时区的 ISO 字符串

    解析失败返回空字符串。
    """
    text = _clean_text(raw_time)
    if not text:
        return ""
    try:
        dt = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""
    return _to_iso(dt.replace(tzinfo=ZoneInfo("Asia/Shanghai")), timezone)


def _iso_from_epoch_ms(raw_ms: Any, timezone: str) -> str:
    """巨潮时间戳（毫秒 epoch，如 1794873873000）→ 配置时区的 ISO 字符串"""
    try:
        ms = int(raw_ms)
    except (TypeError, ValueError):
        return ""
    if ms <= 0:
        return ""
    dt = datetime.fromtimestamp(ms / 1000, tz=dt_timezone.utc)
    return _to_iso(dt, timezone)


def _to_iso(dt: datetime, timezone: str) -> str:
    """转换为配置时区的 ISO 字符串（时区非法时回退 Asia/Shanghai）"""
    try:
        tz = ZoneInfo(timezone)
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    return dt.astimezone(tz).isoformat(timespec="seconds")


def _to_int(value: Any, default: int) -> int:
    """宽松 int 转换（失败回退默认值）"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_text(value: Any) -> str:
    """去除首尾空白并压缩内部空白；非字符串转 str"""
    if value is None:
        return ""
    text = " ".join(str(value).split())
    return text.strip()
