# coding=utf-8
"""
Web 设置中心配置读写测试

覆盖：密钥脱敏、round-trip 注释保留、密钥/env 字段写入防护、
原始 YAML 编辑、白名单路径解析、URL/结构校验、审计日志。
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from trendradar.webadmin import config_io, runner

FIXTURE_CONFIG = """# ═══════════════════
#  TrendRadar 配置文件
# ═══════════════════

app:
  timezone: "Asia/Shanghai"   # 时区注释测试
  show_version_update: true

schedule:
  enabled: false
  preset: "always_on"

platforms:
  enabled: true
  api_url: ""
  sources:
    - id: "weibo"
      name: "微博"
      expected_domain: "weibo.com"
    - id: "zhihu"
      name: "知乎"        # 知乎条目注释
      expected_domain: "zhihu.com"

rss:
  enabled: true
  freshness_filter:
    enabled: true
    max_age_days: 1
  feeds:
    - id: "hacker-news"
      name: "Hacker News"
      url: "https://hnrss.org/frontpage"

report:
  mode: "current"
  display_mode: "keyword"
  sort_by_position_first: false
  rank_threshold: 5
  max_news_per_keyword: 0

filter:
  method: "keyword"
  priority_sort_enabled: true

ai_filter:
  batch_size: 200
  batch_interval: 2
  min_score: 0.7
  reclassify_threshold: 0.6

display:
  region_order:
    - new_items
    - hotlist
  regions:
    hotlist: true
    new_items: false
    rss: true
    standalone: false
    ai_analysis: false
  standalone:
    platforms: []
    rss_feeds: []
    max_items: 20

notification:
  enabled: true
  channels:
    feishu:
      webhook_url: ""
    telegram:
      bot_token: ""
      chat_id: ""

storage:
  backend: "auto"
  formats:
    sqlite: true
    txt: false
    html: true
  local:
    data_dir: "output"
    retention_days: 0
  remote:
    retention_days: 0
    endpoint_url: ""
    bucket_name: ""
    access_key_id: ""
    secret_access_key: ""
    region: ""

ai:
  model: "deepseek/deepseek-v4-flash"
  api_key: "sk-secret-abc123"
  api_base: ""
  timeout: 120
  temperature: 1.0
  max_tokens: 5000
  num_retries: 1
  fallback_models: []

ai_analysis:
  enabled: false
  mode: "follow_report"

ai_translation:
  enabled: false
  language: "中文"
  scope:
    hotlist: false
    rss: false
    standalone: false

advanced:
  debug: false
  crawler:
    request_interval: 2000
    use_proxy: false
    default_proxy: "http://127.0.0.1:10801"
  weight:
    rank: 0.6
    frequency: 0.3
    hotness: 0.1
