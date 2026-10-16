"""Renderer invariants; no network or AstrBot service required."""
from __future__ import annotations

import asyncio
import base64
import importlib
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from jinja2 import Environment
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "_tieba_renderer_tests"
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package
renderer = importlib.import_module(f"{PACKAGE}.renderer")


def document(text="正文", segments=None):
    return {
        "kind": "thread", "id": "12345", "title": "真实标题", "forum": "测试吧",
        "url": "https://tieba.baidu.com/p/12345", "description": "", "avatar": "",
        "author": {"id": "u1", "name": "楼主", "avatar": "", "level": 16},
        "stats": {"replies": 2}, "warnings": [], "threads": [], "fetched_at": "2026-10-15T12:00:00+08:00",
        "pagination": {"fetched": 1, "total": 1, "has_more": False},
        "posts": [{"id": "p1", "floor": 1, "author": {"id": "u1", "name": "楼主", "avatar": "", "level": 16},
                   "is_op": True, "time": "2026-10-15", "likes": 3,
                   "segments": segments or [{"type": "text", "text": text}], "comments": [], "comment_total": 0}],
    }


class SafeAssets(unittest.TestCase):
    def test_canonical_denia_uses_the_renamed_bundled_art_and_font(self):
        self.assertIn("denia", renderer._THEMES)
        self.assertNotIn("daniya", renderer._THEMES)
        self.assertEqual(renderer._THEME_FONTS["denia"], "lxgw")
        self.assertTrue((ROOT / "templates" / "art" / "denia.png").is_file())
        self.assertFalse((ROOT / "templates" / "art" / "daniya.png").exists())
        self.assertIn("denia", renderer.CardRenderer._load_theme_art_assets())

    def test_only_wenkai_is_bundled_and_all_themes_use_it_by_default(self):
        self.assertEqual(set(renderer._THEME_FONTS.values()), {"lxgw"})
        self.assertEqual({theme["font"] for theme in renderer._THEMES.values()}, {"lxgw"})
        self.assertEqual(set(renderer._FONT_FILES), {"lxgw"})
        self.assertEqual(set(renderer._FONT_FAMILIES), {"lxgw", "custom"})
        assets = renderer.CardRenderer._load_font_assets()
        self.assertEqual(set(assets), {"lxgw"})
        self.assertEqual(base64.b64decode(assets["lxgw"].split(",", 1)[1]),
                         (ROOT / "templates/fonts/LXGWWenKaiScreen-Subset.woff2").read_bytes())
        for name in ("HYRunYuan-55S.ttf", "HYRunYuan-Subset.woff2", "HYTangMeiRen-55W.ttf", "HYTangMeiRen-Subset.woff2"):
            self.assertFalse((ROOT / "templates/fonts" / name).exists(), name)

    def test_custom_footer_font_reads_the_same_immutable_font_bytes(self):
        raw = b"font-snapshot-for-footer"
        expected = renderer.ImageFont.load_default()
        with patch.object(renderer.ImageFont, "truetype", return_value=expected) as load:
            self.assertIs(renderer.CardRenderer._font(17, "custom", raw), expected)
        self.assertEqual(load.call_count, 1)
        self.assertIsInstance(load.call_args.args[0], io.BytesIO)
        self.assertEqual(load.call_args.args[0].getvalue(), raw)
        self.assertEqual(load.call_args.args[1], 17)

    def test_only_exact_public_baidu_cdn_hosts(self):
        self.assertEqual(renderer.safe_asset_url("http://himg.bdimg.com/a.png"), "https://himg.bdimg.com/a.png")
        self.assertEqual(renderer.safe_asset_url("https://tb.himg.baidu.com/sys/portrait/item/tb.1.user"), "https://himg.bdimg.com/sys/portrait/item/tb.1.user")
        for bad in ["https://tiebapic.baidu.com.evil.test/x", "https://localhost/x", "http://127.0.0.1/x",
                    "file:///etc/passwd", "data:image/png;base64,abc", "https://tiebapic.baidu.com:8443/x",
                    "https://evil.test@tiebapic.baidu.com/x", "https://tiebapic.baidu.com./x"]:
            with self.subTest(bad=bad):
                self.assertIsNone(renderer.safe_asset_url(bad))

    def test_theme_art_loader_accepts_only_fixed_bundled_pngs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            art = root / "templates" / "art"
            art.mkdir(parents=True)
            with Image.new("RGB", (10, 10), "teal") as image:
                image.save(art / "miku.png", format="PNG")
                image.save(art / "denia.png", format="JPEG")
                image.save(art / "paper.png", format="PNG")
            with patch.object(renderer, "__file__", str(root / "renderer.py")):
                loaded = renderer.CardRenderer._load_theme_art_assets()
            self.assertEqual(set(loaded), {"miku"})
            prefix, encoded = loaded["miku"].split(",", 1)
            self.assertEqual(prefix, "data:image/png;base64")
            self.assertEqual(base64.b64decode(encoded), (art / "miku.png").read_bytes())

    def test_text_split_is_lossless_for_cjk_whitespace_and_combining_marks(self):
        source = ("中文 长文本\n\n  缩进\temoji🙂e\u0301 " * 900) + "最后一个字"
        pieces = renderer._text_parts(source, 24, 18)
        self.assertGreater(len(pieces), 5)
        self.assertEqual("".join(pieces), source)
        self.assertTrue(all(pieces))

    def test_mixed_segments_reply_hierarchy_and_unknown_level_are_preserved(self):
        doc = document(segments=[{"type":"text","text":"前文"}, {"type":"image","url":"https://evil.test/blocked","alt":"配图"}, {"type":"text","text":"后文"}])
        reply = {"id":"c1","floor":None,"author":{"name":"回复者","level":None},"segments":[{"type":"text","text":"楼中楼正文"}],"comments":[],"comment_total":0}
        doc["posts"][0]["comments"] = [reply]
        blocks = renderer.CardRenderer._build_blocks(doc, {}, 960, {})
        self.assertEqual([block["type"] for block in blocks], ["richtext","image","richtext","richtext"])
        self.assertEqual(blocks[-1]["depth"], 1)
        self.assertIsNone(blocks[-1]["author"]["level"])
        self.assertEqual(blocks[0]["author"]["level"], 16)
        self.assertIn("暂不可用", blocks[1]["placeholder"])
        sections = renderer.CardRenderer._as_sections(blocks)
        self.assertEqual(len(sections),1)
        self.assertEqual(len(sections[0]["comments"]),1)
        self.assertEqual(sections[0]["comments"][0]["meta"]["parent_floor"],1)

    def test_missing_reply_ids_do_not_merge_authors_or_misplace_more_notices(self):
        doc = document()
        doc["posts"][0]["comments"] = [
            {"id": "", "author": {"name": name, "level": level}, "likes": level, "comment_total": level,
             "segments": [{"type": "text", "text": name + "前"}, {"type": "image", "url": "https://himg.bdimg.com/a.png"},
                          {"type": "text", "text": name + "后"}], "comments": []}
            for name, level in (("Alice", 2), ("Bob", 3))]
        blocks = renderer.CardRenderer._build_blocks(doc, {}, 960, {})
        children = renderer.CardRenderer._as_sections(blocks)[0]["comments"]
        self.assertEqual([child["meta"]["author"]["name"] for child in children], ["Alice", "Bob"])
        self.assertEqual([len(child["blocks"]) for child in children], [3, 3])
        self.assertEqual([child["blocks"][2]["text"] for child in children], ["Alice后", "Bob后"])
        self.assertEqual([child["more"][0]["text"] for child in children], ["其余 2 条回复请查看原帖", "其余 3 条回复请查看原帖"])

    def test_three_reply_layers_keep_metadata_and_more_notices_on_their_parent(self):
        doc = document()
        parent = doc["posts"][0]
        source_authors = []
        for depth in range(1, 4):
            author = {"id": f"nested-{depth}", "name": f"回复{depth}", "level": depth + 5,
                      "avatar": f"https://himg.bdimg.com/nested-{depth}.png"}
            source_authors.append(author)
            child = {"id": f"c{depth}", "floor": None, "author": author, "is_op": depth == 2,
                     "time": "2026-10-15T04:00:00+00:00", "likes": depth,
                     "segments": [{"type": "text", "text": f"第{depth}层正文"}],
                     "comments": [], "comment_total": 2}
            parent["comments"] = [child]
            parent["comment_total"] = 3
            parent = child
        assets = {author["avatar"]: {"src": "data:image/png;base64,AA==", "width": 10, "height": 10}
                  for author in source_authors}
        blocks = renderer.CardRenderer._build_blocks(doc, {}, 960, assets)
        sections = renderer.CardRenderer._as_sections(blocks)
        self.assertEqual(len(sections), 1)
        node = sections[0]
        self.assertEqual(node["more"][0]["text"], "其余 2 条回复请查看原帖")
        for depth in range(1, 4):
            self.assertEqual(len(node["comments"]), 1)
            node = node["comments"][0]
            self.assertEqual(node["meta"]["depth"], depth)
            self.assertEqual(node["meta"]["parent_floor"], 1)
            self.assertEqual(node["meta"]["author"], source_authors[depth - 1])
            self.assertEqual(node["meta"]["avatar"], assets[source_authors[depth - 1]["avatar"]])
            self.assertEqual(node["meta"]["time"], "2026-10-15 12:00")
            self.assertEqual(node["meta"]["is_op"], depth == 2)
            self.assertEqual(node["meta"]["likes"], depth)
            self.assertEqual(node["blocks"][0]["text"], f"第{depth}层正文")
            self.assertEqual(len(node["more"]), 1)
            missing = 2
            self.assertEqual(node["more"][0]["text"], f"其余 {missing} 条回复请查看原帖")
        self.assertEqual(node["comments"], [])

    def test_adjacent_text_and_inline_emoji_form_one_continuous_post(self):
        doc = document(segments=[{"type":"text","text":"前文"},{"type":"text","text":"#话题#"},
                                 {"type":"emoji","text":"[滑稽]","url":"https://tb2.bdstatic.com/a.png"},
                                 {"type":"emoji","text":"[开心]","url":"https://tb2.bdstatic.com/b.png"},
                                 {"type":"text","text":"后文"}])
        blocks = renderer.CardRenderer._build_blocks(doc,{},960,{})
        self.assertEqual(len(blocks),1)
        self.assertEqual(blocks[0]["text"],"前文#话题#[滑稽][开心]后文")
        self.assertEqual([run["type"] for run in blocks[0]["runs"]],["text","emoji","emoji","text"])
        self.assertFalse(blocks[0]["continuation"])
        self.assertEqual(renderer._display_time("2026-09-28T11:07:05+00:00"),"2026-09-28 19:07")
        self.assertEqual(doc["posts"][0]["time"],"2026-10-15")

    def test_template_escapes_content_even_when_callback_environment_does_not(self):
        doc = document("<script>alert('x')</script>{{ 7*7 }}")
        opts = {"show_source": True}
        blocks = renderer.CardRenderer._build_blocks(doc, opts, 960, {})
        html = Environment(autoescape=False).from_string((ROOT / "templates/card.html").read_text(encoding="utf-8")).render(
            document=doc, blocks=blocks, sections=renderer.CardRenderer._as_sections(blocks), theme="paper", colors=renderer._THEMES["paper"], width=960,
            chunk_index=1, chunk_total=1, warnings=["接口未返回点赞数", "<资源异常>"], options=opts, avatar=None, kind_name="帖子存档", has_more=False)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("{{ 7*7 }}", html)
        self.assertIn('title="贴吧等级 16">Lv.16</span>', html)
        self.assertNotIn("等级 None", html)
        self.assertNotIn("TIEBA LENS",html)
        self.assertNotIn("采集于",html)
        self.assertIn('aria-label="本次读取说明"', html)
        self.assertIn("接口未返回点赞数", html)
        self.assertIn("&lt;资源异常&gt;", html)
        self.assertNotIn("<资源异常>", html)

    def test_nested_reply_template_shows_metadata_and_preserves_mixed_content(self):
        doc = document()
        avatar_url, emoji_url, image_url = [f"https://himg.bdimg.com/{name}.png" for name in ("avatar", "emoji", "body")]
        doc["posts"][0]["comments"] = [{
            "id": "child", "floor": 2, "author": {"name": "子楼作者", "level": 9, "avatar": avatar_url},
            "is_op": True, "time": "2026-10-15T04:05:00+00:00", "likes": 27, "likes_available": True,
            "segments": [{"type": "text", "text": "配图前"}, {"type": "emoji", "url": emoji_url, "text": "[滑稽]"},
                         {"type": "image", "url": image_url}, {"type": "text", "text": "配图后"}],
            "comments": [{"id": "grandchild", "author": {"name": "孙楼作者", "level": None},
                          "likes": 999, "likes_available": False, "segments": [{"type": "text", "text": "嵌套正文"}]}]}]
        assets = {url: {"src": f"data:image/png;base64,{label}", "width": 40, "height": 30}
                  for url, label in zip((avatar_url, emoji_url, image_url), ("AVATAR", "EMOJI", "BODY"))}
        blocks = renderer.CardRenderer._build_blocks(doc, {}, 960, assets)
        html = Environment(autoescape=True).from_string((ROOT / "templates/card.html").read_text(encoding="utf-8")).render(
            document=doc, blocks=blocks, sections=renderer.CardRenderer._as_sections(blocks), theme="miku", colors=renderer._THEMES["miku"],
            width=960, chunk_index=1, chunk_total=1, warnings=[], options={}, avatar=None, kind_name="帖子存档", has_more=False)
        child_html = html[html.index('<div class="comment-entry">'):]
        for expected in ('class="comment-avatar" src="data:image/png;base64,AVATAR"', "子楼作者", ">Lv.9</span>",
                         'class="op">楼主</span>', "</svg>27</span>", "2026-10-15 12:05", "孙楼作者", "嵌套正文"):
            self.assertIn(expected, child_html)
        self.assertLess(child_html.index("配图前"), child_html.index("base64,EMOJI"))
        self.assertLess(child_html.index("base64,EMOJI"), child_html.index("base64,BODY"))
        self.assertLess(child_html.index("base64,BODY"), child_html.index("配图后"))
        self.assertNotIn("</svg>999</span>", child_html)
        self.assertNotIn("Lv.None", child_html)

    def test_source_switch_controls_reading_notes_without_hiding_content(self):
        doc = document("完整正文")
        doc["warnings"] = ["部分回复接口未提供点赞数"]
        doc["pagination"] = {"fetched": 1, "total": 60, "has_more": True}
        doc["posts"][0]["comment_total"] = 58
        doc["posts"][0]["comments"] = [{"id": "child", "author": {"name": "子楼作者", "level": 9},
                                        "segments": [{"type": "text", "text": "完整楼中楼正文"}], "comments": []}]
        for enabled in (True, False):
            with self.subTest(show_source=enabled):
                options = {"show_source": enabled}
                blocks = renderer.CardRenderer._build_blocks(doc, options, 960, {})
                html = Environment(autoescape=True).from_string((ROOT / "templates/card.html").read_text(encoding="utf-8")).render(
                    document=doc, sections=renderer.CardRenderer._as_sections(blocks), theme="paper", colors=renderer._THEMES["paper"],
                    width=960, chunk_index=1, chunk_total=1, warnings=doc["warnings"], options=options, avatar=None, kind_name="帖子存档")
                for content in ("完整正文", "完整楼中楼正文", "子楼作者", ">Lv.9</span>"):
                    self.assertIn(content, html)
                for note in ('aria-label="本次读取说明"', "部分回复接口未提供点赞数", "已收录 1 楼 / 共 60 楼", "其余 57 条回复请查看原帖"):
                    self.assertEqual(note in html, enabled, note)

    def test_missing_source_floor_is_labelled_as_display_index(self):
        doc = document()
        doc["posts"].append({"id": "p2", "floor": 7, "floor_inferred": True, "author": {"name": "回复者"},
                             "segments": [{"type": "text", "text": "接口无楼层号"}], "comments": []})
        def render():
            blocks = renderer.CardRenderer._build_blocks(doc, {}, 960, {})
            return Environment(autoescape=True).from_string((ROOT / "templates/card.html").read_text(encoding="utf-8")).render(
                document=doc, sections=renderer.CardRenderer._as_sections(blocks), theme="paper", colors=renderer._THEMES["paper"],
                width=960, chunk_index=1, chunk_total=1, warnings=[], options={"sort": "hot"}, avatar=None, kind_name="帖子存档")
        html = render()
        self.assertIn("展示序号 7", html)
        self.assertNotIn("第 7 楼", html)
        self.assertIn("按点赞数", html)
        doc["posts"][1]["floor_inferred"] = False
        self.assertIn("第 7 楼", render())

    def test_footer_has_only_real_multiple_image_page_numbers(self):
        with Image.new("RGB", (960, 100), "white") as page:
            with patch.object(renderer.ImageDraw.ImageDraw, "text", autospec=True) as drawn:
                single = renderer.CardRenderer._footer(page, 960, renderer._THEMES["paper"], 1, 1,
                                                       "https://tieba.baidu.com/p/12345", False, 12)
                self.assertNotIn("1 / 1", [call.args[2] for call in drawn.call_args_list])
                self.assertEqual([call.args[2] for call in drawn.call_args_list], ["https://tieba.baidu.com/p/12345"])
                single.close()
                drawn.reset_mock()
                multiple = renderer.CardRenderer._footer(page, 960, renderer._THEMES["paper"], 2, 3,
                                                         None, False, 12)
                self.assertEqual([call.args[2] for call in drawn.call_args_list], ["2 / 3"])
                multiple.close()
            no_footer = renderer.CardRenderer._footer(page, 960, renderer._THEMES["paper"], 1, 1,
                                                       None, False, 12)
            self.assertEqual(no_footer.size, page.size)
            no_footer.close()

    def test_crop_is_contiguous_and_retains_every_pixel_row(self):
        image = Image.new("RGB", (200, 3200), "#f5f1e8")
        for y in range(3200):
            image.putpixel((0,y),(y % 256, (y // 256) % 256, 0))
        parts = renderer.CardRenderer._crop_pages(image, 1500, "#f5f1e8")
        self.assertEqual(sum(part.height for part in parts), image.height)
        joined = Image.new("RGB", image.size)
        top = 0
        for part in parts:
            joined.paste(part,(0,top)); top += part.height
        self.assertEqual(joined.tobytes(), image.tobytes())


class RenderFlow(unittest.IsolatedAsyncioTestCase):
    async def test_defaults_invalid_and_legacy_theme_options_render_canonical_denia(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contexts = []

            async def capture(template, data, options):
                contexts.append(data)
                path = root / "capture.png"
                with Image.new("RGB", (960, 100), "white") as image:
                    image.save(path)
                return str(path)

            cards = renderer.CardRenderer(capture, root)
            cards._load_assets = AsyncMock(return_value=({}, []))
            for index, options in enumerate(({}, {"theme": "invalid"}, {"theme": "daniya"}, {"theme": "danyia"}, {"theme": "denia"})):
                with self.subTest(options=options):
                    await cards.render(document(), options, f"theme_{index}")
                    self.assertEqual(contexts[-1]["theme"], "denia")
                    self.assertEqual(contexts[-1]["options"]["theme"], "denia")
                    self.assertEqual(contexts[-1]["colors"], renderer._THEMES["denia"])
                    self.assertEqual(contexts[-1]["font_key"], "lxgw")
                    self.assertEqual(contexts[-1]["theme_art_src"], cards._theme_art_assets["denia"])

    async def test_all_themes_and_legacy_font_options_use_wenkai(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contexts = []
            async def capture(template, data, options):
                contexts.append(data)
                path = root / "capture.png"
                with Image.new("RGB", (960, 100), "white") as image:
                    image.save(path)
                return str(path)
            cards = renderer.CardRenderer(capture, root)
            cards._load_assets = AsyncMock(return_value=({}, []))
            options = [{"theme": theme} for theme in renderer._THEMES]
            options += [{"font_override": value} for value in ("auto", "system", "runyuan", "tangmeiren", "lxgw", "invalid")]
            for index, option in enumerate(options):
                with self.subTest(options=option):
                    await cards.render(document(), option, f"font_{index}")
                    self.assertEqual(contexts[-1]["font_key"], "lxgw")
                    self.assertEqual(contexts[-1]["font_family"], "LXGW WenKai Screen")
                    self.assertEqual(contexts[-1]["font_face_src"], cards._font_assets["lxgw"])
                    self.assertEqual(contexts[-1]["fallback_font_face_src"], "")

    async def test_missing_custom_font_falls_back_with_persisted_diagnostic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contexts = []
            async def capture(template, data, options):
                contexts.append(data)
                path = root / "capture.png"
                with Image.new("RGB", (960, 100), "white") as image:
                    image.save(path)
                return str(path)
            cards = renderer.CardRenderer(capture, root)
            cards._load_assets = AsyncMock(return_value=({}, []))
            with patch.object(cards.fonts, "load", return_value=None):
                for visible in (True, False):
                    doc = document()
                    with self.subTest(show_source=visible), patch.object(cards, "_footer", wraps=cards._footer) as footer:
                        await cards.render(doc, {"font_override": "custom", "show_source": visible}, f"missing_{visible}")
                        self.assertEqual(contexts[-1]["font_key"], "lxgw")
                        self.assertEqual(contexts[-1]["font_face_src"], cards._font_assets["lxgw"])
                        self.assertEqual(footer.call_args.kwargs["font_key"], "lxgw")
                        self.assertIsNone(footer.call_args.kwargs["font_data"])
                        self.assertTrue(any("自定义字体" in warning and "霞鹜文楷" in warning for warning in doc["warnings"]))
                        self.assertEqual(bool(contexts[-1]["warnings"]), visible)

    async def test_custom_font_snapshot_is_embedded_and_shared_with_footer_after_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contexts = []
            raw = b"immutable-custom-font-snapshot"
            async def capture(template, data, options):
                contexts.append(data)
                # A reset during capture must not change this job's footer font.
                cards.fonts.load.return_value = None
                path = root / "capture.png"
                with Image.new("RGB", (960, 100), "white") as image:
                    image.save(path)
                return str(path)
            cards = renderer.CardRenderer(capture, root)
            cards._load_assets = AsyncMock(return_value=({}, []))
            for index, font_format in enumerate(("truetype", "opentype")):
                with self.subTest(format=font_format), \
                     patch.object(cards.fonts, "load", return_value=({"format": font_format}, raw)) as load, \
                     patch.object(cards, "_footer", side_effect=lambda page, *args, **kwargs: page.copy()) as footer:
                    await cards.render(document(), {"font_override": "custom"}, f"custom_{index}")
                    context = contexts[-1]
                    self.assertEqual(context["font_key"], "custom")
                    self.assertEqual(context["font_family"], "Tieba Custom Font")
                    self.assertEqual(context["font_format"], font_format)
                    self.assertEqual(base64.b64decode(context["font_face_src"].split(",", 1)[1]), raw)
                    self.assertEqual(context["fallback_font_face_src"], cards._font_assets["lxgw"])
                    html = Environment(autoescape=True).from_string(cards.template).render(**context)
                    self.assertIn(f'format("{font_format}")', html)
                    self.assertIn('font-family:"Tieba Custom Font"', html)
                    self.assertIn('"LXGW WenKai Screen"', html)
                    self.assertEqual(footer.call_args.kwargs["font_key"], "custom")
                    self.assertIs(footer.call_args.kwargs["font_data"], raw)
                    load.assert_called_once_with()

    @staticmethod
    def numbered_blocks(count):
        # Deliberately obsolete estimates must not determine long-image splits.
        return [{"type": "notice", "text": f"block-{index}", "number": index, "height": 100_000_000}
                for index in range(count)]

    async def test_source_hidden_at_capture_and_footer_keeps_warning_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contexts = []
            async def capture(template, data, options):
                contexts.append(data)
                path = root / "capture.png"
                with Image.new("RGB", (960, 200), "white") as image:
                    image.save(path)
                return str(path)
            cards = renderer.CardRenderer(capture, root)
            cards._load_assets = AsyncMock(return_value=({}, ["部分图片不可用"]))
            for enabled in (False, True):
                doc = document()
                doc["warnings"] = ["接口未返回点赞数"]
                with self.subTest(show_source=enabled), patch.object(cards, "_footer", wraps=cards._footer) as footer:
                    await cards.render(doc, {"show_source": enabled}, "source_on" if enabled else "source_off")
                    self.assertEqual(contexts[-1]["warnings"], ["接口未返回点赞数", "部分图片不可用"] if enabled else [])
                    self.assertEqual(footer.call_args.args[5], doc["url"] if enabled else None)
                    self.assertEqual(doc["warnings"], ["接口未返回点赞数", "部分图片不可用"])

    async def load_assets_without_network(self, cards, doc, options=None):
        requests = []
        with io.BytesIO() as encoded:
            with Image.new("RGB", (8, 8), "teal") as image:
                image.save(encoded, format="PNG")
            body = encoded.getvalue()
        async def respond(request):
            requests.append(str(request.url))
            return renderer.httpx.Response(200, content=body, headers={"content-type": "image/png"})
        client_type = renderer.httpx.AsyncClient
        transport = renderer.httpx.MockTransport(respond)
        def client_factory(**kwargs):
            return client_type(transport=transport, **kwargs)
        public_dns = [(renderer.socket.AF_INET, renderer.socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        with patch.object(renderer.httpx, "AsyncClient", side_effect=client_factory), \
             patch.object(renderer.socket, "getaddrinfo", return_value=public_dns):
            assets, warnings = await cards._load_assets(doc, options or {}, 960)
        return assets, warnings, requests

    async def test_twelve_floors_with_six_replies_load_all_avatars_after_body_resources(self):
        with tempfile.TemporaryDirectory() as temporary:
            cards = renderer.CardRenderer(AsyncMock(), Path(temporary))
            doc = document()
            doc["posts"] = []
            avatar_urls = []
            content_urls = []
            for floor in range(1, 13):
                avatar = f"https://himg.bdimg.com/root-{floor}.png"
                avatar_urls.append(avatar)
                segments = [{"type": "image", "url": f"https://tiebapic.baidu.com/body-{floor}.png"},
                            {"type": "emoji", "text": "[滑稽]", "url": f"https://tb2.bdstatic.com/emoji-{floor}.png"}]
                content_urls.extend(segment["url"] for segment in segments)
                post = {"id": f"p{floor}", "floor": floor,
                        "author": {"id": f"u{floor}", "name": f"作者{floor}", "avatar": avatar, "level": floor},
                        "segments": segments, "comments": [], "comment_total": 6}
                for reply in range(6):
                    child_avatar = f"https://himg.bdimg.com/reply-{floor}-{reply}.png"
                    avatar_urls.append(child_avatar)
                    post["comments"].append({"id": f"c{floor}-{reply}", "floor": None,
                        "author": {"id": f"r{floor}-{reply}", "name": "回复者", "avatar": child_avatar, "level": reply + 1},
                        "segments": [{"type": "text", "text": "楼中楼正文"}], "comments": [], "comment_total": 0})
                doc["posts"].append(post)
            doc["author"] = doc["posts"][0]["author"]
            assets, warnings, requests = await self.load_assets_without_network(cards, doc)
            self.assertEqual(warnings, [])
            self.assertEqual(len(assets), 108)
            self.assertTrue(all(url in assets for url in avatar_urls + content_urls))
            self.assertEqual(set(requests[:len(content_urls)]), set(content_urls))
            self.assertEqual(set(requests[len(content_urls):]), set(avatar_urls))
            blocks = cards._build_blocks(doc, {}, 960, assets)
            child_blocks = [block for block in blocks if block.get("depth") == 1]
            self.assertEqual(len(child_blocks), 72)
            self.assertTrue(all(block["avatar"] and block["author"]["level"] is not None for block in child_blocks))

    async def test_avatar_overflow_cannot_displace_body_images_or_inline_emoji(self):
        with tempfile.TemporaryDirectory() as temporary:
            cards = renderer.CardRenderer(AsyncMock(), Path(temporary))
            doc = document()
            doc["posts"][0]["comments"] = [
                {"id": f"c{index}", "floor": None,
                 "author": {"avatar": f"https://himg.bdimg.com/reply-{index}.png", "level": index % 18},
                 "segments": [{"type": "text", "text": "回复"}], "comments": [], "comment_total": 0}
                for index in range(130)]
            content = [{"type": "image", "url": "https://tiebapic.baidu.com/important.png"},
                       {"type": "emoji", "text": "[滑稽]", "url": "https://tb2.bdstatic.com/important.png"}]
            doc["posts"][0]["segments"] = content
            assets, warnings, requests = await self.load_assets_without_network(cards, doc)
            self.assertEqual(requests[:2], [segment["url"] for segment in content])
            self.assertEqual(len(requests), renderer._MAX_ASSETS)
            self.assertTrue(all(segment["url"] in assets for segment in content))
            self.assertEqual(len(assets), renderer._MAX_ASSETS)
            self.assertEqual(len(warnings), 1)
            self.assertIn("4 张图片或头像不可用", warnings[0])

    async def test_emoji_have_separate_resource_budget_from_body_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            cards = renderer.CardRenderer(AsyncMock(),Path(temporary))
            doc = document(segments=[{"type":"emoji","text":"[一]","url":"https://tb2.bdstatic.com/e1.png"},
                                     {"type":"emoji","text":"[二]","url":"https://tb2.bdstatic.com/e2.png"},
                                     {"type":"image","url":"https://tiebapic.baidu.com/body1.jpg"},
                                     {"type":"image","url":"https://tiebapic.baidu.com/body2.jpg"}])
            with patch.object(renderer,"safe_asset_url",return_value=None) as checked:
                await cards._load_assets(doc,{"image_limit":1},960)
                attempted = [call.args[0] for call in checked.call_args_list]
                self.assertEqual(len(attempted),3)
                self.assertIn("https://tiebapic.baidu.com/body1.jpg",attempted)
                self.assertNotIn("https://tiebapic.baidu.com/body2.jpg",attempted)
            asset = {"src":"data:image/png;base64,AA==","width":30,"height":30}
            assets = {segment["url"]:asset for segment in doc["posts"][0]["segments"]}
            blocks = cards._build_blocks(doc,{"image_limit":1},960,assets)
            self.assertTrue(all(run["asset"] for run in blocks[0]["runs"]))
            self.assertIsNotNone(blocks[1]["asset"])
            self.assertIsNone(blocks[2]["asset"])

    async def test_render_uses_real_screenshot_options_and_local_artifact_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            captured_options = []
            async def capture(template, data, options):
                captured_options.append(options)
                path = root / "capture.png"
                Image.new("RGB",(options["viewport_width"],600),"white").save(path)
                return str(path)
            cards = renderer.CardRenderer(capture,root)
            cards._load_assets = AsyncMock(return_value=({},[]))
            paths = await cards.render(document(),{},"job_123")
            self.assertEqual(len(paths),1)
            self.assertTrue(Path(paths[0]).is_file())
            self.assertEqual(Path(paths[0]).parent,root / "artifacts" / "job_123")
            self.assertEqual(captured_options[0]["viewport_width"],960)
            self.assertEqual(captured_options[0]["device_scale_factor_level"],"normal")
            self.assertNotIn("width",captured_options[0])

    async def test_long_image_attempts_all_blocks_once_before_any_split(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            requests = []
            blocks = self.numbered_blocks(20)
            async def capture(template, data, options):
                requests.append(data)
                path = root / "capture.png"
                with Image.new("RGB", (960, 600), "white") as image:
                    image.save(path)
                return str(path)
            cards = renderer.CardRenderer(capture, root)
            cards._load_assets = AsyncMock(return_value=({}, []))
            cards._build_blocks = Mock(return_value=blocks)
            with patch.object(cards, "_crop_pages", side_effect=AssertionError("long images cannot crop pages")), \
                 patch.object(cards, "stitch_pages", side_effect=AssertionError("long images cannot stitch pages")):
                paths = await cards.render(document(), {"single_image": True, "page_height": 1600}, "whole")
            self.assertEqual(len(paths), 1)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["blocks"], blocks)
            self.assertEqual(requests[0]["chunk_index"], 1)

    async def test_actual_pixel_limit_splits_only_confirmed_contiguous_prefixes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            requests, footers = [], []
            blocks = self.numbered_blocks(7)
            async def capture(template, data, options):
                numbers = [block["number"] for block in data["blocks"]]
                requests.append((data["chunk_index"], numbers))
                path = root / "capture.png"
                with Image.new("RGB", (960, len(numbers) * 10), "white") as image:
                    for offset, number in enumerate(numbers):
                        image.paste((number, 0, 0), (0, offset * 10, 960, (offset + 1) * 10))
                    image.save(path)
                return str(path)
            def footer(page, width, colors, number, total, source, limited, max_pages, **kwargs):
                footers.append((number, total, source, limited))
                return page.copy()
            cards = renderer.CardRenderer(capture, root)
            cards._load_assets = AsyncMock(return_value=({}, []))
            cards._build_blocks = Mock(return_value=blocks)
            with patch.object(renderer, "_MAX_PIXELS", 960 * 35), patch.object(cards, "_footer", side_effect=footer):
                paths = await cards.render(document(), {"single_image": True}, "actual_limit")
            self.assertEqual(requests[0], (1, list(range(7))))
            self.assertEqual(requests, [(1, list(range(7))), (1, [0, 1, 2]), (1, [0, 1, 2, 3, 4]),
                                        (1, [0, 1, 2, 3]), (2, [3, 4, 5, 6]), (2, [3, 4]),
                                        (2, [3, 4, 5]), (3, [6])])
            actual = []
            for path in paths:
                with Image.open(path) as image:
                    actual.extend(image.getpixel((0, y))[0] for y in range(0, image.height, 10))
            self.assertEqual(actual, list(range(7)))
            self.assertEqual([(number, total) for number, total, _, _ in footers], [(1, 3), (2, 3), (3, 3)])
            self.assertEqual([source for _, _, source, _ in footers], [None, None, document()["url"]])
            self.assertFalse(any(limited for _, _, _, limited in footers))

    async def test_explicit_service_size_limit_allows_fallback_but_other_errors_propagate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            attempts = []
            async def capture(template, data, options):
                attempts.append(len(data["blocks"]))
                if len(data["blocks"]) > 2:
                    raise RuntimeError("Cannot take screenshot larger than 32767 pixels")
                path = root / "capture.png"
                with Image.new("RGB", (960, 100), "white") as image:
                    image.save(path)
                return str(path)
            cards = renderer.CardRenderer(capture, root)
            cards._load_assets = AsyncMock(return_value=({}, []))
            cards._build_blocks = Mock(return_value=self.numbered_blocks(4))
            paths = await cards.render(document(), {}, "service_limit")
            self.assertEqual(len(paths), 2)
            self.assertEqual(attempts, [4, 2, 3, 2])
            for error in (RuntimeError("401 Unauthorized"), TimeoutError("Page.screenshot timed out"),
                          RuntimeError("Unable to capture screenshot")):
                with self.subTest(error=str(error)):
                    cards.render_html = AsyncMock(side_effect=error)
                    with self.assertRaises(type(error)) as caught:
                        await cards.render(document(), {}, "service_error")
                    self.assertIs(caught.exception, error)
                    self.assertEqual(cards.render_html.await_count, 1)

    async def test_single_oversized_block_fails_instead_of_dropping_body(self):
        with tempfile.TemporaryDirectory() as temporary:
            cards = renderer.CardRenderer(AsyncMock(side_effect=RuntimeError("Screenshot is too large")), Path(temporary))
            cards._load_assets = AsyncMock(return_value=({}, []))
            cards._build_blocks = Mock(return_value=self.numbered_blocks(1))
            with self.assertRaisesRegex(RuntimeError, "单个内容块"):
                await cards.render(document(), {}, "oversized")
            self.assertEqual(cards.render_html.await_count, 1)

    async def test_theme_art_context_is_empty_for_all_other_themes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            requests = []
            async def capture(template, data, options):
                requests.append(data["theme_art_src"])
                path = root / "capture.png"
                with Image.new("RGB", (960, 100), "white") as image:
                    image.save(path)
                return str(path)
            cards = renderer.CardRenderer(capture, root)
            cards._load_assets = AsyncMock(return_value=({}, []))
            cards._theme_art_assets = {"miku": "data:image/png;base64,bWlrdQ==", "denia": "data:image/png;base64,ZGFuaXlh"}
            for theme in ("miku", "denia", "paper", "midnight", "mist", "bilibili"):
                await cards.render(document(), {"theme": theme}, f"art_{theme}")
            self.assertEqual(requests, [cards._theme_art_assets["miku"], cards._theme_art_assets["denia"], "", "", "", ""])

    async def test_page_budget_reports_omission_in_document(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            async def capture(template,data,options):
                if len(data["blocks"]) > 2:
                    raise RuntimeError("Cannot take screenshot larger than 32767 pixels")
                path = root / "capture.png"
                with Image.new("RGB",(960,500),"#f5f1e8") as image:
                    image.save(path)
                return str(path)
            cards = renderer.CardRenderer(capture,root)
            cards._load_assets = AsyncMock(return_value=({},[]))
            for enabled in (False, True):
                doc = document("不能悄悄丢失的长文字。" * 4000)
                with self.subTest(show_source=enabled), patch.object(cards, "_footer", wraps=cards._footer) as footer:
                    paths = await cards.render(doc,{"max_pages":1, "show_source":enabled},"bounded_on" if enabled else "bounded_off")
                    self.assertEqual(len(paths),1)
                    self.assertEqual(footer.call_args.args[6], enabled)
                    self.assertTrue(any("1 页上限" in warning and "未收录" in warning for warning in doc["warnings"]))

    async def test_invalid_job_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            cards = renderer.CardRenderer(AsyncMock(),Path(temporary))
            with self.assertRaises(ValueError):
                await cards.render(document(),{},"../escape")


if __name__ == "__main__":
    unittest.main()
