# coding=utf-8
"""
新闻数据服务测试

覆盖：只读库访问（热榜/RSS/搜索/日期）、RSS 2.0 构建、feed 路由 token 鉴权。
"""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from trendradar.webadmin import news_data


class NewsDataBase(unittest.TestCase):
    """临时 output 目录 + 样例库"""

    def setUp(self):
        self._orig_cwd = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        news_dir = root / "output" / "news"
        rss_dir = root / "output" / "rss"
        news_dir.mkdir(parents=True)
        rss_dir.mkdir(parents=True)
        os.chdir(root)

        # 热榜库
        conn = sqlite3.connect(news_dir / "2026-11-15.db")
        conn.executescript("""
            CREATE TABLE platforms (id TEXT PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE news_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                platform_id TEXT NOT NULL, rank INTEGER NOT NULL, url TEXT DEFAULT '',
                mobile_url TEXT DEFAULT '', first_crawl_time TEXT, last_crawl_time TEXT,
                crawl_count INTEGER DEFAULT 1);
            INSERT INTO platforms VALUES ('weibo', '微博');
            INSERT INTO news_items (title, platform_id, rank, url, first_crawl_time, last_crawl_time, crawl_count)
            VALUES ('某地发生地震', 'weibo', 3, 'https://example.com/a', '2026-11-15 08:00:00', '2026-11-15 09:00:00', 5);
        """)
        conn.commit()
        conn.close()

        # RSS 库
        conn = sqlite3.connect(rss_dir / "2026-11-15.db")
        conn.executescript("""
            CREATE TABLE rss_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, feed_id TEXT, url TEXT,
                guid TEXT, published_at TEXT, summary TEXT, author TEXT,
                first_crawl_time TEXT, last_crawl_time TEXT, crawl_count INTEGER DEFAULT 1);
            INSERT INTO rss_items (title, feed_id, url, published_at, summary, author, first_crawl_time, last_crawl_time)
            VALUES ('AI 芯片新突破', 'hn', 'https://example.com/b', '2026-11-15T07:30:00',
                    '<p>厂商发布新一代 &amp; 芯片</p>', '', '2026-11-15 07:31:00', '2026-11-15 07:31:00');
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        os.chdir(self._orig_cwd)
        self.tmp.cleanup()

    def test_list_dates(self):
        dates = news_data.list_news_dates()
        self.assertEqual(dates["hotlist"], ["2026-11-15"])
        self.assertEqual(dates["rss"], ["2026-11-15"])

    def test_get_hotlist(self):
        items = news_data.get_hotlist(days=1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "微博")
        self.assertEqual(items[0]["rank"], 3)
        self.assertEqual(items[0]["count"], 5)
        self.assertEqual(items[0]["date"], "2026-11-15")

    def test_get_hotlist_rejects_bad_date(self):
        self.assertEqual(news_data.get_hotlist(date="../../etc", days=1), [])

    def test_get_rss_items_with_summary(self):
        items = news_data.get_rss_items(days=1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["feed_id"], "hn")
        self.assertIn("芯片", items[0]["summary"])

    def test_search_news(self):
        self.assertEqual(len(news_data.search_news("地震", days=7)), 1)
        self.assertEqual(len(news_data.search_news("芯片", days=7)), 1)
        self.assertEqual(len(news_data.search_news("不存在的词xyz", days=7)), 0)

    def test_build_rss_feed_structure(self):
        items = news_data.get_hotlist(days=1)
        xml = news_data.build_rss_feed("T", "http://x", "d", items, max_items=10)
        self.assertIn("<rss version=\"2.0\">", xml)
        self.assertIn("某地发生地震", xml)
        self.assertIn("【微博 第3名·在榜5次】", xml)
        self.assertIn("pubDate", xml)

    def test_build_rss_feed_escapes_html(self):
        items = [{"title": "<b>标题</b>", "url": "http://x?a=1&b=2", "source": "S",
                  "rank": 1, "count": 1, "date": "2026-11-15", "summary": ""}]
        xml = news_data.build_rss_feed("T", "http://x", "d", items)
        self.assertNotIn("<b>标题</b>", xml)  # 已转义
        self.assertIn("&amp;", xml)

    def test_build_rss_feed_date_fallback(self):
        """时间字段缺失/异常时回退到数据日期（DSA 等消费方要求带日期）"""
        items = [{"title": "t", "url": "u", "source": "S", "rank": 1, "count": 1,
                  "last_time": "00-50", "date": "2026-11-15"}]
        xml = news_data.build_rss_feed("T", "http://x", "d", items)
        self.assertIn("15 Nov 2026", xml)



class FeedTokenTests(unittest.TestCase):

    def test_news_token_ok_unset(self):
        from trendradar.webadmin.api import news_token_ok
        with patch_dict_env({}):
            self.assertTrue(news_token_ok(type("R", (), {"query_params": {}, "headers": {}})()))

    def test_news_token_ok_set(self):
        from trendradar.webadmin.api import news_token_ok
        req = type("R", (), {"query_params": {"token": "abc"}, "headers": {}})()
        with patch_dict_env({"NEWS_API_TOKEN": "abc"}):
            self.assertTrue(news_token_ok(req))
        bad = type("R", (), {"query_params": {}, "headers": {"X-API-Token": "wrong"}})()
        with patch_dict_env({"NEWS_API_TOKEN": "abc"}):
            self.assertFalse(news_token_ok(bad))


def patch_dict_env(env):
    from unittest.mock import patch
    return patch.dict(os.environ, env)


class KwFeedIncludesRssTests(NewsDataBase):
    """kw 词组订阅源应同时并入词组命中的 RSS 条目（含公司公告）"""

    def _run_get_feed(self, name):
        import asyncio

        from trendradar.webadmin.api import get_feed

        req = type("R", (), {
            "path_params": {"name": name},
            "query_params": {},
            "headers": {},
            "base_url": "http://testserver/",
        })()
        return asyncio.run(get_feed(req))

    def test_kw_feed_merges_matched_rss(self):
        # 词组文件：命中 fixture RSS 标题中的"芯片"
        cfg_dir = os.path.join(os.getcwd(), "config")
        os.makedirs(cfg_dir, exist_ok=True)
        with open(os.path.join(cfg_dir, "frequency_words.txt"), "w", encoding="utf-8") as f:
            f.write("[测试组]\n芯片\n")

        resp = self._run_get_feed("kw-测试组.xml")
        self.assertEqual(resp.status_code, 200)
        xml = resp.body.decode("utf-8")
        # 词组命中的 RSS 条目并入；未命中的热榜条目不出现
        self.assertIn("AI 芯片新突破", xml)
        self.assertNotIn("某地发生地震", xml)


if __name__ == "__main__":
    unittest.main()
