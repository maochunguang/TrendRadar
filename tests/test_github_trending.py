# coding=utf-8
"""
GitHub 榜单采集测试

覆盖：trending HTML 解析（真实页面结构离线样本）、Search API 响应解析、
RSSHub 回退解析、多话题合并、RSSItem 构建、独立页面渲染、loader 配置加载。
"""

import os
import tempfile
import unittest
from pathlib import Path

from trendradar.core.loader import _load_github_config
from trendradar.crawler.github_trending import (
    AI_FEED_ID,
    AI_FEED_NAME,
    TRENDING_FEEDS,
    _build_rss_item,
    _merge_ai_repos,
    _parse_rsshub_text,
    _parse_search_payload,
    _parse_trending_html,
    fetch_github,
)
from trendradar.report.github_page import generate_github_page
from trendradar.storage.base import RSSItem


# trending 页面真实结构的离线样本（截取自 2026-11-19 github.com/trending，
# 移除冗长 SVG path 与埋点属性，保留布局骨架）
_TRENDING_ROW_AI = """
<article class="Box-row">
 <div class="float-right d-flex">
  <a href="/login?return_to=%2FNVIDIA%2FSkillEvaluator" rel="nofollow" class="btn-sm btn">
   <svg class="octicon octicon-star"><path/></svg> <span> Star </span></a>
 </div>
 <h2 class="h3 lh-condensed">
  <a href="/NVIDIA/SkillEvaluator" class="Link"><svg class="octicon-repo"><path/></svg>
   <span class="text-normal"> NVIDIA / </span> SkillEvaluator</a>
 </h2>
 <p class="col-9 color-fg-muted my-1 tmp-pr-4">
  Multi-tier framework for evaluating AI agent skills &amp; plugins with quality gates.
 </p>
 <div class="f6 color-fg-muted mt-2">
  <span class="tmp-mr-3 d-inline-block ml-0 tmp-ml-0">
   <span class="repo-language-color" style="background-color: #3572A5"></span>
   <span itemprop="programmingLanguage">Python</span>
  </span>
  <a href="/NVIDIA/SkillEvaluator/stargazers" class="Link Link--muted d-inline-block">
   <svg aria-label="star"><path/></svg> 3,553</a>
  <a href="/NVIDIA/SkillEvaluator/forks" class="Link Link--muted d-inline-block">
   <svg aria-label="fork"><path/></svg> 500</a>
  <span class="d-inline-block float-sm-right"><svg class="octicon-star"><path/></svg>
   323 stars today</span>
 </div>
</article>
"""

_TRENDING_ROW_NO_LANG = """
<article class="Box-row">
 <h2 class="h3 lh-condensed">
  <a href="/punkpeye/awesome-mcp-servers" class="Link">awesome-mcp-servers</a>
 </h2>
 <div class="f6 color-fg-muted mt-2">
  <a href="/punkpeye/awesome-mcp-servers/stargazers" class="Link Link--muted d-inline-block">
   <svg aria-label="star"><path/></svg> 97,922</a>
  <span class="d-inline-block float-sm-right"><svg class="octicon-star"><path/></svg>
   52 stars today</span>
 </div>
</article>
"""

_TRENDING_ROW_NOT_REPO = """
<article class="Box-row">
 <h2 class="h3 lh-condensed">
  <a href="/trending" class="Link">not a repo (no slash in path)</a>
 </h2>
</article>
"""

TRENDING_HTML = (
    '<html><body>'
    + _TRENDING_ROW_AI + _TRENDING_ROW_NO_LANG + _TRENDING_ROW_NOT_REPO
    + '</body></html>'
)

# Search API 真实响应结构的离线样本
SEARCH_PAYLOAD = {
    "total_count": 2,
    "items": [
        {
            "full_name": "user1/awesome-llm",
            "html_url": "https://github.com/user1/awesome-llm",
            "description": "An awesome LLM toolkit",
            "language": "Python",
            "stargazers_count": 5432,
            "topics": ["llm", "ai", "agents"],
            "created_at": "2026-10-21T08:00:00Z",
        },
        {
            "full_name": "not-a-name",
            "html_url": "https://github.com/not-a-name",
            "description": "malformed, should be skipped",
        },
        {
            "full_name": "user2/tiny-agent",
            "html_url": "",
            "description": "",
            "stargazers_count": 120,
            "created_at": "bad-date",
        },
    ],
}

