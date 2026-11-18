# coding=utf-8
"""
资讯聚合增强功能测试

覆盖：RSS 摘要管道、推送条数上限、同事件聚类、AI 条目摘要、
OPML 解析、词组结构化编辑 round-trip。
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from trendradar.core.analyzer import count_rss_frequency
from trendradar.core.clustering import apply_clustering_to_stats, cluster_titles, similarity
from trendradar.notification.splitter import apply_item_cap_to_groups
from trendradar.report.formatter import format_title_for_platform
from trendradar.utils.text import clean_summary_text
from trendradar.webadmin.frequency_editor import parse_frequency, serialize_frequency, validate_frequency
from trendradar.webadmin.opml import OpmlError, make_feed_id, parse_opml


class CleanSummaryTests(unittest.TestCase):

    def test_strips_html_and_entities(self):
        text = '<p>OpenAI <b>发布</b> 新模型 &amp; 更多…</p><script>evil()</script>'
        result = clean_summary_text(text, 100)
        self.assertEqual(result, "OpenAI 发布 新模型 & 更多…")
        self.assertNotIn("evil", result)
        self.assertNotIn("<", result)

    def test_truncates(self):
        result = clean_summary_text("很" * 300, 100)
        self.assertEqual(len(result), 101)  # 100 字 + 省略号
        self.assertTrue(result.endswith("…"))

    def test_empty(self):
        self.assertEqual(clean_summary_text("", 100), "")
        self.assertEqual(clean_summary_text(None, 100), "")

    def test_drops_link_only_lines(self):
        # HN RSS 单行元数据组合（hnrss 实际格式）→ 剥除后无正文则整体为空
        text = ('Article URL: https://openai.com/index/chatgpt-wallet/ '
                'Comments URL: https://news.ycombinator.com/item?id=50518501 '
                'Points: 4 # Comments: 1')
        self.assertEqual(clean_summary_text(text, 100), "")

    def test_hnrss_keeps_real_content_after_metadata(self):
        text = ('Article URL: https://x.com '
                'Comments URL: https://b.com '
                'Points: 5 # Comments: 2 '
                'OpenAI 发布新模型，能力提升明显')
        result = clean_summary_text(text, 100)
        self.assertEqual(result, "OpenAI 发布新模型，能力提升明显")

    def test_normal_text_with_colon_not_eaten(self):
        # 正常文本里 "comments: 3" 不能被误伤（hnrss 剥除需签名匹配）
        text = "User comments: 3 things worth noting about the release"
        self.assertEqual(clean_summary_text(text, 100), text)

    def test_keeps_real_content_after_link_lines(self):
        text = ('<p>Article URL: https://a.com</p>'
                '<p>Comments URL: https://b.com</p>'
                '<p>OpenAI 发布新模型，能力提升明显</p>')
        result = clean_summary_text(text, 100)
        self.assertEqual(result, "OpenAI 发布新模型，能力提升明显")


class AnalyzerSummaryTests(unittest.TestCase):

    def _rss_items(self):
        return [{
            "title": "OpenAI 发布新模型",
            "feed_id": "hn",
            "feed_name": "Hacker News",
            "url": "https://example.com/a",
            "published_at": "2026-11-15T08:00:00+08:00",
            "summary": "<p>OpenAI 发布了 <b>GPT-6</b>，能力大幅提升 &amp; 定价更低</p>",
            "author": "",
        }]

    def test_summary_included_when_enabled(self):
        stats, _ = count_rss_frequency(
            self._rss_items(),
            [{"required": [], "normal": [], "group_key": "全部 RSS"}],
            [],
            summary_config={"ENABLED": True, "MAX_LENGTH": 50},
        )
        titles = stats[0]["titles"]
        self.assertTrue(titles[0]["summary"])
        self.assertIn("GPT-6", titles[0]["summary"])
        self.assertNotIn("<", titles[0]["summary"])

    def test_summary_absent_when_disabled(self):
        stats, _ = count_rss_frequency(
            self._rss_items(),
            [{"required": [], "normal": [], "group_key": "全部 RSS"}],
            [],
        )
        self.assertNotIn("summary", stats[0]["titles"][0])


class FormatterSummaryTests(unittest.TestCase):

    def test_rss_summary_line_rendered(self):
        title_data = {
            "title": "标题",
            "source_name": "HN",
            "time_display": "",
            "count": 1,
            "ranks": [1],
            "rank_threshold": 5,
            "url": "https://e.com",
            "mobile_url": "",
            "summary": "这是摘要",
        }
        line = format_title_for_platform("feishu", title_data)
        self.assertIn("💬 这是摘要", line)
        self.assertIn("<font color='grey'>", line)

    def test_ai_summary_line_rendered(self):
        title_data = {
            "title": "标题", "source_name": "微博", "time_display": "",
            "count": 1, "ranks": [1], "rank_threshold": 5,
            "url": "", "mobile_url": "",
            "ai_summary": "AI 摘要内容",
        }
        line = format_title_for_platform("dingtalk", title_data)
        self.assertIn("🤖 AI 摘要内容", line)

    def test_no_summary_no_change(self):
        title_data = {
            "title": "标题", "source_name": "微博", "time_display": "",
            "count": 1, "ranks": [1], "rank_threshold": 5, "url": "", "mobile_url": "",
        }
        line = format_title_for_platform("dingtalk", title_data)
        self.assertNotIn("💬", line)
        self.assertNotIn("🤖", line)

    def test_also_in_rendered(self):
        title_data = {
            "title": "标题", "source_name": "微博", "time_display": "",
            "count": 1, "ranks": [1], "rank_threshold": 5, "url": "", "mobile_url": "",
            "also_in": ["知乎", "头条"],
        }
        line = format_title_for_platform("feishu", title_data)
        self.assertIn("同见: 知乎、头条", line)


class ItemCapTests(unittest.TestCase):

    def _group(self, n, word="词"):
        return {
            "word": word,
            "count": n,
            "titles": [
                {"title": f"t{i}", "source_name": "s", "time_display": "", "count": 1,
                 "ranks": [1], "rank_threshold": 0, "url": "", "mobile_url": "", "is_new": False}
                for i in range(n)
            ],
        }

    def test_no_cap(self):
        groups = [self._group(3), self._group(2)]
        result = apply_item_cap_to_groups(groups, 0)
        self.assertEqual(sum(len(g["titles"]) for g in result), 5)

    def test_cap_truncates_with_notice(self):
        groups = [self._group(3), self._group(4)]
        result = apply_item_cap_to_groups(groups, 4)
        total_real = sum(len(g["titles"]) for g in result)
        # 4 条真实 + 1 条提示
        self.assertEqual(total_real, 5)
        notice = result[-1]["titles"][-1]
        self.assertIn("还有 3 条未展示", notice["title"])

    def test_first_group_bigger_than_cap(self):
        groups = [self._group(10)]
        result = apply_item_cap_to_groups(groups, 3)
        titles = result[0]["titles"]
        self.assertEqual(len(titles), 4)  # 3 条 + 提示
        self.assertIn("还有 7 条未展示", titles[-1]["title"])


class ClusteringTests(unittest.TestCase):

    def test_similarity_identical_and_unrelated(self):
        self.assertEqual(similarity("某地发生地震", "某地发生地震"), 1.0)
        self.assertLess(similarity("今天天气很好", "股市大涨原因"), 0.3)

    def test_cluster_merges_similar_across_platforms(self):
        items = [
            {"title": "某市发生5.6级地震", "source_name": "微博", "count": 3, "ranks": [1]},
            {"title": "某市发生 5.6 级地震", "source_name": "知乎", "count": 1, "ranks": [2]},
            {"title": " unrelated title about weather", "source_name": "HN", "count": 1, "ranks": [5]},
        ]
        merged = cluster_titles(items, threshold=0.6)
        self.assertEqual(len(merged), 2)
        # 主体保留 count 最大的微博
        primary = next(m for m in merged if m["source_name"] == "微博")
        self.assertEqual(primary.get("also_in"), ["知乎"])

    def test_apply_clustering_updates_count(self):
        stats = [{
            "word": "地震",
            "count": 3,
            "titles": [
                {"title": "某市发生5.6级地震", "source_name": "微博", "count": 3, "ranks": [1]},
                {"title": "某市发生5.6级地震了", "source_name": "头条", "count": 1, "ranks": [2]},
                {"title": "某市发生5.6级地震啦", "source_name": "知乎", "count": 1, "ranks": [3]},
            ],
        }]
        merged = apply_clustering_to_stats(stats, 0.5)
        self.assertGreaterEqual(merged, 1)
        self.assertEqual(stats[0]["count"], len(stats[0]["titles"]))


class _FakeAIResponse:
    def __init__(self, content):
        self.choices = [MagicMock(message=MagicMock(content=content))]


class SummarizerTests(unittest.TestCase):

    def _config(self):
        return {
            "AI": {"MODEL": "deepseek/test", "API_KEY": "sk-x"},
            "AI_SUMMARY": {"ENABLED": True, "MAX_ITEMS": 10, "MAX_CHARS": 40,
                           "BATCH_SIZE": 5, "BATCH_INTERVAL": 0},
        }

    def test_disabled_returns_none(self):
        from trendradar.ai.summarizer import create_item_summarizer
        cfg = self._config()
        cfg["AI_SUMMARY"]["ENABLED"] = False
        self.assertIsNone(create_item_summarizer(cfg))

    def test_no_api_key_returns_none(self):
        from trendradar.ai.summarizer import create_item_summarizer
        cfg = self._config()
        cfg["AI"] = {"MODEL": "x", "API_KEY": ""}
        self.assertIsNone(create_item_summarizer(cfg))

    def test_parse_numbered_response(self):
        from trendradar.ai.summarizer import AIItemSummarizer
        s = AIItemSummarizer(self._config()["AI"], self._config()["AI_SUMMARY"])
        parsed = s._parse_numbered_response(
            "[1] 第一条摘要\n垃圾行\n[2] 第二条摘要", 2
        )
        self.assertEqual(parsed[1], "第一条摘要")
        self.assertEqual(parsed[2], "第二条摘要")
        self.assertEqual(len(parsed), 2)

    @patch("trendradar.ai.summarizer.AIClient")
    def test_attach_to_stats(self, mock_client_cls):
        mock_client_cls.return_value.chat.return_value = "[1] 摘要A\n[2] 摘要B"
        from trendradar.ai.summarizer import AIItemSummarizer
        s = AIItemSummarizer(self._config()["AI"], self._config()["AI_SUMMARY"])
        stats = [{
            "word": "AI",
            "count": 2,
            "titles": [
                {"title": "标题A", "ranks": [1]},
                {"title": "标题B", "ranks": [2]},
            ],
        }]
        attached = s.attach_to_stats(stats, quiet=True)
        self.assertEqual(attached, 2)
        self.assertEqual(stats[0]["titles"][0]["ai_summary"], "摘要A")


class OpmlTests(unittest.TestCase):

    OPML = """<?xml version="1.0" encoding="UTF-8"?>
    <opml version="2.0">
      <head><title>subs</title></head>
      <body>
        <outline text="技术">
          <outline type="rss" text="Hacker News" title="Hacker News" xmlUrl="https://hnrss.org/frontpage"/>
          <outline type="rss" text="阮一峰" xmlUrl="http://www.ruanyifeng.com/blog/atom.xml"/>
        </outline>
        <outline type="rss" text="重复源" xmlUrl="https://hnrss.org/frontpage"/>
      </body>
    </opml>"""

    def test_parse_feeds_flattened_and_deduped(self):
        feeds = parse_opml(self.OPML)
        self.assertEqual(len(feeds), 2)
        self.assertEqual(feeds[0]["name"], "Hacker News")
        self.assertEqual(feeds[0]["url"], "https://hnrss.org/frontpage")

    def test_invalid_xml_raises(self):
        with self.assertRaises(OpmlError):
            parse_opml("not xml")

    def test_no_feeds_raises(self):
        with self.assertRaises(OpmlError):
            parse_opml('<?xml version="1.0"?><opml><body></body></opml>')

    def test_make_feed_id_unique_and_safe(self):
        ids = []
        first = make_feed_id("Hacker News", ids)
        second = make_feed_id("Hacker News", ids + [first])
        self.assertEqual(first, "hacker-news")
        self.assertEqual(second, "hacker-news-2")


SAMPLE_FREQ = """# ═══ 头部注释保留 ═══

