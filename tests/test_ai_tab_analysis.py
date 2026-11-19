# coding=utf-8
"""
AI 分析按 tab 分类配置测试

覆盖：loader tabs 校验、各分类输入数据构建（finance/ai/all/github）、
分类结果合并、多渠道渲染（markdown/plain/html）的分类分组输出。
"""

import unittest
from types import SimpleNamespace

from trendradar.core.loader import _load_ai_analysis_config
from trendradar.ai.analyzer import AIAnalysisResult
from trendradar.ai.formatter import (
    render_ai_analysis_markdown,
    render_ai_analysis_plain,
    render_ai_analysis_html_rich,
)
from trendradar.__main__ import NewsAnalyzer


def _fake_self(all_hotlist=None, raw_rss=None, gh_page=None):
    return SimpleNamespace(
        _all_hotlist_data=all_hotlist or {},
        _raw_rss_items=raw_rss or [],
        _github_page_data=gh_page or {},
    )


HOT_STATS = [{"word": "华为", "count": 1, "titles": [
    {"title": "华为发布新机", "source_name": "微博", "url": "https://e.com/a", "ranks": [1]},
]}]
RSS_STATS = [{"word": "财经股票", "count": 1, "titles": [
    {"title": "AI 芯片概念走强", "source_name": "财联社", "url": "https://r.com/1", "ranks": []},
]}]


class BuildAiTabDataTests(unittest.TestCase):
    """各分类输入数据构建"""

    def test_finance_returns_report_stats(self):
        fake = _fake_self()
        stats, rss, label = NewsAnalyzer._build_ai_tab_data(fake, "finance", HOT_STATS, RSS_STATS)
        self.assertEqual(stats, HOT_STATS)
        self.assertEqual(rss, RSS_STATS)
        self.assertIn("财经", label)

    def test_ai_filters_by_keyword(self):
        fake = _fake_self()
        stats, rss, label = NewsAnalyzer._build_ai_tab_data(fake, "ai", HOT_STATS, RSS_STATS)
        # 热榜"华为发布新机"非 AI 相关被滤掉；RSS"AI 芯片概念走强"保留
        self.assertEqual(stats, [])
        self.assertEqual(len(rss[0]["titles"]), 1)
        self.assertEqual(rss[0]["titles"][0]["title"], "AI 芯片概念走强")
        self.assertIn("AI", label)

    def test_all_excludes_matched_and_ai(self):
        fake = _fake_self(
            all_hotlist={"weibo": {
                "华为发布新机": {"ranks": [1], "url": "https://e.com/a", "mobileUrl": ""},
                "某明星官宣结婚": {"ranks": [2], "url": "https://e.com/b", "mobileUrl": ""},
                "DeepSeek 发布新模型": {"ranks": [3], "url": "https://e.com/c", "mobileUrl": ""},
            }},
            raw_rss=[{"title": "未命中词组的 RSS", "url": "https://r.com/x", "feed_id": "hn"}],
        )
        stats, rss, label = NewsAnalyzer._build_ai_tab_data(fake, "all", HOT_STATS, RSS_STATS)
        titles = [t["title"] for s in stats for t in s["titles"]]
        # 命中的"华为发布新机"与 AI 相关的"DeepSeek"都被排除
        self.assertEqual(titles, ["某明星官宣结婚"])
        # 平台名进入 source_name 与分组 word
        self.assertEqual(stats[0]["word"], "weibo")
        self.assertEqual(stats[0]["titles"][0]["source_name"], "weibo")
        rss_titles = [t["title"] for s in rss for t in s["titles"]]
        self.assertEqual(rss_titles, ["未命中词组的 RSS"])
        self.assertIn("全部新闻", label)

    def test_github_builds_from_page_data(self):
        fake = _fake_self(gh_page={"github-daily": [
            {"rank": 1, "repo": "a/one", "owner": "a", "description": "desc",
             "stars": 100, "url": "https://github.com/a/one"},
        ]})
        stats, rss, label = NewsAnalyzer._build_ai_tab_data(fake, "github", [], [])
        self.assertEqual(rss, [])
        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]["word"], "GitHub 日榜")
        self.assertIn("1. a/one", stats[0]["titles"][0]["title"])
        self.assertIn("GitHub", label)

    def test_unknown_tab_falls_back_to_finance(self):
        fake = _fake_self()
        stats, rss, label = NewsAnalyzer._build_ai_tab_data(fake, "unknown", HOT_STATS, RSS_STATS)
        self.assertEqual(stats, HOT_STATS)


