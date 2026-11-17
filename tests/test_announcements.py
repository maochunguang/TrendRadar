# coding=utf-8
"""
上市公司公告采集测试

覆盖：东财/巨潮响应解析（离线 fixture）、时间格式转换、
多源合并截断、loader 配置加载与清洗。
"""

import unittest
from unittest.mock import MagicMock

from trendradar.core.loader import _load_announcements_config
from trendradar.crawler.announcements import (
    FEED_ID,
    FEED_NAME,
    _parse_cninfo_payload,
    _parse_eastmoney_payload,
    _sort_and_cap,
    _iso_from_beijing,
    _iso_from_epoch_ms,
)
from trendradar.storage.base import RSSItem


# 真实接口返回结构的离线样本（截取自 2026-11-17 抓取）
EASTMONEY_PAYLOAD = {
    "data": {
        "list": [
            {
                "art_code": "AN202611161831754209",
                "codes": [
                    {"ann_type": "A,SHA", "market_code": "1",
                     "short_name": "鼎龙科技", "stock_code": "603004"}
                ],
                "columns": [{"column_code": "001002003004002",
                             "column_name": "股东大会资料"}],
                "display_time": "2026-11-16 17:11:48:404",
                "notice_date": "2026-11-17 00:00:00",
                "title": "鼎龙科技:2026年第二次临时股东会会议资料",
            },
            {
                "art_code": "AN202611161831750891",
                "codes": [
                    {"ann_type": "A,SZA", "market_code": "0",
                     "short_name": "英特集团", "stock_code": "000411"}
                ],
                "columns": [
                    {"column_code": "001003001001003", "column_name": "专项说明/独立意见"},
                    {"column_code": "001003001001001", "column_name": "专项说明/独立意见"},
                ],
                "display_time": "2026-11-16 16:09:03:526",
                "title": "英特集团:2026年半年度审计报告",
            },
        ]
    }
}

CNINFO_PAYLOAD = {
    "announcements": [
        {
            "secCode": "301323",
            "secName": "新莱福",
            "announcementId": "1225674887",
            # 标题不含公司名 → 应补前缀
            "announcementTitle": "发行股份及支付现金购买资产并募集配套资金之独立财务顾问报告",
            "announcementTime": 1794873873000,
            "adjunctUrl": "finalpage/2026-11-17/1225674887.PDF",
        },
        {
            "secCode": "600519",
            "secName": "贵州茅台",
            "announcementId": "1225652625",
            # 标题已含公司名 → 不应再补前缀
            "announcementTitle": "贵州茅台关于召开2026年第三季度业绩说明会的公告",
            "announcementTime": 1793635200000,
            "adjunctUrl": "finalpage/2026-11-03/1225652625.PDF",
        },
    ]
}


class EastmoneyParseTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.items, cls.err = _parse_eastmoney_payload(EASTMONEY_PAYLOAD, "Asia/Shanghai", 50)

    def test_no_error(self):
        self.assertIsNone(self.err)
        self.assertEqual(len(self.items), 2)

    def test_common_fields(self):
        first = self.items[0]
        self.assertIsInstance(first, RSSItem)
        self.assertEqual(first.feed_id, FEED_ID)
        self.assertEqual(first.feed_name, FEED_NAME)
        self.assertEqual(first.guid, "AN202611161831754209")
        self.assertEqual(first.title, "鼎龙科技:2026年第二次临时股东会会议资料")
        self.assertEqual(first.author, "鼎龙科技")
        self.assertEqual(first.summary, "股东大会资料")
        self.assertEqual(
            first.url,
            "https://data.eastmoney.com/notices/detail/603004/AN202611161831754209.html",
        )

    def test_time_conversion(self):
        # 北京时间 17:11:48 → ISO 带偏移
        self.assertEqual(
            self.items[0].published_at,
            "2026-11-16T17:11:48+08:00",
        )

    def test_duplicate_column_types_deduped(self):
        # 同名公告类型去重，不重复显示
        self.assertEqual(self.items[1].summary, "专项说明/独立意见")

    def test_max_items_cap(self):
        items, err = _parse_eastmoney_payload(EASTMONEY_PAYLOAD, "Asia/Shanghai", 1)
        self.assertIsNone(err)
        self.assertEqual(len(items), 1)

    def test_empty_payload(self):
        items, err = _parse_eastmoney_payload({"data": {"list": []}}, "Asia/Shanghai", 50)
        self.assertEqual(items, [])
        self.assertIn("空", err)


class CninfoParseTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.items, cls.err = _parse_cninfo_payload(CNINFO_PAYLOAD, "Asia/Shanghai", 30)

    def test_no_error(self):
        self.assertIsNone(self.err)
        self.assertEqual(len(self.items), 2)

    def test_pdf_url_and_guid(self):
        first = self.items[0]
        self.assertEqual(first.guid, "1225674887")
        self.assertEqual(
            first.url,
            "http://static.cninfo.com.cn/finalpage/2026-11-17/1225674887.PDF",
        )

    def test_title_prefix_when_company_missing(self):
        # 标题不含公司名 → 补前缀；已含 → 保持原样
        self.assertTrue(self.items[0].title.startswith("新莱福："))
        self.assertEqual(
            self.items[1].title,
            "贵州茅台关于召开2026年第三季度业绩说明会的公告",
        )

    def test_epoch_time_conversion(self):
        # 1794873873000 = 2026-11-17（北京时间上午）
        self.assertTrue(self.items[0].published_at.startswith("2026-11-17T"))

    def test_empty_payload(self):
        items, err = _parse_cninfo_payload({"announcements": []}, "Asia/Shanghai", 30)
        self.assertEqual(items, [])
        self.assertIn("空", err)


class MergeAndTimeTests(unittest.TestCase):

    def test_sort_and_cap_by_published_desc(self):
        items = [
            RSSItem(title="new", feed_id=FEED_ID, published_at="2026-11-17T10:00:00+08:00"),
            RSSItem(title="old", feed_id=FEED_ID, published_at="2026-11-16T09:00:00+08:00"),
            RSSItem(title="mid", feed_id=FEED_ID, published_at="2026-11-16T18:00:00+08:00"),
            RSSItem(title="nodate", feed_id=FEED_ID, published_at=""),
        ]
        merged = _sort_and_cap(items, 3)
        self.assertEqual([i.title for i in merged], ["new", "mid", "old"])

    def test_invalid_beijing_time_returns_empty(self):
        self.assertEqual(_iso_from_beijing("not-a-time", "Asia/Shanghai"), "")
        self.assertEqual(_iso_from_beijing("", "Asia/Shanghai"), "")

    def test_epoch_invalid_returns_empty(self):
        self.assertEqual(_iso_from_epoch_ms(None, "Asia/Shanghai"), "")
        self.assertEqual(_iso_from_epoch_ms("abc", "Asia/Shanghai"), "")
        self.assertEqual(_iso_from_epoch_ms(0, "Asia/Shanghai"), "")

    def test_timezone_fallback_on_bad_name(self):
        # 非法时区回退 Asia/Shanghai，不抛异常
        result = _iso_from_epoch_ms(1794873873000, "Mars/Olympus")
        self.assertTrue(result.startswith("2026-11-"))