"""

FREQUENCY_WORDS = "[WORD_GROUPS]\nAI\n人工智能\n\n[GLOBAL_FILTER]\n广告\n"


class ConfigIOBase(unittest.TestCase):
    """公共环境：临时目录 + env 指向临时配置"""

    def setUp(self):
        self._orig_cwd = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "config").mkdir()
        (root / "output" / "logs").mkdir(parents=True)
        self.config_path = root / "config" / "config.yaml"
        self.config_path.write_text(FIXTURE_CONFIG, encoding="utf-8")
        self.freq_path = root / "config" / "frequency_words.txt"
        self.freq_path.write_text(FREQUENCY_WORDS, encoding="utf-8")
        os.chdir(root)
        self.env_patch = patch.dict(
            os.environ,
            {
                "CONFIG_PATH": str(self.config_path),
                "FREQUENCY_WORDS_PATH": str(self.freq_path),
                "TIMEZONE": "",
                "SCHEDULE_ENABLED": "",
                "FEISHU_WEBHOOK_URL": "",
            },
        )
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        os.chdir(self._orig_cwd)
        self.tmp.cleanup()


class GetStructuredConfigTests(ConfigIOBase):

    def test_masks_credentials_and_reports_readonly(self):
        result = config_io.get_structured_config()
        self.assertEqual(result["data"]["ai"]["api_key"], config_io.MASK_TOKEN)
        # 非密钥字段保持原值
        self.assertEqual(result["data"]["ai"]["model"], "deepseek/deepseek-v4-flash")
        self.assertIn("ai.api_key", result["readonly_fields"])
        self.assertIn("notification.channels.feishu.webhook_url", result["readonly_fields"])

    def test_empty_credentials_stay_empty(self):
        result = config_io.get_structured_config()
        self.assertEqual(result["data"]["notification"]["channels"]["feishu"]["webhook_url"], "")

    def test_env_shadowed_reported(self):
        with patch.dict(os.environ, {"TIMEZONE": "Asia/Tokyo"}):
            result = config_io.get_structured_config()
        self.assertEqual(result["env_shadowed"]["app.timezone"], "TIMEZONE")


class SaveStructuredTests(ConfigIOBase):

    def _load_current(self):
        return config_io.get_structured_config()["data"]

    def test_scalar_change_preserves_comments(self):
        data = self._load_current()
        data["app"]["timezone"] = "America/New_York"
        ok, errors, warnings, changed = config_io.save_structured_config(data)
        self.assertTrue(ok, msg=f"errors={errors}")
        self.assertEqual(errors, [])

        text = self.config_path.read_text(encoding="utf-8")
        # 注释保留
        self.assertIn("# 时区注释测试", text)
        self.assertIn("# ═══════════════════", text)
        # 值已更新
        self.assertEqual(yaml.safe_load(text)["app"]["timezone"], "America/New_York")
        # 变更记录
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["path"], "app.timezone")

    def test_backup_created(self):
        data = self._load_current()
        data["report"]["rank_threshold"] = 8
        config_io.save_structured_config(data)
        backups = list(self.config_path.parent.glob("config.yaml.bak-*"))
        self.assertEqual(len(backups), 1)

    def test_audit_log_written(self):
        data = self._load_current()
        data["app"]["show_version_update"] = False
        config_io.save_structured_config(data, source_ip="1.2.3.4")
        audit = Path("output/logs/config-audit.jsonl")
        self.assertTrue(audit.exists())
        content = audit.read_text(encoding="utf-8")
        self.assertIn("config.yaml", content)
        self.assertIn("1.2.3.4", content)

    def test_list_append_with_trailing_comments_keeps_structure(self):
        """回归：feeds 列表带尾部注释块时，追加条目不得错位到顶层文档"""
        fixture = FIXTURE_CONFIG + """
    # 自定义源示例
    # - id: "custom-feed"
    #   name: "自定义源"