class MergeTabResultsTests(unittest.TestCase):
    """分类结果合并"""

    def test_merge_stats_and_success(self):
        sub1 = AIAnalysisResult(success=True, core_trends="态势A",
                                total_news=10, analyzed_news=8, ai_mode="current")
        sub2 = AIAnalysisResult(success=True, rss_insights="洞察B",
                                total_news=5, analyzed_news=5, ai_mode="current")
        merged = NewsAnalyzer._merge_tab_results(
            None,
            [("all", "🗞️ 全部新闻", sub1), ("finance", "📰 财经新闻", sub2)],
            ai_mode="current",
        )
        self.assertTrue(merged.success)
        self.assertEqual(len(merged.tab_results), 2)
        self.assertEqual(merged.tab_results[0][0], "🗞️ 全部新闻")
        self.assertEqual(merged.total_news, 15)
        self.assertEqual(merged.analyzed_news, 13)

    def test_merge_all_failed_marks_skipped(self):
        sub1 = AIAnalysisResult(success=False, skipped=True, error="无内容")
        merged = NewsAnalyzer._merge_tab_results(None, [("ai", "🤖 AI 新闻", sub1)], ai_mode="daily")
        self.assertFalse(merged.success)
        self.assertTrue(merged.skipped)

    def test_merge_partial_failure(self):
        sub1 = AIAnalysisResult(success=True, core_trends="ok")
        sub2 = AIAnalysisResult(success=False, error="timeout")
        merged = NewsAnalyzer._merge_tab_results(
            None, [("all", "🗞️ 全部新闻", sub1), ("finance", "📰 财经新闻", sub2)], ai_mode="daily",
        )
        self.assertTrue(merged.success)
        self.assertIn("timeout", merged.error)


class FormatterTabResultsTests(unittest.TestCase):
    """多渠道渲染的分类分组输出"""

    def _tabbed(self):
        sub1 = AIAnalysisResult(success=True, core_trends="全部新闻态势",
                                outlook_strategy="全部新闻策略", total_news=5)
        sub2 = AIAnalysisResult(success=True, core_trends="财经态势", total_news=3)
        return AIAnalysisResult(
            success=True,
            tab_results=[("🗞️ 全部新闻", sub1), ("📰 财经新闻", sub2)],
            ai_mode="current",
        )

    def test_markdown_renders_tab_groups_without_duplicate_header(self):
        text = render_ai_analysis_markdown(self._tabbed())
        self.assertIn("分类分析", text)
        self.assertIn("━━ 🗞️ 全部新闻 ━━", text)
        self.assertIn("全部新闻态势", text)
        self.assertIn("📰 财经新闻", text)
        # 主标题只出现一次（子结果不再带标题）
        self.assertEqual(text.count("✨ AI 热点分析"), 1)

    def test_plain_renders_tab_groups(self):
        text = render_ai_analysis_plain(self._tabbed())
        self.assertIn("分类分析", text)
        self.assertIn("—— 🗞️ 全部新闻 ——", text)
        self.assertIn("全部新闻态势", text)
        self.assertEqual(text.count("【✨ AI 热点分析"), 1)

    def test_html_renders_tab_dividers(self):
        html = render_ai_analysis_html_rich(self._tabbed())
        self.assertIn("分类分析", html)
        self.assertEqual(html.count("ai-tab-divider"), 2)
        self.assertEqual(html.count("ai-section-header"), 1)
        self.assertIn("全部新闻态势", html)
        self.assertIn("ai-blocks-grid", html)

    def test_single_result_unchanged(self):
        sub = AIAnalysisResult(success=True, core_trends="普通态势")
        html = render_ai_analysis_html_rich(sub)
        self.assertNotIn("ai-tab-divider", html)
        self.assertIn("普通态势", html)


class LoaderTabsTests(unittest.TestCase):
    """loader tabs 配置校验"""

    def test_valid_tabs_dedup_and_filter(self):
        cfg = _load_ai_analysis_config({
            "ai_analysis": {"tabs": ["finance", "ai", "invalid", "AI", "finance"]},
        })
        self.assertEqual(cfg["TABS"], ["finance", "ai"])

    def test_default_empty(self):
        cfg = _load_ai_analysis_config({})
        self.assertEqual(cfg["TABS"], [])


if __name__ == "__main__":
    unittest.main()
