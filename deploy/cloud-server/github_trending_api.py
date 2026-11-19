#!/usr/bin/env python3
# coding=utf-8
"""
GitHub Trending JSON API —— 部署在你自己的云服务器上

为什么用它而不是 nginx 纯反代：
- nginx 反代是把 github.com 的 HTML 全页透传，TrendRadar 侧仍要解析；
  此 API 在云服务器上直接抓取并解析好，TrendRadar 拿到的就是结构化 JSON
- 内置 20 分钟内存缓存（GitHub trending 本就是小时级变化，也避免被 GitHub 限流）

前提：云服务器必须能稳定访问 github.com（境外/香港节点最稳；
境内节点与 NAS 同受干扰，部署前先自测：
    curl -s -o /dev/null -w "%{http_code}" -m 10 https://github.com/trending
返回 200 才有意义）。

零依赖：仅用 Python 标准库（3.8+），无需 pip install。
解析正则与 TrendRadar 的 trendradar/crawler/github_trending.py 保持同步
（GitHub 页面改版时两处需一起更新）。

运行：
    python3 github_trending_api.py            # 默认监听 0.0.0.0:8787
    PORT=9000 API_TOKEN=你的密钥 python3 ...   # 可选端口与访问令牌

配 systemd 常驻（见同目录 README.md）。

TrendRadar 侧配置：
    github:
      mirror_url: http://<云服务器IP>:8787
（该地址既支持本 JSON API，也支持 Cloudflare Workers 反代等 HTML 反代，
TrendRadar 会按响应 Content-Type 自动选择解析方式。）

可选鉴权：设置 API_TOKEN 环境变量后，请求需带
    ?token=<API_TOKEN> 或 Authorization: Bearer <API_TOKEN>
TrendRadar 侧把 token 拼在 mirror_url 上即可：
      mirror_url: http://ip:8787?token=你的密钥
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urlunparse

PORT = int(os.environ.get("PORT", "8787"))
API_TOKEN = os.environ.get("API_TOKEN", "")
CACHE_TTL = int(os.environ.get("CACHE_TTL_SECONDS", str(20 * 60)))
UPSTREAM_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "20"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# trending 页面结构（与 trendradar/crawler/github_trending.py 同步，2026-11 实测）
_ROW_SPLIT = '<article class="Box-row">'
_REPO_RE = re.compile(r'<h2[^>]*>\s*<a[^>]*href="/([^"]+?)"', re.S)
_DESC_RE = re.compile(r'<p class="col-9[^"]*"[^>]*>\s*(.*?)\s*</p>', re.S)
_LANG_RE = re.compile(r'itemprop="programmingLanguage">([^<]+)<')
_TOTAL_RE = re.compile(r'href="/[^"]+/stargazers".*?</svg>\s*([\d,]+)', re.S)
_PERIOD_RE = re.compile(r'([\d,]+)\s+stars\s+(today|this week|this month)')

_cache = {}          # since -> {"ts": float, "payload": dict}
_cache_lock = threading.Lock()


def _clean_text(value):
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def _parse_count_str(text):
    try:
        return int(str(text).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0


def parse_trending_html(html, since, limit=25):
    """解析 trending 页 HTML → 结构化条目列表"""
    repos = []
    for block in html.split(_ROW_SPLIT)[1:]:
        body = block.split("</article>")[0]

        m = _REPO_RE.search(body)
        if not m:
            continue
        repo = _clean_text(m.group(1))
        if "/" not in repo:
            continue

        desc = ""
        dm = _DESC_RE.search(body)
        if dm:
            desc = _clean_text(re.sub(r"<[^>]+>", "", dm.group(1)))

        lang = ""
        lm = _LANG_RE.search(body)
        if lm:
            lang = _clean_text(lm.group(1))

        stars = 0
        tm = _TOTAL_RE.search(body)
        if tm:
            stars = _parse_count_str(tm.group(1))

        period_stars = 0
        pm = _PERIOD_RE.search(body)
        if pm:
            period_stars = _parse_count_str(pm.group(1))

        repos.append({
            "rank": len(repos) + 1,
            "repo": repo,
            "description": desc,
            "language": lang,
            "stars": stars,
            "period_stars": period_stars,
            "period": since,
            "url": f"https://github.com/{repo}",
        })
        if len(repos) >= limit:
            break
    return repos


def fetch_cached(since):
    """带 20 分钟缓存的抓取（多线程安全）"""
    now = time.time()
    with _cache_lock:
        entry = _cache.get(since)
        if entry and now - entry["ts"] < CACHE_TTL:
            return entry["payload"], True

    url = f"https://github.com/trending/{since}"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
        html = resp.read().decode("utf-8", "ignore")

    repos = parse_trending_html(html, since)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "period": since,
        "source": "github.com/trending",
        "count": len(repos),
        "repos": repos,
    }
    with _cache_lock:
        _cache[since] = {"ts": now, "payload": payload}
    return payload, False


class Handler(BaseHTTPRequestHandler):
    server_version = "GH-Trending-API/1.0"

    def _send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authorized(self, query):
        if not API_TOKEN:
            return True
        token = (query.get("token") or [""])[0]
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = token or auth[7:]
        return token == API_TOKEN

    def do_GET(self):
        parts = urlparse(self.path)
        path = parts.path
        query = parse_qs(parts.query)

        if path in ("/health", "/healthz"):
            return self._send_json(200, {"status": "ok", "cached_periods": list(_cache.keys())})

        m = re.fullmatch(r"/trending/(daily|weekly|monthly)", path)
        if not m:
            return self._send_json(404, {"error": "use /trending/daily|weekly|monthly or /health"})

        if not self._authorized(query):
            return self._send_json(401, {"error": "invalid token"})

        since = m.group(1)
        try:
            payload, cached = fetch_cached(since)
        except Exception as e:  # 上游失败：缓存旧数据兜底
            with _cache_lock:
                entry = _cache.get(since)
            if entry:
                stale = dict(entry["payload"])
                stale["stale"] = True
                stale["upstream_error"] = str(e)[:200]
                return self._send_json(200, stale)
            return self._send_json(502, {"error": f"upstream fetch failed: {e}"})

        out = dict(payload)
        out["cached"] = cached
        return self._send_json(200, out)

    do_HEAD = do_GET

    def log_message(self, fmt, *args):  # 静默访问日志（避免刷盘）
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    print(f"GitHub Trending API listening on 0.0.0.0:{PORT} "
          f"(cache {CACHE_TTL}s, token {'enabled' if API_TOKEN else 'disabled'})")
    Server(("0.0.0.0", PORT), Handler).serve_forever()
