"""Offline font cache integrity, atomic writes, and concurrent first use."""
from __future__ import annotations

import base64
import hashlib
import importlib
import io
import json
import subprocess
import sys
import tempfile
import types
import unittest
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from PIL import ImageFont

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "_tieba_bundled_font_tests"
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package
bundled = importlib.import_module(f"{PACKAGE}.bundled_fonts")
fonts = importlib.import_module(f"{PACKAGE}.fonts")


def make_font(advance=600):
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder([".notdef", "A"])
    pen = TTGlyphPen(None)
    pen.moveTo((50, 0))
    pen.lineTo((250, 700))
    pen.lineTo((450, 0))
    pen.closePath()
    builder.setupGlyf({".notdef": TTGlyphPen(None).glyph(), "A": pen.glyph()})
    builder.setupCharacterMap({65: "A"})
    builder.setupHorizontalMetrics({".notdef": (advance, 0), "A": (advance, 50)})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": "Cache Test", "styleName": "Regular"})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    builder.setupPost()
    output = io.BytesIO()
    builder.font.save(output)
    sfnt = output.getvalue()
    builder.font.flavor = "woff2"
    output = io.BytesIO()
    builder.font.save(output)
    return sfnt, output.getvalue()


class BundledAssetTests(unittest.TestCase):
    def test_complete_bundle_preserves_coverage_and_skips_expensive_glyph_transforms(self):
        path = ROOT / "templates" / "fonts" / bundled.BUNDLED_FONT_FILENAME
        raw = bundled.read_bundled_woff2(path)
        with TTFont(io.BytesIO(raw)) as font:
            self.assertEqual(font.flavorData.transformedTables, set())
            self.assertEqual(font["maxp"].numGlyphs, 47871)
            self.assertEqual(len(font.getBestCmap()), 47449)

    def test_dynamic_subset_preserves_coverage_metrics_and_pixels_including_rare_characters(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = bundled.BundledFontCache(Path(temporary))
            source = cache.sfnt_path()
            with TTFont(source) as full:
                original = full.getBestCmap()
                text = "贴吧拾光繁體龘Aa e\u0301" + "".join(chr(point) for point in sorted(original)[-12:])
                requested = frozenset(map(ord, text))
                raw = cache.subset_woff2(requested)
                self.assertLess(len(raw), 300000)
                with TTFont(io.BytesIO(raw)) as partial:
                    actual = partial.getBestCmap()
                    self.assertTrue(requested & original.keys() <= actual.keys())
                    self.assertTrue({0xE9, 0x25CC} <= actual.keys())
                    for point in requested & original.keys():
                        self.assertEqual(full["hmtx"].metrics[original[point]], partial["hmtx"].metrics[actual[point]])
                        self.assertEqual(full["glyf"][original[point]].getCoordinates(full["glyf"]),
                                         partial["glyf"][actual[point]].getCoordinates(partial["glyf"]))
                    self.assertEqual(full["head"].unitsPerEm, partial["head"].unitsPerEm)
                    self.assertEqual(full["hhea"].ascent, partial["hhea"].ascent)
                    self.assertEqual(full["hhea"].descent, partial["hhea"].descent)
                    self.assertEqual(full["name"].getDebugName(0), partial["name"].getDebugName(0))
                    self.assertEqual(full["name"].getDebugName(13), partial["name"].getDebugName(13))
                    partial.flavor = None
                    output = io.BytesIO()
                    partial.save(output)
            for size in (13, 15, 17, 24, 32, 48):
                before = ImageFont.truetype(str(source), size)
                after = ImageFont.truetype(io.BytesIO(output.getvalue()), size)
                with self.subTest(size=size):
                    self.assertEqual(before.getbbox(text), after.getbbox(text))
                    self.assertEqual(before.getlength(text), after.getlength(text))
                    self.assertEqual(bytes(before.getmask(text)), bytes(after.getmask(text)))


class CharacterCollectionTests(unittest.TestCase):
    def test_template_document_blocks_warning_and_rare_tail_are_kept_without_media(self):
        tail = "".join(chr(point) for point in range(0x4E00, 0x4E00 + 10000)) + "𠮷"
        context = {"document": {"title": "标题", "author": {"name": "用户"}, "description": tail},
                   "blocks": [{"placeholder": "图片占位", "runs": [{"text": "正文"}],
                               "asset": {"src": "data:image/png;base64,🚀"}}],
                   "warnings": ["读取警告"], "kind_name": "帖子存档",
                   "theme_art_src": "data:image/png;base64,🛸",
                   "font_face_src": "data:font/otf;base64,🦄",
                   "fallback_font_face_src": "data:font/woff2;base64,🧪"}
        context["loop"] = context
        actual = bundled.collect_font_characters("<body>固定标签插画七宝カラ</body>", context)
        expected = set(map(ord, "固定标签插画七宝カラ标题用户图片占位正文读取警告帖子存档" + tail))
        self.assertTrue(expected <= actual)
        self.assertTrue(set(range(0x20, 0x7F)) <= actual)
        self.assertFalse(set(map(ord, "🚀🛸🦄🧪")) & actual)


class BundledFontCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sfnt, cls.woff2 = make_font()
        cls.digest = hashlib.sha256(cls.woff2).hexdigest()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "readonly-plugin" / "font.woff2"
        self.source.parent.mkdir()
        self.source.write_bytes(self.woff2)
        self.data_root = self.root / "plugin_data"
        for name, value in (("_SUBSET_CACHE", OrderedDict()), ("_SUBSET_CACHE_BYTES", 0)):
            replacement = patch.object(bundled, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)

    def cache(self, data_root=None):
        return bundled.BundledFontCache(data_root or self.data_root, self.source, self.digest)

    def test_first_use_writes_only_data_directory_and_restart_reuses_verified_sfnt(self):
        cache = self.cache()
        path = cache.sfnt_path()
        self.assertEqual(path.parent, self.data_root / "fonts" / "bundled")
        self.assertEqual(list(self.source.parent.iterdir()), [self.source])
        self.assertTrue(ImageFont.truetype(str(path), 16).getmask("A").getbbox())
        with TTFont(path) as font:
            self.assertIsNone(font.flavor)
            self.assertEqual(font.getBestCmap(), {65: "A"})
        with patch.object(bundled.BundledFontCache, "_decode", side_effect=AssertionError("unnecessary decode")):
            self.assertEqual(cache.sfnt_path(), path)
            self.assertEqual(self.cache().sfnt_path(), path)
        metadata = json.loads(cache.manifest.read_text())
        self.assertEqual(metadata["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_threads_share_one_decode_across_cache_instances(self):
        caches = [self.cache() for _ in range(8)]
        with patch.object(bundled.BundledFontCache, "_decode", wraps=bundled.BundledFontCache._decode) as decode, \
             ThreadPoolExecutor(max_workers=8) as pool:
            paths = list(pool.map(lambda cache: cache.sfnt_path(), caches))
        self.assertEqual(len(set(paths)), 1)
        self.assertEqual(decode.call_count, 1)

    def test_processes_share_one_decode_and_never_read_a_partial_file(self):
        script = """
import os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from bundled_fonts import BundledFontCache
original = BundledFontCache._decode
def tracked(raw):
    with (Path(sys.argv[2]) / 'decode-count').open('ab') as stream:
        stream.write(b'x')
    time.sleep(0.15)
    return original(raw)
BundledFontCache._decode = staticmethod(tracked)
cache = BundledFontCache(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
print(cache.sfnt_path())
"""
        self.data_root.mkdir()
        command = [sys.executable, "-c", script, str(ROOT), str(self.data_root), str(self.source), self.digest]
        processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
        try:
            for process in processes:
                output, error = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, error)
                self.assertTrue(Path(output.strip()).is_file())
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        self.assertEqual((self.data_root / "decode-count").read_bytes(), b"x")
        self.assertEqual(list(self.cache().root.glob("*.tmp-*")), [])

    def test_truncated_same_size_tampered_or_invalid_metadata_cache_is_repaired(self):
        for fault in ("truncated", "same-size", "metadata", "missing-metadata", "wrong-source"):
            with self.subTest(fault=fault):
                cache = self.cache(self.data_root / fault)
                path = cache.sfnt_path()
                expected = path.read_bytes()
                if fault == "truncated":
                    path.write_bytes(expected[:20])
                elif fault == "same-size":
                    path.write_bytes(expected[:-1] + bytes([expected[-1] ^ 1]))
                elif fault == "metadata":
                    cache.manifest.write_text("[]")
                elif fault == "missing-metadata":
                    cache.manifest.unlink()
                else:
                    metadata = json.loads(cache.manifest.read_text())
                    metadata["source_sha256"] = "0" * 64
                    cache.manifest.write_text(json.dumps(metadata))
                with patch.object(cache, "_decode", wraps=cache._decode) as decode:
                    self.assertEqual(cache.sfnt_path().read_bytes(), expected)
                self.assertEqual(decode.call_count, 1)

    def test_corrupt_bundle_is_rejected_before_decompression(self):
        self.source.write_bytes(self.woff2[:-1] + bytes([self.woff2[-1] ^ 1]))
        cache = self.cache()
        with patch.object(cache, "_decode", side_effect=AssertionError("corruption must be caught first")), \
             self.assertRaisesRegex(ValueError, "字体损坏"):
            cache.sfnt_path()
        self.assertFalse(cache.path.exists())

    def test_crash_leftover_temporary_files_are_cleaned_under_the_cache_lock(self):
        cache = self.cache()
        cache.root.mkdir(parents=True)
        abandoned = [target.with_name(target.name + ".tmp-abandoned")
                     for target in (cache.path, cache.manifest)]
        for temporary in abandoned:
            temporary.write_bytes(b"partial write")
        self.assertTrue(cache.sfnt_path().is_file())
        self.assertTrue(all(not temporary.exists() for temporary in abandoned))

    def test_interrupted_manifest_commit_cleans_temporary_files_and_repairs_next_use(self):
        cache = self.cache()
        replace = bundled.os.replace

        def fail_manifest(source, target):
            if Path(target) == cache.manifest:
                raise OSError("simulated interrupted manifest commit")
            return replace(source, target)

        with patch.object(bundled.os, "replace", side_effect=fail_manifest), self.assertRaisesRegex(OSError, "interrupted"):
            cache.sfnt_path()
        self.assertEqual(list(cache.root.glob("*.tmp-*")), [])
        self.assertFalse(cache.manifest.exists())
        self.assertTrue(self.cache().sfnt_path().is_file())

    def test_updated_bundle_has_an_independent_cache_identity(self):
        original = self.cache().sfnt_path()
        _, updated = make_font(720)
        self.source.write_bytes(updated)
        cache = bundled.BundledFontCache(self.data_root, self.source, hashlib.sha256(updated).hexdigest())
        path = cache.sfnt_path()
        self.assertNotEqual(path, original)
        with TTFont(path) as font:
            self.assertEqual(font["hmtx"].metrics["A"][0], 720)

    def test_custom_font_upload_and_reset_preserve_bundled_cache(self):
        cache = self.cache()
        path = cache.sfnt_path()
        original = path.read_bytes()
        store = fonts.FontStore(self.data_root)
        store.upload({"name": "custom.ttf", "data": base64.b64encode(self.sfnt).decode("ascii")})
        self.assertIsNotNone(store.load())
        store.reset()
        self.assertIsNone(store.load())
        self.assertEqual(cache.sfnt_path().read_bytes(), original)

    def test_subset_cache_reuses_full_character_set_and_concurrent_misses(self):
        cache = self.cache()
        with patch.object(cache, "_make_subset", wraps=cache._make_subset) as subset:
            first = cache.subset_woff2([65, 66, 65])
            self.assertIs(cache.subset_woff2([66, 65]), first)
            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(lambda _: self.cache().subset_woff2([65, 66]), range(6)))
        self.assertEqual(subset.call_count, 1)
        self.assertTrue(all(result is first for result in results))
        self.assertEqual(bundled._SUBSET_CACHE_BYTES, sum(map(len, bundled._SUBSET_CACHE.values())))

    def test_subset_cache_evicts_by_bytes_and_count_without_changing_results(self):
        cache = self.cache()
        with patch.object(bundled, "_SUBSET_CACHE_MAX_BYTES", 20), \
             patch.object(bundled, "_SUBSET_CACHE_MAX_ENTRIES", 2), \
             patch.object(cache, "_make_subset", return_value=b"x" * 10) as subset:
            for character in (0x20000, 0x20001, 0x20002):
                self.assertEqual(cache.subset_woff2([character]), b"x" * 10)
            self.assertEqual(len(bundled._SUBSET_CACHE), 2)
            self.assertEqual(bundled._SUBSET_CACHE_BYTES, 20)
            cache.subset_woff2([0x20002])
            self.assertEqual(subset.call_count, 3)
            cache.subset_woff2([0x20000])
            self.assertEqual(subset.call_count, 4)
            self.assertEqual(len(bundled._SUBSET_CACHE), 2)
            self.assertEqual(bundled._SUBSET_CACHE_BYTES, 20)

    def test_over_budget_subset_is_used_without_caching_or_truncating_the_character_set(self):
        cache = self.cache()
        requested = frozenset(range(10000))
        with patch.object(bundled, "_SUBSET_CACHE_MAX_BYTES", 10), \
             patch.object(cache, "_make_subset", return_value=b"x" * 11) as subset:
            self.assertEqual(cache.subset_woff2(requested), b"x" * 11)
            self.assertEqual(cache.subset_woff2(requested), b"x" * 11)
        self.assertEqual(subset.call_count, 2)
        self.assertTrue(requested <= subset.call_args.args[1])
        self.assertTrue(bundled._HINT_CHARACTERS <= subset.call_args.args[1])
        self.assertEqual(len(bundled._SUBSET_CACHE), 0)
        self.assertEqual(bundled._SUBSET_CACHE_BYTES, 0)
        self.assertEqual(list(cache.root.glob("*.woff2")), [])

    def test_subset_cache_identity_includes_source_font_hash(self):
        first = self.cache().subset_woff2([65])
        _, raw = make_font(720)
        self.source.write_bytes(raw)
        cache = bundled.BundledFontCache(self.data_root, self.source, hashlib.sha256(raw).hexdigest())
        second = cache.subset_woff2([65])
        self.assertNotEqual(first, second)
        with TTFont(io.BytesIO(second)) as font:
            self.assertEqual(font["hmtx"].metrics["A"][0], 720)

    def test_subset_keeps_cmap_aliases_for_requested_glyphs(self):
        sfnt, _ = make_font()
        source = self.data_root / "alias.sfnt"
        source.parent.mkdir(parents=True, exist_ok=True)
        with TTFont(io.BytesIO(sfnt)) as font:
            for table in font["cmap"].tables:
                if table.isUnicode():
                    table.cmap[0xE001] = "A"
            font.save(source)
        raw = bundled.BundledFontCache._make_subset(source, frozenset({65}))
        with TTFont(io.BytesIO(raw)) as font:
            cmap = font.getBestCmap()
            self.assertEqual(cmap[65], cmap[0xE001])


if __name__ == "__main__":
    unittest.main()