MINIMAL_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>GitHub Trending Daily</title>
<item><title>owner1/repo1</title><link>https://github.com/owner1/repo1</link>
<description>&lt;p&gt;A fallback repo&lt;/p&gt;</description></item>
<item><title>invalid title no slash</title><link>https://example.com</link></item>
</channel></rss>
"""


class TrendingHtmlParseTests(unittest.TestCase):
    """trending 页 HTML 解析"""

    def setUp(self):
        self.repos = _parse_trending_html(TRENDING_HTML, top_n=10)

    def test_extracts_repo_entries(self):
        self.assertEqual(len(self.repos), 2)  # 非 repo 行被跳过
        self.assertEqual(self.repos[0]["repo"], "NVIDIA/SkillEvaluator")
        self.assertEqual(self.repos[0]["owner"], "NVIDIA")
        self.assertEqual(self.repos[1]["repo"], "punkpeye/awesome-mcp-servers")

    def test_fields(self):
        first = self.repos[0]
        self.assertEqual(first["language"], "Python")
        self.assertEqual(first["stars"], 3553)
        self.assertEqual(first["period_stars"], 323)
        self.assertEqual(
            first["description"],
            "Multi-tier framework for evaluating AI agent skills & plugins with quality gates.",
        )
        self.assertEqual(first["url"], "https://github.com/NVIDIA/SkillEvaluator")

    def test_no_language_entry_kept(self):
        second = self.repos[1]
        self.assertEqual(second["language"], "")
        self.assertEqual(second["description"], "")
        self.assertEqual(second["stars"], 97922)

    def test_top_n_truncates(self):
        self.assertEqual(len(_parse_trending_html(TRENDING_HTML, top_n=1)), 1)

    def test_empty_html(self):
        self.assertEqual(_parse_trending_html("", 10), [])
        self.assertEqual(_parse_trending_html("<html></html>", 10), [])


class SearchPayloadParseTests(unittest.TestCase):
    """Search API 响应解析"""

    def setUp(self):
        self.repos = _parse_search_payload(SEARCH_PAYLOAD, top_n=10)

    def test_skips_malformed(self):
        self.assertEqual(len(self.repos), 2)

    def test_fields(self):
        first = self.repos[0]
        self.assertEqual(first["repo"], "user1/awesome-llm")
        self.assertEqual(first["stars"], 5432)
        self.assertEqual(first["published_at"], "2026-10-21T08:00:00+00:00")
        self.assertEqual(first["topics"], ["llm", "ai", "agents"])

    def test_bad_date_and_missing_url(self):
        second = self.repos[1]
        self.assertEqual(second["published_at"], "")
        self.assertEqual(second["url"], "https://github.com/user2/tiny-agent")

    def test_empty_payload(self):
        self.assertEqual(_parse_search_payload({}, 10), [])
        self.assertEqual(_parse_search_payload({"items": None}, 10), [])


class MergeAiReposTests(unittest.TestCase):
    """多话题合并去重排序"""

    def test_merge_dedup_sort_truncate(self):
        per_topic = [
            [{"repo": "a/one", "stars": 100, "url": "", "owner": "a", "description": "", "language": "", "period_stars": 0}],
            [{"repo": "a/one", "stars": 100, "url": "", "owner": "a", "description": "", "language": "", "period_stars": 0},
             {"repo": "b/two", "stars": 500, "url": "", "owner": "b", "description": "", "language": "", "period_stars": 0}],
            [{"repo": "c/three", "stars": 300, "url": "", "owner": "c", "description": "", "language": "", "period_stars": 0}],
        ]
        merged = _merge_ai_repos(per_topic, top_n=2)
        self.assertEqual([r["repo"] for r in merged], ["b/two", "c/three"])


class BuildRssItemTests(unittest.TestCase):
    """RSSItem 构建与星数文案"""

    def test_trending_item(self):
        repo = {
            "repo": "NVIDIA/SkillEvaluator", "owner": "NVIDIA",
            "description": "AI agent skills framework", "language": "Python",
            "stars": 3553, "period_stars": 323,
            "url": "https://github.com/NVIDIA/SkillEvaluator",
        }
        item = _build_rss_item(repo, "github-daily", TRENDING_FEEDS["daily"][1],
                               "Asia/Shanghai", "daily", rank=1)
        self.assertIsInstance(item, RSSItem)
        self.assertEqual(item.title, "1. NVIDIA/SkillEvaluator")
        self.assertEqual(item.feed_id, "github-daily")
        self.assertEqual(item.feed_name, "GitHub 日榜")
        self.assertEqual(item.guid, "NVIDIA/SkillEvaluator")
        self.assertEqual(item.author, "NVIDIA")
        self.assertIn("AI agent skills framework", item.summary)
        self.assertIn("⭐ 3,553", item.summary)
        self.assertIn("今日 +323", item.summary)

    def test_ai_item_no_period_line(self):
        repo = {
            "repo": "user1/awesome-llm", "owner": "user1",
            "description": "LLM toolkit", "language": "Python",
            "stars": 5432, "period_stars": 0,
            "url": "https://github.com/user1/awesome-llm",
        }
        item = _build_rss_item(repo, AI_FEED_ID, AI_FEED_NAME, "Asia/Shanghai", "", rank=2)
        self.assertEqual(item.feed_id, "github-ai")
        self.assertEqual(item.title, "2. user1/awesome-llm")
        self.assertIn("⭐ 5,432", item.summary)
        self.assertNotIn("+", item.summary.split("\n")[-1])


class RsshubFallbackParseTests(unittest.TestCase):
    """RSSHub 回退通道解析"""

    def test_parse_minimal_rss(self):
        repos = _parse_rsshub_text(MINIMAL_RSS, top_n=10)
        self.assertEqual(len(repos), 1)  # 无 "/" 标题被跳过
        self.assertEqual(repos[0]["repo"], "owner1/repo1")
        self.assertEqual(repos[0]["description"], "A fallback repo")
        self.assertEqual(repos[0]["url"], "https://github.com/owner1/repo1")

    def test_empty(self):
        self.assertEqual(_parse_rsshub_text("", 10), [])


class FetchGithubTests(unittest.TestCase):
    """fetch_github 主入口（mock 网络）"""

    def _config(self, **overrides):
        cfg = {
            "ENABLED": True, "TOP_N": 10,
            "PERIODS": ["daily"], "AI_TOPICS": ["llm"],
            "AI_DAYS": 30, "RSSHUB_BASE": "", "TOKEN": "",
        }
        cfg.update(overrides)
        return cfg

    def test_disabled_returns_empty(self):
        items, page, errors = fetch_github(self._config(ENABLED=False), timeout=1)
        self.assertEqual((items, page, errors), ({}, {}, []))

    def test_all_channels_fail_reports_errors(self):
        """直连 + RSSHub + Search API 全部失败时返回错误信息且不抛异常"""
        import unittest.mock as mock

        with mock.patch("trendradar.crawler.github_trending.requests.get",
                        side_effect=Exception("network down")):
            items, page, errors = fetch_github(self._config(), timeout=1)
        self.assertEqual(items, {})
        self.assertEqual(page, {})
        self.assertTrue(errors)
        self.assertIn("Trending daily", errors[0])
        self.assertIn("全部通道失败", errors[0])

    def test_fallback_to_search_when_trending_unreachable(self):
        """官方 trending 不可达时自动降级为 Search API 近似榜"""
        import unittest.mock as mock

        api_resp = mock.Mock(status_code=200, json=mock.Mock(return_value=SEARCH_PAYLOAD))
        api_resp.raise_for_status = mock.Mock()

        def fake_get(url, **kwargs):
            if "api.github.com" in url:
                return api_resp
            raise Exception("connection timed out")

        with mock.patch("trendradar.crawler.github_trending.requests.get",
                        side_effect=fake_get):
            items, page, errors = fetch_github(
                self._config(RSSHUB_BASE="http://172.17.0.1:1200"), timeout=1,
            )

        self.assertIn("github-daily", items)
        self.assertEqual(len(items["github-daily"]), 2)
        self.assertEqual(page["github-daily"][0]["source"], "search")
        self.assertTrue(any("已降级" in e for e in errors))

    def test_mirror_channel_used_before_rsshub(self):
        """配置镜像反代时，直连失败先走镜像（不经 RSSHub/Search）"""
        import unittest.mock as mock

        mirror_resp = mock.Mock(status_code=200, text=TRENDING_HTML,
                                headers={"Content-Type": "text/html; charset=utf-8"})
        mirror_resp.raise_for_status = mock.Mock()
        api_resp = mock.Mock(status_code=200, json=mock.Mock(return_value=SEARCH_PAYLOAD))
        api_resp.raise_for_status = mock.Mock()

        def fake_get(url, **kwargs):
            if "api.github.com" in url:
                return api_resp
            if "workers.dev" in url:
                return mirror_resp
            raise Exception("connection timed out")

        with mock.patch("trendradar.crawler.github_trending.requests.get",
                        side_effect=fake_get):
            items, page, errors = fetch_github(
                self._config(MIRROR_URL="https://gh-mirror.workers.dev"),
                timeout=1,
            )

        self.assertEqual(errors, [])
        self.assertIn("github-daily", items)
        self.assertEqual(items["github-daily"][0].title, "1. NVIDIA/SkillEvaluator")
        self.assertEqual(page["github-daily"][0]["source"], "mirror")
        self.assertIn(AI_FEED_ID, items)

    def test_mirror_json_api_adaptive(self):
        """mirror_url 指向云服务器 JSON API 时按 Content-Type 自动解析 JSON"""
        import unittest.mock as mock

        api_payload = {
            "period": "daily",
            "repos": [
                {"rank": 1, "repo": "json/repo", "description": "from cloud api",
                 "language": "Go", "stars": 500, "period_stars": 40,
                 "url": "https://github.com/json/repo"},
            ],
        }
        json_resp = mock.Mock(status_code=200, json=mock.Mock(return_value=api_payload),
                              headers={"Content-Type": "application/json; charset=utf-8"})
        json_resp.raise_for_status = mock.Mock()
        api_resp = mock.Mock(status_code=200, json=mock.Mock(return_value=SEARCH_PAYLOAD))
        api_resp.raise_for_status = mock.Mock()

        def fake_get(url, **kwargs):
            if "api.github.com" in url:
                return api_resp
            if "cloud:8787" in url:
                return json_resp
            raise Exception("connection timed out")

        with mock.patch("trendradar.crawler.github_trending.requests.get",
                        side_effect=fake_get):
            items, page, errors = fetch_github(
                self._config(MIRROR_URL="http://my-cloud:8787"), timeout=1,
            )

        self.assertEqual(errors, [])
        self.assertEqual(page["github-daily"][0]["source"], "mirror")
        self.assertEqual(page["github-daily"][0]["repo"], "json/repo")
        self.assertEqual(page["github-daily"][0]["stars"], 500)
        self.assertEqual(page["github-daily"][0]["period_stars"], 40)

    def test_mirror_url_empty_config(self):
        cfg = _load_github_config({"github": {"mirror_url": " https://x.workers.dev/ "}})
        self.assertEqual(cfg["MIRROR_URL"], "https://x.workers.dev")

    def test_success_builds_items_and_page(self):
        import unittest.mock as mock

        html_resp = mock.Mock(status_code=200, text=TRENDING_HTML)
        html_resp.raise_for_status = mock.Mock()
        api_resp = mock.Mock(status_code=200, json=mock.Mock(return_value=SEARCH_PAYLOAD))
        api_resp.raise_for_status = mock.Mock()

        def fake_get(url, **kwargs):
            if "api.github.com" in url:
                return api_resp
            return html_resp

        with mock.patch("trendradar.crawler.github_trending.requests.get",
                        side_effect=fake_get):
            items, page, errors = fetch_github(self._config(), timeout=1)

        self.assertEqual(errors, [])
        self.assertIn("github-daily", items)
        self.assertIn(AI_FEED_ID, items)
        self.assertEqual(items["github-daily"][0].title, "1. NVIDIA/SkillEvaluator")
        self.assertEqual(page["github-daily"][0]["rank"], 1)
        self.assertEqual(page[AI_FEED_ID][0]["stars"], 5432)


class GithubPageTests(unittest.TestCase):
    """独立 GitHub 页面渲染"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.page_data = {
            "github-daily": [
                {"rank": 1, "repo": "a/one<script>", "owner": "a",
                 "description": 'desc with "quotes" & <tags>', "language": "Python",
                 "stars": 3553, "period_stars": 323,
                 "topics": [], "url": "https://github.com/a/one"},
                {"rank": 2, "repo": "b/two", "owner": "b", "description": "",
                 "language": "", "stars": 100, "period_stars": 0,
                 "topics": [], "url": "https://github.com/b/two"},
            ],
            "github-ai": [
                {"rank": 1, "repo": "c/three", "owner": "c", "description": "AI repo",
                 "language": "Go", "stars": 900, "period_stars": 0,
                 "topics": ["llm", "rag"], "url": "https://github.com/c/three"},
            ],
        }

    def test_generates_page_with_sections(self):
        path = generate_github_page(self.page_data, output_dir=self.tmpdir)
        self.assertTrue(path and Path(path).exists())
        html_text = Path(path).read_text(encoding="utf-8")
        for feed_id in ("github-daily", "github-weekly", "github-monthly", "github-ai"):
            self.assertIn(f'id="{feed_id}"', html_text)
        # 有数据的区块 + 空榜占位
        self.assertIn("a/one&lt;script&gt;", html_text)  # repo 名转义
        self.assertIn("desc with &quot;quotes&quot; &amp; &lt;tags&gt;", html_text)
        self.assertIn("本轮未抓取到数据", html_text)  # weekly/monthly 空榜
        self.assertIn("GitHub 日榜", html_text)
        self.assertIn("AI 热门新星", html_text)

    def test_empty_page_data_returns_none(self):
        self.assertIsNone(generate_github_page({}, output_dir=self.tmpdir))
        self.assertIsNone(generate_github_page({"github-daily": []}, output_dir=self.tmpdir))


