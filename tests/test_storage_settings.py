import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
from astrbot_plugin_tieba_lens.settings import DEFAULTS, ENUMS, GROUP_KEYS, effective_settings, public_settings, validate_patch
from astrbot_plugin_tieba_lens.storage import Store, cache_key


class SettingsTests(unittest.TestCase):
    def test_render_environment_defaults_and_enum_validation_match_schema(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        for key, expected, options in (
            ("render_backend", "auto", {"auto", "service", "local"}),
            ("browser_engine", "headless", {"headless", "chromium"}),
        ):
            self.assertEqual(DEFAULTS[key], expected)
            self.assertEqual(effective_settings({})[key], expected)
            self.assertEqual(schema[key]["default"], expected)
            self.assertEqual(set(schema[key]["options"]), options)
            self.assertEqual(ENUMS[key], options)
            for value in options:
                self.assertEqual(validate_patch({key: value}), {key: value})
            for value in ("unknown", True, None, 1):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    validate_patch({key: value})

    def test_render_environment_is_global_and_does_not_change_content_cache(self):
        config = {"render_backend": "local", "browser_engine": "chromium",
                  "group_overrides": {"12345": {"theme": "miku"}}}
        public = public_settings(effective_settings(config, "12345"))
        self.assertEqual(public["render_backend"], "local")
        self.assertEqual(public["browser_engine"], "chromium")
        for key, value in (("render_backend", "service"), ("browser_engine", "headless")):
            self.assertNotIn(key, GROUP_KEYS)
            with self.assertRaises(ValueError):
                validate_patch({key: value}, group=True)
            with self.assertRaises(ValueError):
                validate_patch({"group_overrides": {"12345": {key: value}}})
            target = {"url": "https://tieba.baidu.com/p/123"}
            self.assertEqual(cache_key(target, DEFAULTS), cache_key(target, {**DEFAULTS, key: value}))

    def test_font_default_and_options_match_schema(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(DEFAULTS["font_override"], "lxgw")
        self.assertEqual(effective_settings({})["font_override"], "lxgw")
        self.assertEqual(schema["font_override"]["default"], "lxgw")
        self.assertEqual(schema["font_override"]["options"], ["lxgw", "custom"])
        self.assertEqual(validate_patch({"font_override": "custom"}), {"font_override": "custom"})

    def test_legacy_fonts_migrate_without_losing_global_group_or_single_overrides(self):
        for old in ("auto", "system", "runyuan", "tangmeiren"):
            with self.subTest(old=old):
                config = {"font_override": old, "group_overrides": {"12345": {"font_override": old, "theme": "miku"}, "23456": {"font_override": "custom"}}}
                result = effective_settings(config)
                self.assertEqual(result["font_override"], "lxgw")
                self.assertEqual(result["group_overrides"]["12345"], {"font_override": "lxgw", "theme": "miku"})
                self.assertEqual(effective_settings(config, "12345")["font_override"], "lxgw")
                self.assertEqual(effective_settings(config, "23456")["font_override"], "custom")
                self.assertEqual(validate_patch({"font_override": old}, group=True), {"font_override": "lxgw"})
                self.assertEqual(config["font_override"], old)
                self.assertEqual(config["group_overrides"]["12345"]["font_override"], old)
        self.assertEqual(effective_settings({"font_override": "custom"})["font_override"], "custom")
        with self.assertRaises(ValueError):
            validate_patch({"font_override": "arbitrary"})

    def test_denia_default_matches_schema_and_preserves_explicit_other_themes(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(DEFAULTS["theme"], "denia")
        self.assertEqual(schema["theme"]["default"], "denia")
        self.assertEqual(effective_settings({})["theme"], "denia")
        self.assertIn("denia", schema["theme"]["options"])
        self.assertNotIn("daniya", schema["theme"]["options"])
        self.assertNotIn("danyia", schema["theme"]["options"])
        self.assertEqual(effective_settings({"theme": "paper"})["theme"], "paper")

    def test_legacy_theme_names_normalize_global_group_and_per_parse_options(self):
        for old in ("daniya", "danyia"):
            with self.subTest(old=old):
                config = {"theme": old, "group_overrides": {"12345": {"theme": old, "show_source": False}}}
                result = effective_settings(config)
                self.assertEqual(result["theme"], "denia")
                self.assertEqual(result["group_overrides"]["12345"], {"theme": "denia", "show_source": False})
                self.assertEqual(effective_settings(config, "12345")["theme"], "denia")
                self.assertEqual(validate_patch({"theme": old}), {"theme": "denia"})
                self.assertEqual(validate_patch({"theme": old}, group=True), {"theme": "denia"})
                self.assertEqual(validate_patch(config)["group_overrides"]["12345"]["theme"], "denia")
                self.assertEqual(config["theme"], old)
                self.assertEqual(config["group_overrides"]["12345"]["theme"], old)
        self.assertEqual(effective_settings({"theme": "daniya", "group_overrides": {"12345": {"theme": "midnight"}}}, "12345")["theme"], "midnight")
        self.assertEqual(effective_settings({"theme": "midnight", "group_overrides": {"12345": {"theme": "danyia"}}}, "12345")["theme"], "denia")

    def test_hot_default_matches_schema_and_respects_explicit_modes(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(DEFAULTS["sort"], "hot")
        self.assertEqual(schema["sort"]["default"], "hot")
        self.assertEqual(effective_settings({})["sort"], "hot")
        for mode in ["default", "latest", "hot"]:
            self.assertEqual(effective_settings({"sort": mode})["sort"], mode)
        config = {"sort": "hot", "group_overrides": {"12345": {"sort": "latest"}}}
        self.assertEqual(effective_settings(config, "12345")["sort"], "latest")
        self.assertEqual(effective_settings(config, "99999")["sort"], "hot")

    def test_type_bounds_and_unknown_fields(self):
        for patch in [{"comment_limit": -1}, {"comment_limit": True}, {"request_delay": float("nan")},
                      {"theme": "oops"}, {"enabled": "true"}, {"unexpected": 1},
                      {"cookie": "bad\r\nheader"}]:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                validate_patch(patch)

    def test_group_override_and_blacklist(self):
        config = {"comment_limit": 12, "blacklist": ["12345"],
                  "group_overrides": {"12345": {"comment_limit": 30, "theme": "midnight"}}}
        result = effective_settings(config, "12345")
        self.assertEqual(result["comment_limit"], 30)
        self.assertEqual(result["blacklist"], ["12345"])
        self.assertEqual(effective_settings(config, "99999")["comment_limit"], 12)
        self.assertEqual(config["comment_limit"], 12)
        with self.assertRaises(ValueError):
            validate_patch({"group_overrides": {"12345": {"cookie": "secret"}}})

    def test_public_settings_does_not_contain_secret(self):
        result = public_settings({**DEFAULTS, "cookie": "BDUSS=secret"})
        self.assertNotIn("secret", json.dumps(result))
        self.assertTrue(result["cookie_set"])
        self.assertNotIn("cookie", result)

    def test_fetch_affecting_options_change_cache(self):
        target = {"url": "https://tieba.baidu.com/p/123456"}
        one = cache_key(target, DEFAULTS)
        self.assertNotEqual(one, cache_key(target, {**DEFAULTS, "include_images": False}))
        self.assertNotEqual(one, cache_key(target, {**DEFAULTS, "cookie": "other-user"}))
        self.assertNotEqual(one, cache_key(target, {**DEFAULTS, "comment_limit": 0}))
        self.assertNotEqual(one, cache_key(target, {**DEFAULTS, "sort": "default"}))
        self.assertNotEqual(one, cache_key(target, {**DEFAULTS, "sort": "latest"}))
        self.assertEqual(one, cache_key(target, {**DEFAULTS, "theme": "midnight"}))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_persistent_jobs_and_interrupted_recovery(self):
        self.store.save_job({"id": "abc", "status": "fetching", "document": {"text": "long"}})
        self.assertNotIn("document", self.store.list_jobs()[0])
        self.store.recover()
        self.assertEqual(self.store.get_job("abc")["status"], "failed")
        self.assertIn("重新解析", self.store.get_job("abc")["error"])

    def test_recover_sending_preserves_ready_results_and_marks_delivery_unknown(self):
        job = {"id": "sending", "status": "sending", "result_ready": True,
               "files": ["card.png"], "document": {"text": "完整正文"}, "delivered_units": 1}
        self.store.save_job(job)
        self.store.recover()
        result = self.store.get_job(job["id"])
        self.assertEqual(result["status"], "delivery_failed")
        self.assertEqual(result["delivery_status"], "unknown")
        self.assertIn("未知", result["error"])
        self.assertTrue(result["result_ready"])
        self.assertEqual(result["files"], job["files"])
        self.assertEqual(result["document"], job["document"])
        self.assertEqual(result["delivered_units"], 1)

    def test_recover_preserves_terminal_delivery_failure(self):
        job = {"id": "delivery_failed", "status": "delivery_failed", "result_ready": True,
               "error": "QQ 图片上传连接超时", "files": ["card.png"]}
        self.store.save_job(job)
        self.store.recover()
        self.assertEqual(self.store.get_job(job["id"]), job)

    def test_cache_expires_and_no_zero_minute_cache(self):
        self.store.put_cache("test", {"posts": ["原文"]}, 1)
        self.assertEqual(self.store.get_cache("test")["posts"], ["原文"])
        self.store.put_cache("disabled", {}, 0)
        self.assertIsNone(self.store.get_cache("disabled"))
        with self.store.connection() as db:
            db.execute("UPDATE cache SET expires=0")
        self.assertIsNone(self.store.get_cache("test"))
        self.store.cleanup(24, 32, set())
        self.assertEqual(self.store.cache_info()["entries"], 0)

    def test_clear_history_preserves_active_artifacts(self):
        for jid in ["running", "finished"]:
            folder = self.store.artifacts / jid
            folder.mkdir()
            (folder / "page.png").write_bytes(b"test")
            self.store.save_job({"id": jid, "status": "done"})
        self.assertEqual(self.store.clear_history({"running"}), 1)
        self.assertTrue((self.store.artifacts / "running/page.png").is_file())
        self.assertIsNone(self.store.get_job("finished"))

    def test_counters_survive_new_connection(self):
        self.store.increment("total")
        self.store.increment("success")
        second = Store(Path(self.tmp.name))
        self.assertEqual(second.stats()["total"], 1)
        self.assertEqual(second.stats()["success"], 1)

    def test_cleanup_reclaims_sqlite_pages_before_removing_recent_history(self):
        self.store.put_cache("large", {"payload": "x" * (34 * 1024 * 1024)}, 1)
        self.store.save_job({"id": "recent", "status": "done"})
        result = self.store.cleanup(24, 32, set())
        self.assertLess(result["bytes"], 32 * 1024 * 1024)
        self.assertIsNotNone(self.store.get_job("recent"))

    def test_browser_fonts_and_render_scratch_cannot_evict_history(self):
        for name in ('browser/kernel', 'fonts/custom.sfnt', 'render_tmp/capture.png'):
            file = self.store.root / name
            file.parent.mkdir(parents=True, exist_ok=True)
            with file.open('wb') as stream:
                stream.truncate(40 * 1024 * 1024)
        folder = self.store.artifacts / 'recent'
        folder.mkdir()
        (folder / 'page.png').write_bytes(b'rendered-result')
        self.store.save_job({'id': 'recent', 'status': 'done'})
        self.assertLess(self.store.cache_info()['bytes'], 1024 * 1024)
        result = self.store.cleanup(24, 32, set())
        self.assertEqual(result['removed'], 0)
        self.assertIsNotNone(self.store.get_job('recent'))
        self.assertTrue((folder / 'page.png').is_file())

    def test_cache_budget_still_evicts_old_artifacts(self):
        for name, size in (('old', 33 * 1024 * 1024), ('recent', 1024)):
            folder = self.store.artifacts / name
            folder.mkdir()
            with (folder / 'page.png').open('wb') as stream:
                stream.truncate(size)
            self.store.save_job({'id': name, 'status': 'done'})
        self.assertGreater(self.store.cache_info()['bytes'], 32 * 1024 * 1024)
        self.store.cleanup(24, 32, set())
        self.assertIsNone(self.store.get_job('old'))
        self.assertIsNotNone(self.store.get_job('recent'))
        self.assertLess(self.store.cache_info()['bytes'], 32 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