class LoaderTests(unittest.TestCase):

    def test_defaults(self):
        cfg = _load_announcements_config({})
        self.assertFalse(cfg["ENABLED"])
        self.assertEqual(cfg["SOURCE"], "eastmoney")
        self.assertEqual(cfg["MAX_ITEMS"], 30)
        self.assertEqual(cfg["STOCK_POOL"], [])
        self.assertEqual(cfg["CATEGORIES"], [])

    def test_invalid_source_falls_back(self):
        cfg = _load_announcements_config({"announcements": {"source": "ths"}})
        self.assertEqual(cfg["SOURCE"], "eastmoney")

    def test_max_items_clamped(self):
        cfg = _load_announcements_config({"announcements": {"max_items": 999}})
        self.assertEqual(cfg["MAX_ITEMS"], 100)
        cfg = _load_announcements_config({"announcements": {"max_items": -5}})
        self.assertEqual(cfg["MAX_ITEMS"], 0)
        cfg = _load_announcements_config({"announcements": {"max_items": "abc"}})
        self.assertEqual(cfg["MAX_ITEMS"], 30)

    def test_pool_and_categories_cleaned(self):
        cfg = _load_announcements_config({"announcements": {
            "stock_pool": ["600519", "", None, "300750"],
            "categories": ["category_ndbg_szsh", " "],
        }})
        self.assertEqual(cfg["STOCK_POOL"], ["600519", "300750"])
        self.assertEqual(cfg["CATEGORIES"], ["category_ndbg_szsh"])


if __name__ == "__main__":
    unittest.main()


class AIFilterSummaryTests(unittest.TestCase):
    """AI 筛选模式下 RSS 摘要应随条目透传到报告数据"""

    def _convert(self, summary_enabled):
        from trendradar.ai.filter_pipeline import AIFilterPipeline
        from trendradar.ai.filter import AIFilterResult

        pipeline = AIFilterPipeline(
            config={
                "RSS": {"SUMMARY": {"ENABLED": summary_enabled, "MAX_LENGTH": 50}},
                "AI_FILTER": {},
            },
            storage_manager=MagicMock(),
            get_time_func=lambda: "2026-11-17 10:00:00",
        )
        result = AIFilterResult(tags=[{
            "tag": "芯片", "description": "", "position": 1, "count": 1,
            "items": [{
                "title": "AI 芯片新突破",
                "source_id": "techcrunch", "source_name": "TechCrunch",
                "url": "https://e.com/a", "mobile_url": "", "rank": 0,
                "ranks": [], "first_time": "", "last_time": "",
                "summary": "<p>厂商发布新一代 &amp; 芯片</p>",
                "count": 1, "relevance_score": 0.9,
                "source_type": "rss",
            }],
        }], total_matched=1, total_processed=1, success=True)
        return pipeline.convert_to_report_data(result, mode="daily")

    def test_summary_included_when_enabled(self):
        _, rss_stats, _ = self._convert(True)
        entry = rss_stats[0]["titles"][0]
        self.assertIn("summary", entry)
        self.assertIn("厂商发布新一代 & 芯片", entry["summary"])
        self.assertNotIn("<", entry["summary"])

    def test_summary_absent_when_disabled(self):
        _, rss_stats, _ = self._convert(False)
        self.assertNotIn("summary", rss_stats[0]["titles"][0])

    def test_hotlist_entry_no_summary(self):
        from trendradar.ai.filter_pipeline import AIFilterPipeline
        from trendradar.ai.filter import AIFilterResult

        pipeline = AIFilterPipeline(
            config={"RSS": {"SUMMARY": {"ENABLED": True}}, "AI_FILTER": {}},
            storage_manager=MagicMock(),
            get_time_func=lambda: "2026-11-17 10:00:00",
        )
        result = AIFilterResult(tags=[{
            "tag": "华为", "description": "", "position": 1, "count": 1,
            "items": [{
                "title": "华为发布新机", "source_id": "weibo",
                "source_name": "微博", "url": "", "mobile_url": "",
                "rank": 1, "ranks": [1], "first_time": "", "last_time": "",
                "summary": "", "count": 1, "relevance_score": 0.9,
                "source_type": "hotlist",
            }],
        }], total_matched=1, total_processed=1, success=True)
        hotlist_stats, _, _ = pipeline.convert_to_report_data(result, mode="daily")
        self.assertNotIn("summary", hotlist_stats[0]["titles"][0])
