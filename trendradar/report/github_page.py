"""
GitHub 榜单页面生成

每次运行后从采集到的结构化榜单数据渲染独立页面 output/html/github.html
（与主报告同风格：浅色主题 + 靛紫渐变头部），经 webadmin 静态托管后
通过 /html/github.html 访问；主报告头部有入口链接。

页面只依赖本轮抓取的结构化数据（不依赖存储），榜单缺失时对应区块显示
"暂无数据" 而不是整页失败。
"""

import html
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

# 区块元数据：feed_id → (标题, 默认副标题说明)
SECTIONS = {
    "github-daily": ("📅 GitHub 日榜", "今日新增星数 Top 10"),
    "github-weekly": ("🗓️ GitHub 周榜", "本周新增星数 Top 10"),
    "github-monthly": ("📆 GitHub 月榜", "本月新增星数 Top 10"),
    "github-ai": ("🤖 AI 热门新星", "GitHub AI 话题 · 近期新建仓库按星数"),
}

# 降级来源的副标题（官方 trending 不可达时用 Search API 近似）
_FALLBACK_DAYS = {"github-daily": 1, "github-weekly": 7, "github-monthly": 30}


def _section_hint(feed_id: str, entries: List[dict], default_hint: str) -> str:
    """按数据来源选择副标题：降级榜单明确标注，避免与官方 trending 混淆"""
    source = (entries[0].get("source") or "trending") if entries else "trending"
    if source == "search" and feed_id in _FALLBACK_DAYS:
        days = _FALLBACK_DAYS[feed_id]
        return f"近 {days} 天新建仓库按星数（官方 trending 不可达时的近似榜）"
    if source == "rsshub":
        return f"{default_hint} · 经 RSSHub 抓取"
    return default_hint

# 常见语言徽章配色（与 GitHub 语言色一致，未收录用灰色）
_LANG_COLORS = {
    "Python": "#3572A5",
    "TypeScript": "#3178c6",
    "JavaScript": "#f1e05a",
    "Go": "#00ADD8",
    "Rust": "#dea584",
    "Shell": "#89e051",
    "Java": "#b07219",
    "C++": "#f34b7d",
    "C": "#555555",
    "C#": "#178600",
    "Jupyter Notebook": "#DA5B0B",
    "Vue": "#41b883",
    "HTML": "#e34c26",
    "CSS": "#563d7c",
    "Swift": "#F05138",
    "Kotlin": "#A97BFF",
    "Ruby": "#701516",
    "PHP": "#4F5D95",
    "Dart": "#00B4AB",
    "Lua": "#000080",
}

_CSS = """
:root { --accent1: #4f46e5; --accent2: #7c3aed; }
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    color: #1f2937;
    background: #fafafa;
    line-height: 1.5;
    padding: 24px 16px 48px;
}
.container { max-width: 1080px; margin: 0 auto; }
.page-header {
    background: linear-gradient(135deg, var(--accent1) 0%, var(--accent2) 100%);
    border-radius: 16px;
    padding: 28px 32px;
    color: #fff;
    margin-bottom: 24px;
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 8px 18px;
}
.page-header h1 { font-size: 26px; font-weight: 700; }
.page-header .sub { font-size: 13px; opacity: .85; }
.page-header .back {
    margin-left: auto; color: #fff; text-decoration: none; font-size: 13px;
    background: rgba(255,255,255,.18); padding: 6px 14px; border-radius: 999px;
}
.page-header .back:hover { background: rgba(255,255,255,.3); }
.section { margin-bottom: 28px; }
.section-head { display: flex; align-items: baseline; gap: 12px; margin: 0 4px 12px; }
.section-head h2 { font-size: 19px; font-weight: 700; }
.section-head .hint { font-size: 12px; color: #6b7280; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(480px, 1fr)); gap: 12px; }
@media (max-width: 560px) { .grid { grid-template-columns: 1fr; } }
.repo-card {
    background: #fff; border-radius: 12px; padding: 14px 16px;
    border: 1px solid #e5e7eb; box-shadow: 0 1px 2px rgba(0,0,0,.04);
    transition: box-shadow .15s ease;
}
.repo-card:hover { box-shadow: 0 4px 14px rgba(79,70,229,.12); }
.repo-top { display: flex; align-items: center; gap: 10px; }
.rank {
    flex: none; width: 28px; height: 28px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 13px; font-weight: 700; color: #6b7280; background: #f3f4f6;
}
.rank.r1 { background: linear-gradient(135deg,#fbbf24,#f59e0b); color: #fff; }
.rank.r2 { background: linear-gradient(135deg,#cbd5e1,#94a3b8); color: #fff; }
.rank.r3 { background: linear-gradient(135deg,#d97706,#b45309); color: #fff; }
.repo-name {
    font-size: 15px; font-weight: 700; color: #4f46e5;
    text-decoration: none; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.repo-name:hover { text-decoration: underline; }
.lang { flex: none; font-size: 11px; color: #4b5563; background: #f3f4f6;
    padding: 3px 8px; border-radius: 999px; white-space: nowrap; display: flex; align-items: center; gap: 5px; }
.lang .dot { width: 8px; height: 8px; border-radius: 50%; background: #9ca3af; }
.repo-desc {
    margin-top: 8px; font-size: 13px; color: #4b5563;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.repo-meta { margin-top: 8px; display: flex; flex-wrap: wrap; align-items: center; gap: 6px 14px; font-size: 12px; color: #6b7280; }
.stars { color: #b45309; font-weight: 600; }
.period { color: #059669; font-weight: 600; }
.topic { background: #eef2ff; color: #4338ca; border-radius: 999px; padding: 2px 9px; font-size: 11px; }
.empty { color: #9ca3af; font-size: 13px; padding: 14px 16px; background: #fff;
    border: 1px dashed #e5e7eb; border-radius: 12px; }
.page-footer { text-align: center; font-size: 12px; color: #9ca3af; margin-top: 32px; }
"""


