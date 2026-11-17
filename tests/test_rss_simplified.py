# coding=utf-8
"""
RSS 繁体转简体测试

覆盖：zh-cn 转换函数、feed 级 to_simplified 开关（开启转换/关闭保持原文）。
"""

import unittest
from unittest.mock import MagicMock

from trendradar.crawler.rss.fetcher import RSSFeedConfig, RSSFetcher, _convert_to_simplified

TRADITIONAL_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
<item>
  <title>習近平會晤特朗普</title>
  <link>https://example.com/1</link>
  <description>俄羅斯超級富豪們已經在設想一個「後普京時代」</description>
</item>
</channel></rss>"""


def _fetcher_with(feed_config: RSSFeedConfig) -> RSSFetcher:
    """构造带固定响应的抓取器（离线）"""
    fetcher = RSSFetcher(feeds=[feed_config], request_interval=0, timeout=5)
    response = MagicMock()
    response.text = TRADITIONAL_RSS
    response.raise_for_status = MagicMock()
    response.headers = {}
    fetcher.session.get = MagicMock(return_value=response)
    return fetcher


class ConvertHelperTests(unittest.TestCase):

    def test_traditional_to_simplified(self):
        self.assertEqual(_convert_to_simplified("習近平會晤"), "习近平会晤")

    def test_empty_passthrough(self):
        self.assertEqual(_convert_to_simplified(""), "")
        self.assertEqual(_convert_to_simplified(None), None)

    def test_simplified_untouched(self):
        self.assertEqual(_convert_to_simplified("深圳观察"), "深圳观察")


class FetchConversionTests(unittest.TestCase):

    def test_to_simplified_enabled(self):
        feed = RSSFeedConfig(id="bbc", name="BBC 中文网", url="https://e.com/rss",
                             to_simplified=True)
        items, error = _fetcher_with(feed).fetch_feed(feed)
        self.assertIsNone(error)
        self.assertEqual(items[0].title, "习近平会晤特朗普")
        # zh-cn 含大陆用词转换
        self.assertIn("俄罗斯", items[0].summary)
        self.assertNotIn("習", items[0].title)

    def test_to_simplified_disabled_keeps_original(self):
        feed = RSSFeedConfig(id="bbc", name="BBC 中文网", url="https://e.com/rss")
        items, error = _fetcher_with(feed).fetch_feed(feed)
        self.assertIsNone(error)
        self.assertEqual(items[0].title, "習近平會晤特朗普")


if __name__ == "__main__":
    unittest.main()
