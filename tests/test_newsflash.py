# coding=utf-8
"""
同花顺 7×24 快讯采集测试

覆盖：接口响应解析（离线 fixture）、时间戳转换、loader 配置。
"""

import unittest

from trendradar.core.loader import _load_newsflash_config
from trendradar.crawler.newsflash import (
    FEED_ID,
    FEED_NAME,
    _parse_payload,
)
from trendradar.storage.base import RSSItem

# 真实接口返回结构的离线样本（截取自 2026-11-18 抓取）
PAYLOAD = {
    "code": "200",
    "data": {
        "list": [
            {
                "id": "5700303",
                "seq": "681965450",
                "title": "苏林：中国发展成就证明改革开放正确性",
                "digest": "越共中央总书记、国家主席苏林18日在2026年亚太经合组织（APEC）"
                          "工商领导人峰会上说，过去几十年来取得的跨越式发展成就清晰证明"
                          "了坚持改革开放和创新发展的正确性。(新华社)",
                "url": "https://news.10jqka.com.cn/20261118/c681965450.shtml",
                "ctime": "1794998763",
                "tags": [{"id": "21107", "name": "美股"}, {"id": "21103", "name": "A股"}],
            },
            {
                "id": "5700279",
                "seq": "681965416",
                "title": "麦格米特：控股股东、实控人及部分董监高拟增持公司股份",
                "digest": "麦格米特公告，控股股东、实际控制人童永胜等拟自2026年11月19日起"
                          "6个月内增持公司股份，增持金额3000万元-6000万元。",
                "url": "https://news.10jqka.com.cn/20261118/c681965416.shtml",
                "ctime": "1794998667",
                "tags": [{"id": "34843", "name": "公告"}, {"id": "21103", "name": "A股"}],
            },
        ]
    },
}


class NewsflashParseTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.items, cls.err = _parse_payload(PAYLOAD, "Asia/Shanghai", 30)

    def test_no_error(self):
        self.assertIsNone(self.err)
        self.assertEqual(len(self.items), 2)

    def test_common_fields(self):
        first = self.items[0]
        self.assertIsInstance(first, RSSItem)
        self.assertEqual(first.feed_id, FEED_ID)
        self.assertEqual(first.feed_name, FEED_NAME)
        self.assertEqual(first.guid, "5700303")
        self.assertEqual(first.title, "苏林：中国发展成就证明改革开放正确性")
        self.assertEqual(
            first.url, "https://news.10jqka.com.cn/20261118/c681965450.shtml"
        )
        # digest 作为摘要原文（展示层按 rss.summary 处理）
        self.assertIn("APEC", first.summary)
        # tags 作为作者
        self.assertEqual(first.author, "美股、A股")

    def test_time_conversion(self):
        # 1794998763 ≈ 2026-11-18 上午（北京时间）
        self.assertTrue(self.items[0].published_at.startswith("2026-11-18T"))
        self.assertIn("+08:00", self.items[0].published_at)

    def test_max_items_cap(self):
        items, err = _parse_payload(PAYLOAD, "Asia/Shanghai", 1)
        self.assertIsNone(err)
        self.assertEqual(len(items), 1)

    def test_empty_payload(self):
        items, err = _parse_payload({"data": {"list": []}}, "Asia/Shanghai", 30)
        self.assertEqual(items, [])
        self.assertIn("空", err)

    def test_invalid_time_returns_empty(self):
        payload = {"data": {"list": [{
            "id": "1", "title": "测试", "digest": "摘要",
            "url": "https://e.com/1", "ctime": "not-a-time", "tags": [],
        }]}}
        items, err = _parse_payload(payload, "Asia/Shanghai", 30)
        self.assertIsNone(err)
        self.assertEqual(items[0].published_at, "")


class NewsflashLoaderTests(unittest.TestCase):

    def test_defaults(self):
        cfg = _load_newsflash_config({})
        self.assertFalse(cfg["ENABLED"])
        self.assertEqual(cfg["MAX_ITEMS"], 30)

    def test_clamped(self):
        cfg = _load_newsflash_config({"newsflash": {"max_items": 500}})
        self.assertEqual(cfg["MAX_ITEMS"], 100)
        cfg = _load_newsflash_config({"newsflash": {"max_items": -1}})
        self.assertEqual(cfg["MAX_ITEMS"], 0)


if __name__ == "__main__":
    unittest.main()