class GithubConfigTests(unittest.TestCase):
    """loader 配置加载与清洗"""

    def test_defaults(self):
        cfg = _load_github_config({})
        self.assertFalse(cfg["ENABLED"])
        self.assertEqual(cfg["TOP_N"], 10)
        self.assertEqual(cfg["PERIODS"], ["daily", "weekly", "monthly"])
        self.assertEqual(cfg["AI_TOPICS"], ["ai", "llm", "agent"])
        self.assertEqual(cfg["AI_DAYS"], 30)

    def test_clamps_and_filters(self):
        cfg = _load_github_config({
            "github": {
                "top_n": 99,
                "periods": ["daily", "invalid", "monthly", "daily"],
                "ai_topics": ["LLM", "", None, "llm"],
                "ai_days": 0,
                "rsshub_base": "http://172.17.0.1:1200/",
            },
        })
        self.assertEqual(cfg["TOP_N"], 25)
        self.assertEqual(cfg["PERIODS"], ["daily", "monthly"])
        self.assertEqual(cfg["AI_TOPICS"], ["llm"])
        self.assertEqual(cfg["AI_DAYS"], 1)
        self.assertEqual(cfg["RSSHUB_BASE"], "http://172.17.0.1:1200")

    def test_bad_types_fall_back(self):
        cfg = _load_github_config({"github": {"top_n": "abc", "ai_days": "xyz"}})
        self.assertEqual(cfg["TOP_N"], 10)
        self.assertEqual(cfg["AI_DAYS"], 30)


if __name__ == "__main__":
    unittest.main()