def generate_github_page(
    page_data: Dict[str, List[dict]],
    output_dir: str = "output",
    timezone: str = "Asia/Shanghai",
) -> Optional[str]:
    """
    生成 GitHub 榜单页面

    Args:
        page_data: {feed_id: [结构化榜单条目]}（crawler.github_trending._page_entry 产出）
        output_dir: 输出根目录（页面写到 <output_dir>/html/github.html）
        timezone: IANA 时区名，用于生成时间显示

    Returns:
        生成的文件路径；无任何榜单数据时返回 None
    """
    if not page_data or not any(page_data.values()):
        return None

    try:
        tz = ZoneInfo(timezone)
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    generated_at = datetime.now(tz).strftime("%Y-%m-%d %H:%M")

    sections_html = ""
    for feed_id, (title, hint) in SECTIONS.items():
        entries = page_data.get(feed_id) or []
        sections_html += _render_section(
            feed_id, title, _section_hint(feed_id, entries, hint), entries,
        )

    page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GitHub Trending 榜单</title>
<style>{_CSS}</style>
</head>
<body>
<div class="container">
    <div class="page-header">
        <h1>🐙 GitHub Trending 榜单</h1>
        <span class="sub">生成时间 {generated_at}</span>
        <a class="back" href="../">返回报告</a>
    </div>
    {sections_html}
    <div class="page-footer">
        TrendRadar 自动采集 · 数据来源 github.com/trending 与 api.github.com ·
        榜单每轮运行更新
    </div>
</div>
</body>
</html>"""

    output_path = Path(output_dir) / "html"
    output_path.mkdir(parents=True, exist_ok=True)
    target = output_path / "github.html"
    target.write_text(page, encoding="utf-8")
    return str(target)


def _render_section(feed_id: str, title: str, hint: str, entries: List[dict]) -> str:
    """渲染单个榜单区块；无数据显示占位提示"""
    head = (f'<div class="section" id="{html.escape(feed_id)}">'
            f'<div class="section-head"><h2>{html.escape(title)}</h2>'
            f'<span class="hint">{html.escape(hint)}</span></div>')
    if not entries:
        return head + '<div class="empty">本轮未抓取到数据（数据源不可达或未配置该榜单）</div></div>'

    cards = "".join(_render_card(entry) for entry in entries)
    return head + f'<div class="grid">{cards}</div></div>'


def _render_card(entry: dict) -> str:
    """渲染单个仓库卡片"""
    rank = _to_int(entry.get("rank"), 0)
    rank_cls = f" r{rank}" if rank in (1, 2, 3) else ""

    lang_html = ""
    language = str(entry.get("language") or "")
    if language:
        color = _LANG_COLORS.get(language, "#9ca3af")
        lang_html = (f'<span class="lang"><span class="dot" style="background:{color}"></span>'
                     f'{html.escape(language)}</span>')

    stars = _to_int(entry.get("stars"), 0)
    period = _to_int(entry.get("period_stars"), 0)
    meta_parts = [f'<span class="stars">⭐ {stars:,}</span>']
    if period:
        meta_parts.append(f'<span class="period">+{period:,}</span>')
    for topic in (entry.get("topics") or [])[:4]:
        meta_parts.append(f'<span class="topic">{html.escape(str(topic))}</span>')

    description = str(entry.get("description") or "")
    desc_html = f'<div class="repo-desc">{html.escape(description)}</div>' if description else ""

    return f"""<div class="repo-card">
    <div class="repo-top">
        <span class="rank{rank_cls}">{rank or '-'}</span>
        <a class="repo-name" href="{html.escape(str(entry.get('url') or ''), quote=True)}"
           target="_blank" rel="noopener noreferrer">{html.escape(str(entry.get('repo') or ''))}</a>
        {lang_html}
    </div>
    {desc_html}
    <div class="repo-meta">{"".join(meta_parts)}</div>
</div>"""


def _to_int(value, default: int = 0) -> int:
    """宽松 int 转换（失败回退默认值）"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
