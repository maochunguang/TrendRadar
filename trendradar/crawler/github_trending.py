"""
GitHub 榜单采集模块（Trending 日/周/月 + AI 新星）

采集四类榜单，转换为 RSSItem 并入 RSS 数据流（复用词组过滤/存储/订阅源链路），
同时返回结构化数据供独立 GitHub 页面（output/html/github.html）渲染：

- github-daily / github-weekly / github-monthly：GitHub Trending 榜
  数据源优先直连 github.com/trending/{since}（结构化字段最全），
  失败时回退自建 RSSHub 的 /github/trending/{since} 路由
  （github.com 在国内网络下间歇性不可达，RSSHub 有服务端缓存与重试）
- github-ai：AI 热门新星榜
  走 api.github.com Search API，按话题（topic:ai/llm/...）搜近 N 天
  新建仓库按星数排序；api.github.com 可达性通常优于网页端

GitHub 官方没有 Trending API，网页抓取是唯一忠实来源；Search API 的
"近 N 天新建 + 星数排序" 是社区公认的新星榜近似。
"""

import re
from datetime import datetime, timedelta, timezone as dt_timezone
from html import unescape as _html_unescape
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo
from xml.sax.saxutils import unescape as _xml_unescape

import feedparser
import requests

from trendradar.storage.base import RSSItem

TRENDING_BASE_URL = "https://github.com/trending/{since}"
RSSHUB_TRENDING_PATH = "/github/trending/{since}"
SEARCH_API_URL = "https://api.github.com/search/repositories"

# 榜单配置：feed_id、显示名、周期星数的文案（如 "今日 +2,322"）
TRENDING_FEEDS = {
    "daily": ("github-daily", "GitHub 日榜", "今日"),
    "weekly": ("github-weekly", "GitHub 周榜", "本周"),
    "monthly": ("github-monthly", "GitHub 月榜", "本月"),
}