"""
        self.config_path.write_text(fixture, encoding="utf-8")

        data = config_io.get_structured_config()["data"]
        data["rss"]["feeds"].append({"id": "new-feed", "name": "新源", "url": "https://x.example/feed"})
        ok, errors, _, _ = config_io.save_structured_config(data)
        self.assertTrue(ok, msg=f"errors={errors}")

        parsed = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        # 顶层文档不能混入 feeds 条目（曾出现的错位 bug）
        self.assertNotIn("id", {k for k in parsed.keys() if k == "id"} or set())
        self.assertIsInstance(parsed.get("report"), dict, "report 段必须仍是映射")
        feeds = parsed["rss"]["feeds"]
        self.assertEqual(len(feeds), 2)  # 原 hacker-news + 新增
        self.assertEqual(feeds[-1]["id"], "new-feed")
        # 原有条目仍在
        self.assertEqual(feeds[0]["id"], "hacker-news")
        # 新增条目不得出现在顶层
        top_keys = set(parsed.keys())
        self.assertNotIn("name", top_keys)

    def test_list_merge_by_id_keeps_comments_and_order(self):
        data = self._load_current()
        sources = data["platforms"]["sources"]
        # 更新知乎名称、删除微博、新增条目
        sources[1]["name"] = "知乎新版"
        data["platforms"]["sources"] = [sources[1], {"id": "douyin", "name": "抖音", "expected_domain": "douyin.com"}]
        ok, errors, _, _ = config_io.save_structured_config(data)
        self.assertTrue(ok, msg=f"errors={errors}")

        text = self.config_path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(text)["platforms"]["sources"]
        self.assertEqual([s["id"] for s in parsed], ["zhihu", "douyin"])
        self.assertEqual(parsed[0]["name"], "知乎新版")
        # 存活条目的行内注释保留
        self.assertIn("# 知乎条目注释", text)
        self.assertNotIn("weibo", text)

    def test_credential_change_rejected(self):
        data = self._load_current()
        data["ai"]["api_key"] = "sk-attacker-key"
        ok, errors, _, _ = config_io.save_structured_config(data)
        self.assertFalse(ok)
        self.assertTrue(any("不允许通过网页修改" in e for e in errors))
        # 文件未被修改
        self.assertIn("sk-secret-abc123", self.config_path.read_text(encoding="utf-8"))

    def test_masked_value_roundtrip(self):
        data = self._load_current()  # api_key 已是掩码
        data["app"]["show_version_update"] = False
        ok, errors, _, _ = config_io.save_structured_config(data)
        self.assertTrue(ok, msg=f"errors={errors}")
        self.assertIn("sk-secret-abc123", self.config_path.read_text(encoding="utf-8"))

    def test_cleared_credential_keeps_original(self):
        data = self._load_current()
        data["ai"]["api_key"] = ""  # 清空也应视为"保持原值"
        ok, errors, _, _ = config_io.save_structured_config(data)
        self.assertTrue(ok, msg=f"errors={errors}")
        self.assertIn("sk-secret-abc123", self.config_path.read_text(encoding="utf-8"))

    def test_env_shadowed_change_ignored_with_warning(self):
        with patch.dict(os.environ, {"TIMEZONE": "Asia/Tokyo"}):
            data = self._load_current()
            data["app"]["timezone"] = "Europe/London"
            ok, errors, warnings, _ = config_io.save_structured_config(data)
            self.assertTrue(ok, msg=f"errors={errors}")
            self.assertTrue(any("TIMEZONE" in w for w in warnings))
            # 文件保持原值
            self.assertEqual(yaml.safe_load(self.config_path.read_text(encoding="utf-8"))["app"]["timezone"], "Asia/Shanghai")

    def test_invalid_data_rejected(self):
        data = self._load_current()
        data["report"]["mode"] = "wrong-mode"
        ok, errors, _, _ = config_io.save_structured_config(data)
        self.assertFalse(ok)
        self.assertTrue(any("report.mode" in e for e in errors))


class SaveRawTests(ConfigIOBase):

    def test_raw_read_masks_secret(self):
        text = config_io.get_config_raw()
        self.assertIn(config_io.MASK_TOKEN, text)
        self.assertNotIn("sk-secret-abc123", text)

    def test_raw_save_restores_mask_token(self):
        text = config_io.get_config_raw()
        ok, errors, _ = config_io.save_config_raw(text)
        self.assertTrue(ok, msg=f"errors={errors}")
        self.assertIn("sk-secret-abc123", self.config_path.read_text(encoding="utf-8"))

    def test_raw_save_rejects_credential_change(self):
        text = config_io.get_config_raw().replace(config_io.MASK_TOKEN, "sk-attacker")
        ok, errors, _ = config_io.save_config_raw(text)
        self.assertFalse(ok)
        self.assertTrue(any("不允许通过网页修改" in e for e in errors))

    def test_raw_save_rejects_invalid_yaml(self):
        ok, errors, _ = config_io.save_config_raw("app: [unclosed")
        self.assertFalse(ok)
        self.assertTrue(any("YAML 语法错误" in e for e in errors))

    def test_raw_save_rejects_non_mapping(self):
        ok, errors, _ = config_io.save_config_raw("- just\n- a\n- list\n")
        self.assertFalse(ok)


class ValidationTests(ConfigIOBase):

    def test_invalid_timezone(self):
        data = yaml.safe_load(FIXTURE_CONFIG)
        data["app"]["timezone"] = "Mars/Olympus"
        errors, _ = config_io.validate_config_data(data)
        self.assertTrue(any("时区" in e for e in errors))

    def test_telegram_pairing_mismatch(self):
        data = yaml.safe_load(FIXTURE_CONFIG)
        data["notification"]["channels"]["telegram"] = {
            "bot_token": "t1;t2", "chat_id": "c1",
        }
        errors, _ = config_io.validate_config_data(data)
        self.assertTrue(any("Telegram" in e for e in errors))

    def test_weight_sum_warning(self):
        data = yaml.safe_load(FIXTURE_CONFIG)
        data["advanced"]["weight"] = {"rank": 0.5, "frequency": 0.3, "hotness": 0.1}
        _, warnings = config_io.validate_config_data(data)
        self.assertTrue(any("权重" in w for w in warnings))

    def test_rss_feed_requires_id_and_url(self):
        data = yaml.safe_load(FIXTURE_CONFIG)
        data["rss"]["feeds"] = [{"name": "无 id 源"}, {"id": "x", "url": "ftp://bad.example"}]
        errors, _ = config_io.validate_config_data(data)
        self.assertTrue(any("缺少 id" in e for e in errors))
        self.assertTrue(any("http" in e for e in errors))

    def test_private_url_warning_and_bad_scheme_error(self):
        data = yaml.safe_load(FIXTURE_CONFIG)
        data["ai"]["api_base"] = "http://192.168.1.5/v1"
        errors, warnings = config_io.validate_config_data(data)
        self.assertEqual(errors, [])
        self.assertTrue(any("内网" in w for w in warnings))

        data["ai"]["api_base"] = "ftp://example.com"
        errors, _ = config_io.validate_config_data(data)
        self.assertTrue(any("http" in e for e in errors))


class WhitelistTests(ConfigIOBase):

    def test_frequency_words_resolved_from_env_path(self):
        path = config_io._resolve_whitelisted("frequency_words.txt")
        self.assertEqual(path, Path(self.freq_path))

    def test_traversal_rejected(self):
        for name in ["../config.yaml", "..\\config.yaml", "/etc/passwd", "ai_filter/../config.yaml", "a/../b"]:
            self.assertIsNone(config_io._resolve_whitelisted(name), msg=name)

    def test_ai_filter_subdir_allowed(self):
        (self.config_path.parent / "ai_filter").mkdir(exist_ok=True)
        (self.config_path.parent / "ai_filter" / "prompt.txt").write_text("p", encoding="utf-8")
        path = config_io._resolve_whitelisted("ai_filter/prompt.txt")
        self.assertIsNotNone(path)
        self.assertEqual(path.name, "prompt.txt")

    def test_list_whitelisted_files(self):
        (self.config_path.parent / "timeline.yaml").write_text("presets: {}\n", encoding="utf-8")
        files = config_io.list_whitelisted_files()
        names = [f["name"] for f in files]
        self.assertIn("frequency_words.txt", names)
        self.assertIn("timeline.yaml", names)


class RunnerTests(ConfigIOBase):

    def test_read_run_log_rejects_invalid_names(self):
        self.assertEqual(runner.read_run_log("../config.yaml"), "")
        self.assertEqual(runner.read_run_log("other.log"), "")

    def test_list_run_logs_empty(self):
        self.assertEqual(runner.list_run_logs(), [])

    def test_find_processes_no_crash(self):
        result = runner.find_trendradar_processes()
        self.assertIsInstance(result, list)


class EditableSecretsTests(ConfigIOBase):
    """EDIT_SECRETS=true 时密钥可编辑的场景"""

    def _with_edit_secrets(self):
        return patch.dict(os.environ, {"EDIT_SECRETS": "true"})

    def test_get_structured_returns_real_values(self):
        with self._with_edit_secrets():
            result = config_io.get_structured_config()
            self.assertTrue(result["secrets_editable"])
            self.assertEqual(result["readonly_fields"], [])
            self.assertEqual(result["data"]["ai"]["api_key"], "sk-secret-abc123")

    def test_get_raw_unmasked(self):
        with self._with_edit_secrets():
            text = config_io.get_config_raw()
            self.assertIn("sk-secret-abc123", text)
            self.assertNotIn(config_io.MASK_TOKEN, text)

    def test_save_credential_change_succeeds(self):
        with self._with_edit_secrets():
            data = config_io.get_structured_config()["data"]
            data["ai"]["api_key"] = "sk-new-key-456"
            ok, errors, _, changed = config_io.save_structured_config(data)
            self.assertTrue(ok, msg=f"errors={errors}")
            self.assertIn("sk-new-key-456", self.config_path.read_text(encoding="utf-8"))
            self.assertTrue(any(c["path"] == "ai.api_key" for c in changed))

    def test_save_raw_credential_change_succeeds(self):
        with self._with_edit_secrets():
            text = config_io.get_config_raw().replace("sk-secret-abc123", "sk-raw-key-789")
            ok, errors, _ = config_io.save_config_raw(text)
            self.assertTrue(ok, msg=f"errors={errors}")
            self.assertIn("sk-raw-key-789", self.config_path.read_text(encoding="utf-8"))

    def test_cleared_credential_writes_empty(self):
        with self._with_edit_secrets():
            data = config_io.get_structured_config()["data"]
            data["ai"]["api_key"] = ""  # 可编辑模式下允许清空
            ok, errors, _, _ = config_io.save_structured_config(data)
            self.assertTrue(ok, msg=f"errors={errors}")
            self.assertEqual(yaml.safe_load(self.config_path.read_text(encoding="utf-8"))["ai"]["api_key"], "")

    def test_env_shadowed_credential_still_ignored(self):
        envs = {"EDIT_SECRETS": "true", "AI_API_KEY": "env-key-000"}
        with patch.dict(os.environ, envs):
            data = config_io.get_structured_config()["data"]
            data["ai"]["api_key"] = "sk-page-key"
            ok, errors, warnings, _ = config_io.save_structured_config(data)
            self.assertTrue(ok, msg=f"errors={errors}")
            self.assertTrue(any("AI_API_KEY" in w for w in warnings))
            # 文件保持原值，页面修改被忽略
            self.assertIn("sk-secret-abc123", self.config_path.read_text(encoding="utf-8"))

    def test_audit_log_redacts_credential_values(self):
        with self._with_edit_secrets():
            data = config_io.get_structured_config()["data"]
            data["ai"]["api_key"] = "sk-audit-secret"
            ok, _, _, _ = config_io.save_structured_config(data)
            self.assertTrue(ok)
            audit = Path("output/logs/config-audit.jsonl").read_text(encoding="utf-8")
            self.assertIn("ai.api_key", audit)
            self.assertNotIn("sk-audit-secret", audit)
            self.assertIn("***", audit)

    def test_stale_mask_token_restored_when_editable(self):
        # 页面在开关切换前加载过（带掩码），保存时掩码应还原而不是写入字面量
        with self._with_edit_secrets():
            data = config_io.get_structured_config()["data"]
            data["ai"]["api_key"] = config_io.MASK_TOKEN
            ok, errors, _, _ = config_io.save_structured_config(data)
            self.assertTrue(ok, msg=f"errors={errors}")
            self.assertIn("sk-secret-abc123", self.config_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