[GLOBAL_FILTER]
震惊

[WORD_GROUPS]
!赌博

[华为]
+华为
鸿蒙
/\\bAI\\b/i => AI
@10

[苹果]
苹果
"""


class FrequencyEditorTests(unittest.TestCase):

    def test_parse_roundtrip(self):
        model = parse_frequency(SAMPLE_FREQ)
        self.assertIn("头部注释保留", model["header"])
        self.assertEqual(len(model["global_filters"]), 1)
        self.assertEqual(model["global_filters"][0]["text"], "震惊")
        self.assertEqual(len(model["filters"]), 1)
        self.assertEqual(model["filters"][0]["text"], "赌博")
        self.assertEqual(len(model["groups"]), 2)

        huawei = model["groups"][0]
        self.assertEqual(huawei["alias"], "华为")
        self.assertEqual(huawei["required"][0]["text"], "华为")
        self.assertEqual(len(huawei["normal"]), 2)
        self.assertEqual(huawei["max_count"], 10)

        # round-trip: 序列化后再解析应等价
        text = serialize_frequency(model)
        model2 = parse_frequency(text)
        self.assertEqual(model2["global_filters"][0]["text"], "震惊")
        self.assertEqual(model2["filters"][0]["text"], "赌博")
        self.assertEqual(model2["groups"][0]["alias"], "华为")
        self.assertEqual(model2["groups"][0]["max_count"], 10)
        self.assertEqual(model2["groups"][0]["normal"][1]["kind"], "regex")
        self.assertEqual(model2["groups"][0]["normal"][1]["display"], "AI")

    def test_serialized_output_parses_by_main_program(self):
        from trendradar.core.frequency import load_frequency_words
        model = parse_frequency(SAMPLE_FREQ)
        text = serialize_frequency(model)

        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(text)
            tmp = f.name
        try:
            groups, filter_words, global_filters = load_frequency_words(tmp)
            self.assertEqual(len(groups), 2)
            self.assertEqual(len(filter_words), 1)  # !赌博
            self.assertEqual(len(global_filters), 1)  # 震惊
        finally:
            os.unlink(tmp)

    def test_validate_catches_bad_regex(self):
        model = parse_frequency(SAMPLE_FREQ)
        model["groups"][0]["normal"].append({"kind": "regex", "text": "([unclosed", "flags": "", "display": ""})
        errors = validate_frequency(model)
        self.assertTrue(any("正则无效" in e for e in errors))


if __name__ == "__main__":
    unittest.main()


class HtmlAiSummaryRenderTests(unittest.TestCase):
    """热榜条目的 ai_summary 应渲染到 HTML 报告（此前只有 CSS 没有渲染点）"""

    def _report_data(self):
        return {
            "stats": [{
                "word": "华为",
                "count": 1,
                "position": 1,
                "titles": [{
                    "title": "华为发布新机",
                    "source_name": "微博",
                    "url": "https://e.com/a",
                    "mobile_url": "",
                    "ranks": [1],
                    "rank_threshold": 5,
                    "count": 1,
                    "time_display": "",
                    "ai_summary": "华为发布搭载新芯片的旗舰机型",
                }],
            }],
            "new_titles": [],
            "total_new_count": 0,
            "rss_matched_count": 0,
            "failed_ids": [],
        }

    def test_ai_summary_rendered_in_hotlist(self):
        from trendradar.report.html import render_html_content

        html = render_html_content(self._report_data(), total_titles=1)
        self.assertIn('<div class="item-summary">🤖 华为发布搭载新芯片的旗舰机型</div>', html)

    def test_absent_when_no_ai_summary(self):
        from trendradar.report.html import render_html_content

        data = self._report_data()
        del data["stats"][0]["titles"][0]["ai_summary"]
        html = render_html_content(data, total_titles=1)
        self.assertNotIn('<div class="item-summary">', html)


class PrepareReportDataTests(unittest.TestCase):
    """prepare_report_data 重建 title_data 时应保留可选展示字段"""

    def test_preserves_ai_summary_and_matched_keyword(self):
        from trendradar.report.generator import prepare_report_data

        stats = [{
            "word": "华为", "count": 1, "percentage": 10,
            "titles": [{
                "title": "华为发布新机", "source_name": "微博",
                "time_display": "", "count": 1, "ranks": [1],
                "rank_threshold": 5, "url": "", "mobile_url": "",
                "is_new": False, "rank_timeline": [],
                "ai_summary": "华为发布新旗舰",
                "summary": "原文摘要",
                "matched_keyword": "华为",
            }],
        }]
        rd = prepare_report_data(stats, mode="daily")
        title = rd["stats"][0]["titles"][0]
        self.assertEqual(title["ai_summary"], "华为发布新旗舰")
        self.assertEqual(title["summary"], "原文摘要")
        self.assertEqual(title["matched_keyword"], "华为")


class SummaryExpandTests(unittest.TestCase):
    """网页摘要：默认 2 行截断 + 点击展开；max_length=0 时展示源全文"""

    def test_max_length_zero_keeps_full_text(self):
        from trendradar.utils.text import clean_summary_text

        text = "很" * 400
        self.assertEqual(clean_summary_text(text, 0), text)

    def test_report_contains_clamp_and_toggle(self):
        from trendradar.report.html import render_html_content

        rd = {
            "stats": [], "new_titles": [], "total_new_count": 0,
            "failed_ids": [], "rss_matched_count": 0,
        }
        rss_items = [{
            "word": "rss", "count": 1, "position": 1,
            "titles": [{
                "title": "RSS 标题", "source_name": "BBC", "url": "https://e.com",
                "mobile_url": "", "ranks": [], "rank_threshold": 5, "count": 1,
                "time_display": "", "summary": "很" * 400,
            }],
        }]
        html = render_html_content(rd, total_titles=0, rss_items=rss_items)
        self.assertIn("-webkit-line-clamp: 2", html)
        self.assertIn("initSummaryToggle", html)
        self.assertIn("summary-expanded", html)


class TopTabTests(unittest.TestCase):
    """主报告三 tab：财经新闻 / GitHub / AI 新闻"""

    def _report_data(self):
        return {
            "stats": [{
                "word": "华为",
                "count": 1,
                "position": 1,
                "titles": [{
                    "title": "华为发布新机",
                    "source_name": "微博",
                    "url": "https://e.com/a",
                    "mobile_url": "",
                    "ranks": [1],
                    "rank_threshold": 5,
                    "count": 1,
                    "time_display": "",
                }],
            }],
            "new_titles": [],
            "total_new_count": 0,
            "rss_matched_count": 0,
            "failed_ids": [],
        }

    def test_tab_bar_rendered(self):
        from trendradar.report.html import render_html_content

        html = render_html_content(self._report_data(), total_titles=1)
        self.assertIn("switchTopTab('top-tab-others')", html)
        self.assertIn("switchTopTab('top-tab-finance')", html)
        self.assertIn("switchTopTab('top-tab-github')", html)
        self.assertIn("switchTopTab('top-tab-ai')", html)
        self.assertIn('id="top-tab-others"', html)
        self.assertIn('id="top-tab-finance"', html)
        self.assertIn('id="top-tab-github"', html)
        self.assertIn('id="top-tab-ai"', html)

    def test_others_is_default_tab(self):
        from trendradar.report.html import render_html_content

        html = render_html_content(self._report_data(), total_titles=1)
        self.assertIn('id="top-tab-others" class="top-tab-panel top-tab-active"', html)
        self.assertIn('id="top-tab-finance" class="top-tab-panel"', html)

    def test_others_panel_filters_matched_and_ai(self):
        from trendradar.report.html import render_html_content

        data = self._report_data()  # 热榜命中："华为发布新机"
        all_hotlist = {
            "weibo": {
                "华为发布新机": {"ranks": [1], "url": "https://e.com/a", "mobileUrl": ""},
                "某明星官宣结婚": {"ranks": [2], "url": "https://e.com/b", "mobileUrl": ""},
                "DeepSeek 发布新模型": {"ranks": [3], "url": "https://e.com/c", "mobileUrl": ""},
            },
        }
        html = render_html_content(
            data, total_titles=3, all_hotlist_data=all_hotlist,
        )
        self.assertIn("某明星官宣结婚", html)
        self.assertNotIn("华为发布新机", html.split('id="top-tab-others"')[1].split('id="top-tab-finance"')[0])
        self.assertNotIn("DeepSeek 发布新模型", html.split('id="top-tab-others"')[1].split('id="top-tab-finance"')[0])

    def test_others_panel_includes_raw_rss(self):
        from trendradar.report.html import render_html_content

        raw_rss = [{"title": "未命中词组的一条 RSS", "url": "https://r.com/x",
                    "feed_id": "hacker-news", "summary": ""}]
        html = render_html_content(
            self._report_data(), total_titles=1, raw_rss_items=raw_rss,
        )
        self.assertIn("未命中词组的一条 RSS", html)
        self.assertIn("未命中词组的 RSS", html)

    def test_others_panel_empty_state(self):
        from trendradar.report.html import render_html_content

        # 全量只有已命中的那条 → 无其他新闻
        all_hotlist = {"weibo": {"华为发布新机": {"ranks": [1], "url": "", "mobileUrl": ""}}}
        html = render_html_content(
            self._report_data(), total_titles=1, all_hotlist_data=all_hotlist,
        )
        self.assertIn("本轮没有其他新闻", html)

    def test_github_panel_embedded(self):
        from trendradar.report.html import render_html_content

        page_data = {"github-daily": [{
            "rank": 1, "repo": "a/one", "owner": "a", "description": "d",
            "language": "Python", "stars": 100, "period_stars": 5,
            "topics": [], "url": "https://github.com/a/one", "source": "trending",
        }]}
        html = render_html_content(
            self._report_data(), total_titles=1, github_page_data=page_data,
        )
        self.assertIn("gh-section", html)
        self.assertIn("a/one", html)

    def test_github_panel_placeholder_when_no_data(self):
        from trendradar.report.html import render_html_content

        html = render_html_content(self._report_data(), total_titles=1)
        self.assertIn("本轮未抓取 GitHub 榜单", html)

    def test_ai_tab_filters_entries(self):
        from trendradar.report.html import render_html_content

        data = self._report_data()
        data["stats"][0]["titles"][0]["title"] = "DeepSeek 发布新模型"
        rss_items = [{"word": "AI", "count": 1, "titles": [{
            "title": "OpenAI 融资新闻", "url": "https://r.com/1",
            "source_name": "HN", "summary": "s", "time_display": "",
        }]}]
        html = render_html_content(data, total_titles=1, rss_items=rss_items)
        self.assertIn("🔥 热榜 AI 相关", html)
        self.assertIn("📡 RSS AI 相关", html)
        self.assertIn("DeepSeek 发布新模型", html)
        self.assertIn("OpenAI 融资新闻", html)

    def test_ai_tab_empty_state(self):
        from trendradar.report.html import render_html_content

        html = render_html_content(self._report_data(), total_titles=1)
        self.assertIn("本轮新闻中没有 AI 相关内容", html)


class AiKeywordTests(unittest.TestCase):
    """AI 标题关键词匹配"""

    def test_matches(self):
        from trendradar.report.helpers import is_ai_related

        for t in ["DeepSeek 发布新模型", "AI agent 大战", "大模型降价",
                  "OpenAI 收购 io", "GPT-5 发布", "智能体元年", "算力概念股走强"]:
            self.assertTrue(is_ai_related(t), t)

    def test_non_matches(self):
        from trendradar.report.helpers import is_ai_related

        for t in ["华为发布新机", "said something", "A股三大指数收涨",
                  "雪碧新品上市", " Certain brands ranked higher"]:
            self.assertFalse(is_ai_related(t), t)