AI_FEED_ID = "github-ai"
AI_FEED_NAME = "GitHub AI 新星"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_API_HEADERS = {
    "User-Agent": _HEADERS["User-Agent"],
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# trending 页面结构（article.Box-row，2026-11 实测 20/20 命中）
_TRENDING_ROW_SPLIT = '<article class="Box-row">'
_REPO_RE = re.compile(r'<h2[^>]*>\s*<a[^>]*href="/([^"]+?)"', re.S)
_DESC_RE = re.compile(r'<p class="col-9[^"]*"[^>]*>\s*(.*?)\s*</p>', re.S)
_LANG_RE = re.compile(r'itemprop="programmingLanguage">([^<]+)<')
_TOTAL_RE = re.compile(r'href="/[^"]+/stargazers".*?</svg>\s*([\d,]+)', re.S)
_PERIOD_RE = re.compile(r'([\d,]+)\s+stars\s+(today|this week|this month)')


def fetch_github(
    gh_config: Dict[str, Any],
    timezone: str = "Asia/Shanghai",
    timeout: int = 15,
    proxy_url: str = "",
) -> Tuple[Dict[str, List[RSSItem]], Dict[str, List[dict]], List[str]]:
    """
    采集 GitHub 四类榜单

    Args:
        gh_config: config["GITHUB"] 配置
            （ENABLED/TOP_N/PERIODS/AI_TOPICS/AI_DAYS/RSSHUB_BASE/TOKEN）
        timezone: IANA 时区名，用于时间显示
        timeout: 请求超时（秒）
        proxy_url: 代理地址（留空直连）

    Returns:
        (rss_items_by_feed, page_data_by_feed, errors) 三元组：
        - rss_items_by_feed: {feed_id: [RSSItem, ...]}，并入 RSS 数据流
        - page_data_by_feed: {feed_id: [结构化榜单条目]}，供 GitHub 页面渲染
        - errors: 错误/警告信息列表（单个榜单失败不影响其他榜单）
    """
    if not gh_config.get("ENABLED"):
        return {}, {}, []

    top_n = _to_int(gh_config.get("TOP_N"), 10)
    if top_n <= 0:
        return {}, {}, []

    items_by_feed: Dict[str, List[RSSItem]] = {}
    page_data: Dict[str, List[dict]] = {}
    errors: List[str] = []

    # ── Trending 日/周/月榜 ──
    rsshub_base = str(gh_config.get("RSSHUB_BASE") or "").strip().rstrip("/")
    for period in gh_config.get("PERIODS") or ["daily"]:
        feed_id, feed_name, _ = TRENDING_FEEDS.get(period, (f"github-{period}", f"GitHub {period}", period))
        try:
            repos, err = _fetch_trending_period(
                period, top_n, rsshub_base, str(gh_config.get("TOKEN") or ""), timeout, proxy_url,
            )
        except Exception as e:
            repos, err = [], f"{feed_name}抓取失败: {e}"
        if err:
            errors.append(err)
        if repos:
            now_iso = _to_iso(datetime.now(tz=dt_timezone.utc), timezone)
            rss_list = []
            for rank, repo in enumerate(repos, 1):
                rss_list.append(_build_rss_item(
                    repo, feed_id, feed_name, timezone, period,
                    published_at=repo.get("published_at") or now_iso,
                    rank=rank,
                ))
            items_by_feed[feed_id] = rss_list
            page_data[feed_id] = [_page_entry(repo, rank) for rank, repo in enumerate(repos, 1)]

    # ── AI 新星榜（Search API）──
    topics = [t for t in (gh_config.get("AI_TOPICS") or []) if t]
    if topics:
        try:
            repos, err = _fetch_ai_repos(
                topics,
                days=_to_int(gh_config.get("AI_DAYS"), 30),
                top_n=top_n,
                token=str(gh_config.get("TOKEN") or ""),
                timeout=timeout,
                proxy_url=proxy_url,
            )
        except Exception as e:
            repos, err = [], f"AI 新星榜抓取失败: {e}"
        if err:
            errors.append(err)
        if repos:
            rss_list = [
                _build_rss_item(repo, AI_FEED_ID, AI_FEED_NAME, timezone, "", rank=rank)
                for rank, repo in enumerate(repos, 1)
            ]
            items_by_feed[AI_FEED_ID] = rss_list
            page_data[AI_FEED_ID] = [_page_entry(repo, rank) for rank, repo in enumerate(repos, 1)]

    return items_by_feed, page_data, errors


# 第三级降级：官方 trending 不可达（github.com 被墙）时，
# 用 Search API "近 N 天新建仓库按星数" 近似对应周期
TRENDING_FALLBACK_DAYS = {"daily": 1, "weekly": 7, "monthly": 30}


def _fetch_trending_period(
    period: str,
    top_n: int,
    rsshub_base: str,
    token: str,
    timeout: int,
    proxy_url: str,
) -> Tuple[List[dict], Optional[str]]:
    """抓取单个周期的 Trending 榜：直连 → RSSHub 回退 → Search API 近似降级"""
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    # 一级：直连（github.com 国内网络下经常不可达，重试一次）
    last_err = ""
    for attempt in range(2):
        if attempt:
            import time
            time.sleep(1)
        try:
            resp = requests.get(
                TRENDING_BASE_URL.format(since=period),
                timeout=timeout,
                headers=_HEADERS,
                proxies=proxies,
            )
            resp.raise_for_status()
            repos = _parse_trending_html(resp.text, top_n)
            if repos:
                for repo in repos:
                    repo["source"] = "trending"
                return repos, None
            last_err = "页面未解析出条目（页面结构可能变化）"
        except Exception as e:
            last_err = str(e)

    # 二级：回退自建 RSSHub（路由本身也依赖 RSSHub 服务端能访问 github.com）
    if rsshub_base:
        try:
            resp = requests.get(
                rsshub_base + RSSHUB_TRENDING_PATH.format(since=period),
                timeout=timeout,
                headers=_HEADERS,
                proxies=proxies,
            )
            resp.raise_for_status()
            repos = _parse_rsshub_text(resp.text, top_n)
            if repos:
                for repo in repos:
                    repo["source"] = "rsshub"
                return repos, None
            rsshub_err = "RSSHub 未解析出条目"
        except Exception as e:
            rsshub_err = str(e)
    else:
        rsshub_err = "未配置 RSSHub"

    # 三级：Search API 近似榜（api.github.com 可达性远好于网页端）
    days = TRENDING_FALLBACK_DAYS.get(period, 7)
    try:
        repos, search_err = _fetch_search_new_repos(
            days=days, top_n=top_n, token=token, timeout=timeout, proxy_url=proxy_url,
        )
        if repos:
            for repo in repos:
                repo["source"] = "search"
            return repos, (
                f"Trending {period} 官方榜不可达（直连: {last_err}；RSSHub: {rsshub_err}），"
                f"已降级为 Search API 近似榜（近 {days} 天新建仓库按星数）"
            )
        return [], f"Trending {period} 全部通道失败（直连: {last_err}；RSSHub: {rsshub_err}；Search: {search_err}）"
    except Exception as e:
        return [], f"Trending {period} 全部通道失败（直连: {last_err}；RSSHub: {rsshub_err}；Search: {e}）"


def _fetch_search_new_repos(
    days: int,
    top_n: int,
    token: str,
    timeout: int,
    proxy_url: str,
) -> Tuple[List[dict], Optional[str]]:
    """Search API 新建仓库榜：created:>N 天前 按星数排序（trending 降级通道）"""
    cutoff = (datetime.now(tz=dt_timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    query = f"created:>{cutoff} stars:>0"
    return _search_repos(query, top_n, token, timeout, proxy_url)


def _fetch_ai_repos(
    topics: List[str],
    days: int,
    top_n: int,
    token: str,
    timeout: int,
    proxy_url: str,
) -> Tuple[List[dict], Optional[str]]:
    """按话题搜近 N 天新建仓库（每话题一次查询），合并去重按星数取 Top N"""
    cutoff = (datetime.now(tz=dt_timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

    per_topic: List[List[dict]] = []
    failed_topics: List[str] = []
    for topic in topics:
        try:
            repos, _ = _search_repos(
                f"topic:{topic} created:>{cutoff}", top_n, token, timeout, proxy_url,
            )
            per_topic.append(repos)
        except Exception as e:
            failed_topics.append(f"{topic}: {e}")

    if not per_topic:
        return [], "AI 新星榜全部话题查询失败: " + "; ".join(failed_topics)

    repos = _merge_ai_repos(per_topic, top_n)
    if not repos:
        return [], "AI 新星榜未解析出条目"

    err = "; ".join(failed_topics) if failed_topics else None
    return repos, err


def _search_repos(
    query: str,
    top_n: int,
    token: str,
    timeout: int,
    proxy_url: str,
) -> Tuple[List[dict], Optional[str]]:
    """调用 GitHub Search API（repositories），返回解析后的仓库列表"""
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    headers = dict(_API_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = requests.get(
        SEARCH_API_URL,
        params={
            "q": query,
            "sort": "stars",
            "order": "desc",
            "per_page": min(top_n, 100),
        },
        timeout=timeout,
        headers=headers,
        proxies=proxies,
    )
    resp.raise_for_status()
    return _parse_search_payload(resp.json(), top_n), None


def _parse_trending_html(html: str, top_n: int) -> List[dict]:
    """解析 trending 页 HTML（纯函数，便于离线测试）"""
    if not html:
        return []
    repos: List[dict] = []
    for block in html.split(_TRENDING_ROW_SPLIT)[1:]:
        body = block.split("</article>")[0]

        repo_match = _REPO_RE.search(body)
        if not repo_match:
            continue
        repo = _clean_text(repo_match.group(1))
        if "/" not in repo:
            continue

        desc = ""
        desc_match = _DESC_RE.search(body)
        if desc_match:
            desc = _clean_text(_html_unescape(re.sub(r"<[^>]+>", "", desc_match.group(1))))

        language = ""
        lang_match = _LANG_RE.search(body)
        if lang_match:
            language = _clean_text(lang_match.group(1))

        stars = _parse_count(_TOTAL_RE.search(body))
        period_stars, _ = _match_period(_PERIOD_RE.search(body))

        repos.append({
            "repo": repo,
            "owner": repo.split("/", 1)[0],
            "description": desc,
            "language": language,
            "stars": stars,
            "period_stars": period_stars,
            "url": f"https://github.com/{repo}",
        })
        if len(repos) >= top_n:
            break
    return repos


def _parse_search_payload(payload: Dict[str, Any], top_n: int) -> List[dict]:
    """解析 Search API 响应（纯函数，便于离线测试）"""
    raw_list = (payload or {}).get("items") or []
    repos: List[dict] = []
    for raw in raw_list[:top_n]:
        full_name = _clean_text(raw.get("full_name"))
        if not full_name or "/" not in full_name:
            continue
        repos.append({
            "repo": full_name,
            "owner": full_name.split("/", 1)[0],
            "description": _clean_text(raw.get("description") or ""),
            "language": _clean_text(raw.get("language") or ""),
            "stars": _to_int(raw.get("stargazers_count"), 0),
            "period_stars": 0,
            "topics": [str(t) for t in (raw.get("topics") or [])][:5],
            "url": _clean_text(raw.get("html_url")) or f"https://github.com/{full_name}",
            "published_at": _github_time_iso(raw.get("created_at")),
        })
    return repos


def _parse_rsshub_text(xml_text: str, top_n: int) -> List[dict]:
    """解析 RSSHub trending 路由的 RSS 文本（回退通道，字段不全从宽）"""
    if not xml_text:
        return []
    feed = feedparser.parse(xml_text)
    repos: List[dict] = []
    for entry in feed.entries[:top_n]:
        title = _clean_text(_xml_unescape(getattr(entry, "title", "") or ""))
        if "/" not in title:
            continue
        title = title.split(" ")[0].strip()  # 部分路由标题带后缀
        desc = _clean_text(_xml_unescape(getattr(entry, "description", "") or ""))
        # 剥离 description 中的 HTML 标签
        desc = _html_unescape(re.sub(r"<[^>]+>", " ", desc)).strip()
        repos.append({
            "repo": title,
            "owner": title.split("/", 1)[0],
            "description": desc[:300],
            "language": "",
            "stars": 0,
            "period_stars": 0,
            "url": getattr(entry, "link", "") or f"https://github.com/{title}",
        })
    return repos


def _merge_ai_repos(per_topic: List[List[dict]], top_n: int) -> List[dict]:
    """多话题结果合并去重（full_name），按星数倒序截断（纯函数）"""
    seen: Dict[str, dict] = {}
    for topic_repos in per_topic:
        for repo in topic_repos:
            seen.setdefault(repo["repo"], repo)
    merged = sorted(seen.values(), key=lambda r: r.get("stars", 0), reverse=True)
    return merged[:top_n]


def _build_rss_item(
    repo: dict,
    feed_id: str,
    feed_name: str,
    timezone: str,
    period: str,
    published_at: str = "",
    rank: int = 0,
) -> RSSItem:
    """结构化仓库数据 → RSSItem（summary 携带星数信息，供报告/DSA 订阅源展示）"""
    label = TRENDING_FEEDS.get(period, ("", "", ""))[2] if period else ""
    summary_lines = []
    if repo.get("description"):
        summary_lines.append(repo["description"])
    star_line = f"⭐ {_format_count(repo.get('stars', 0))}"
    if label and repo.get("period_stars"):
        star_line += f"（{label} +{_format_count(repo['period_stars'])}）"
    summary_lines.append(star_line)

    return RSSItem(
        title=f"{rank}. {repo['repo']}" if rank else repo["repo"],
        feed_id=feed_id,
        feed_name=feed_name,
        url=repo.get("url") or f"https://github.com/{repo['repo']}",
        guid=repo["repo"],
        published_at=published_at,
        summary="\n".join(summary_lines),
        author=repo.get("owner", ""),
    )


def _page_entry(repo: dict, rank: int) -> dict:
    """GitHub 页面渲染所需的结构化条目"""
    return {
        "rank": rank,
        "repo": repo["repo"],
        "owner": repo.get("owner", ""),
        "description": repo.get("description", ""),
        "language": repo.get("language", ""),
        "stars": repo.get("stars", 0),
        "period_stars": repo.get("period_stars", 0),
        "topics": repo.get("topics", []),
        "source": repo.get("source", "trending"),
        "url": repo.get("url") or f"https://github.com/{repo['repo']}",
    }


def _match_period(match: Optional[re.Match]) -> Tuple[int, str]:
    """周期星数匹配 → (数值, today/this week/this month)，未命中返回 (0, "")"""
    if not match:
        return 0, ""
    return _parse_count_str(match.group(1)), match.group(2)


def _parse_count(match: Optional[re.Match]) -> int:
    """总星数匹配 → 数值，未命中返回 0"""
    if not match:
        return 0
    return _parse_count_str(match.group(1))


def _parse_count_str(text: str) -> int:
    try:
        return int(str(text).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0


def _format_count(value: int) -> str:
    """星数 → 千分位字符串（12345 → 12,345）"""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def _github_time_iso(raw: Any) -> str:
    """Search API 的 created_at（ISO8601 UTC，如 2026-10-01T00:00:00Z）→ 规范 ISO"""
    text = _clean_text(raw)
    if not text:
        return ""
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return dt.isoformat(timespec="seconds")


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
