"""Font persistence and upload contracts; generated fonts and temporary files only."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import ImageFont

try:
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.ttLib import TTFont
except ImportError:
    FontBuilder = TTGlyphPen = TTFont = None


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("tieba_font_store_tests", ROOT / "fonts.py")
fonts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fonts)


def encode(raw):
    return base64.b64encode(raw).decode("ascii")


def chunk(raw=b"HEAD", *, name="font.ttf", total=8, offset=0, upload_id=None):
    payload = {"name": name, "data": encode(raw), "total_size": total, "offset": offset}
    if upload_id is not None:
        payload["upload_id"] = upload_id
    return payload


def make_font(variant):
    """A complete tiny TrueType font, including the characters used by validation."""
    names = [".notdef", "space", "A", "uni5B57", "uni4F53"]
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(names)
    builder.setupCharacterMap({32: "space", 65: "A", 0x5B57: "uni5B57", 0x4F53: "uni4F53"})
    glyphs = {}
    for name in names:
        pen = TTGlyphPen(None)
        if name != "space":
            pen.moveTo((50, 0))
            pen.lineTo((450 + variant, 0))
            pen.lineTo((450 + variant, 700))
            pen.lineTo((50, 700))
            pen.closePath()
        glyphs[name] = pen.glyph()
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({name: (600, 0) for name in names})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    family = f"Tieba Test {variant}"
    builder.setupNameTable({"familyName": family, "styleName": "Regular",
                            "uniqueFontIdentifier": family, "fullName": family + " Regular",
                            "psName": f"TiebaTest{variant}-Regular", "version": "Version 1.0"})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    builder.setupPost()
    builder.setupMaxp()
    builder.font.recalcTimestamp = False
    builder.font["head"].created = builder.font["head"].modified = 3_800_000_000
    output = io.BytesIO()
    builder.save(output)
    return output.getvalue()


class FontStoreCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = fonts.FontStore(self.root)


class FontValidationTests(FontStoreCase):
    def test_empty_store_has_safe_metadata_and_reset_is_idempotent(self):
        self.assertIsNone(self.store.load())
        info = self.store.info()
        self.assertFalse(info["uploaded"])
        self.assertEqual(info["max_bytes"], 33554432)
        self.assertEqual(info["formats"], ["ttf", "otf", "woff", "woff2"])
        self.assertEqual(self.store.reset(), info)
        self.assertEqual(self.store.reset(), info)

    def test_whole_upload_rejects_invalid_payload_extension_raw_and_base64(self):
        for payload in (None, [], {}, {"name": 1, "data": "eA=="},
                        {"name": "bad.exe", "data": "eA=="},
                        {"name": "font.ttf", "data": "not base64!"},
                        {"name": "font.ttf", "data": "eA==\n"},
                        {"name": "font.ttf", "data": ""},
                        {"name": "font.ttf", "data": encode(b"not a font")}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.store.upload(payload)
        self.assertIsNone(self.store.load())

    def test_whole_upload_limits_both_encoded_and_decoded_bytes(self):
        # A smaller test budget checks both gates without allocating a 32 MB file.
        with patch.object(fonts, "MAX_FONT_BYTES", 8):
            for raw in (b"x" * 9, b"x" * 13):
                with self.subTest(length=len(raw)), self.assertRaises(ValueError):
                    self.store.upload({"name": "font.ttf", "data": encode(raw)})

    def test_chunk_rejects_invalid_sizes_offsets_and_more_than_32_mb(self):
        for field, value in (("total_size", 0), ("total_size", -1), ("total_size", True),
                             ("total_size", 1.5), ("total_size", 33554433),
                             ("offset", -1), ("offset", True), ("offset", 1.5), ("offset", 8)):
            payload = {**chunk(), field: value}
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.store.upload_chunk(payload)
        with self.assertRaises(ValueError):
            self.store.upload_chunk(chunk(b"too many bytes", total=8))

    def test_chunk_rejects_invalid_data_extension_and_oversized_body(self):
        for value in ("", "bad base64!", "eA==\n", True, [], "A" * (1024 * 1024 + 1)):
            with self.subTest(data_type=type(value).__name__, length=len(value) if isinstance(value, str) else None), self.assertRaises(ValueError):
                self.store.upload_chunk({**chunk(), "data": value})
        for name in ("font.exe", "", 42):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.upload_chunk({**chunk(), "name": name})

    def test_session_ids_cannot_escape_the_upload_directory(self):
        sentinel = self.root / "outside.part"
        sentinel.write_bytes(b"untouched")
        for upload_id in ("../outside", "..\\outside", "/outside", "a" * 31, "a" * 33, "G" * 32, 123):
            with self.subTest(upload_id=upload_id), self.assertRaises(ValueError):
                self.store.upload_chunk(chunk(upload_id=upload_id))
        self.assertEqual(sentinel.read_bytes(), b"untouched")
        self.assertFalse((self.root / "outside.json").exists())

    def test_chunks_reject_wrong_order_duplicate_offset_total_and_name(self):
        first = self.store.upload_chunk(chunk())
        upload_id = first["upload_id"]
        self.assertRegex(upload_id, r"^[0-9a-f]{32}$")
        self.assertEqual(first["received"], 4)
        for payload in (chunk(b"END", offset=3, upload_id=upload_id),
                        chunk(offset=0, upload_id=upload_id),
                        chunk(b"TAIL", total=9, offset=4, upload_id=upload_id),
                        chunk(b"TAIL", name="other.ttf", offset=4, upload_id=upload_id),
                        chunk(b"TAIL", offset=4)):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.store.upload_chunk(payload)
        manifest = self.store.root / "uploads" / (upload_id + ".json")
        self.assertEqual(json.loads(manifest.read_text())["received"], 4)
        self.assertEqual(manifest.with_suffix(".part").read_bytes(), b"HEAD")

    def test_chunks_reject_a_partial_file_that_disagrees_with_manifest(self):
        first = self.store.upload_chunk(chunk())
        part = self.store.root / "uploads" / (first["upload_id"] + ".part")
        part.write_bytes(b"HEADX")
        with self.assertRaises(ValueError):
            self.store.upload_chunk(chunk(b"TAIL", offset=4, upload_id=first["upload_id"]))

    def test_expired_sessions_are_removed_and_cannot_be_resumed(self):
        first = self.store.upload_chunk(chunk())
        uploads = self.store.root / "uploads"
        stale = [uploads / (first["upload_id"] + suffix) for suffix in (".json", ".part")]
        past = time.time() - 901
        for path in stale:
            os.utime(path, (past, past))
        self.store.upload_chunk(chunk(name="fresh.ttf"))
        self.assertTrue(all(not path.exists() for path in stale))
        with self.assertRaises(ValueError):
            self.store.upload_chunk(chunk(b"TAIL", offset=4, upload_id=first["upload_id"]))

    def test_pending_session_count_is_bounded_and_expiry_releases_capacity(self):
        sessions = [self.store.upload_chunk(chunk(name=f"font-{index}.ttf")) for index in range(4)]
        with self.assertRaises(ValueError):
            self.store.upload_chunk(chunk(name="fifth.ttf"))
        past = time.time() - 901
        for suffix in (".json", ".part"):
            os.utime(self.store.root / "uploads" / (sessions[0]["upload_id"] + suffix), (past, past))
        self.assertFalse(self.store.upload_chunk(chunk(name="fifth.ttf"))["complete"])
        self.assertEqual(len(list((self.store.root / "uploads").glob("*.json"))), 4)

    def test_reset_invalidates_old_upload_ids(self):
        first = self.store.upload_chunk(chunk())
        self.assertFalse(self.store.reset()["uploaded"])
        self.assertEqual(list((self.store.root / "uploads").iterdir()), [])
        with self.assertRaises(ValueError):
            self.store.upload_chunk(chunk(b"TAIL", offset=4, upload_id=first["upload_id"]))

    def test_damaged_manifest_never_crashes_metadata_or_load(self):
        self.store.root.mkdir(parents=True)
        cases = ("", "{bad", "[]", "null", "true", "{}",
                 json.dumps({"format": "invalid", "sha256": "a" * 64}),
                 json.dumps({"format": [], "sha256": "a" * 64}),
                 json.dumps({"format": "truetype", "sha256": "../outside"}),
                 json.dumps({"format": "truetype", "sha256": None}),
                 json.dumps({"format": "truetype", "sha256": "a" * 64}))
        for value in cases:
            with self.subTest(manifest=value):
                self.store.manifest.write_text(value, encoding="utf-8")
                self.assertIsNone(self.store.load())
                self.assertFalse(self.store.info()["uploaded"])

    def test_woff_declared_decompression_limit_is_rejected_before_font_parsing(self):
        for magic in (b"wOFF", b"wOF2"):
            raw = magic + b"\0" * 12 + struct.pack(">I", 33554433) + b"\0" * 28
            with self.subTest(magic=magic), self.assertRaisesRegex(ValueError, "声明.*32 MB"):
                self.store.upload({"name": "font.woff2", "data": encode(raw)})
        self.assertFalse(self.store.info()["uploaded"])


@unittest.skipUnless(FontBuilder is not None, "fonttools is required for generated font round trips")
class FontRoundTripTests(FontStoreCase):
    @classmethod
    def setUpClass(cls):
        cls.first_font = make_font(10)
        cls.second_font = make_font(30)
        with TTFont(io.BytesIO(cls.second_font)) as font:
            font.flavor = "woff"
            output = io.BytesIO()
            font.save(output)
        cls.woff_font = output.getvalue()

    def install(self, raw=None, name="original.ttf"):
        self.store.upload({"name": name, "data": encode(raw or self.first_font)})
        return self.store.load()

    def assert_readable(self, snapshot):
        meta, raw = snapshot
        self.assertEqual(hashlib.sha256(raw).hexdigest(), meta["sha256"])
        self.assertTrue(ImageFont.truetype(io.BytesIO(raw), 16).getmask("A字体").getbbox())
        with TTFont(io.BytesIO(raw)) as font:
            self.assertIsNone(font.flavor)
            self.assertEqual(font.getBestCmap()[65], "A")

    def test_whole_woff_upload_normalizes_to_a_hash_verified_readable_sfnt(self):
        result = self.store.upload({"name": "../子目录/测试字体.woff", "data": encode(self.woff_font)})
        self.assertTrue(result["uploaded"])
        self.assertEqual(result["name"], "测试字体.woff")
        self.assertEqual(result["size"], len(self.woff_font))
        snapshot = self.store.load()
        self.assertEqual(snapshot[1][:4], b"\0\1\0\0")
        self.assertNotEqual(snapshot[1], self.woff_font)
        self.assert_readable(snapshot)

    def test_two_chunks_complete_and_partial_upload_preserves_the_old_font(self):
        original = self.install()
        cut = len(self.second_font) // 2
        first = self.store.upload_chunk(chunk(self.second_font[:cut], name="new.ttf", total=len(self.second_font)))
        self.assertFalse(first["complete"])
        self.assertEqual(self.store.load(), original)
        complete = self.store.upload_chunk(chunk(self.second_font[cut:], name="new.ttf", total=len(self.second_font), offset=cut, upload_id=first["upload_id"]))
        self.assertTrue(complete["complete"])
        self.assertEqual(complete["received"], len(self.second_font))
        self.assertEqual(complete["font"]["name"], "new.ttf")
        self.assertNotEqual(complete["font"]["sha256"], original[0]["sha256"])
        self.assertEqual(list((self.store.root / "uploads").iterdir()), [])
        self.assertEqual(len(list(self.store.root.glob("*.sfnt"))), 1)
        self.assert_readable(self.store.load())

    def test_bad_final_chunk_removes_only_the_session_and_preserves_old_font(self):
        original = self.install()
        first = self.store.upload_chunk(chunk(b"\0\1\0\0"))
        with self.assertRaises(ValueError):
            self.store.upload_chunk(chunk(b"bad!", offset=4, upload_id=first["upload_id"]))
        self.assertEqual(self.store.load(), original)
        self.assertEqual(list((self.store.root / "uploads").iterdir()), [])

    def test_invalid_whole_uploads_preserve_the_old_font(self):
        original = self.install()
        for payload in ({"name": "font.exe", "data": encode(self.second_font)},
                        {"name": "font.ttf", "data": "bad base64!"},
                        {"name": "font.ttf", "data": encode(b"bad font")}):
            with self.subTest(name=payload["name"]), self.assertRaises(ValueError):
                self.store.upload(payload)
            self.assertEqual(self.store.load(), original)

    def test_compressed_font_expansion_limit_preserves_the_old_font(self):
        original = self.install()
        if len(self.woff_font) >= len(self.second_font):
            self.skipTest("tiny generated WOFF was not smaller than its SFNT")
        with patch.object(fonts, "MAX_FONT_BYTES", len(self.woff_font)):
            with self.assertRaisesRegex(ValueError, "32 MB"):
                self.store.upload({"name": "font.woff", "data": encode(self.woff_font)})
        self.assertEqual(self.store.load(), original)

    def test_manifest_commit_failure_preserves_old_font_and_cleans_temporary_file(self):
        original = self.install()
        real_replace = fonts.os.replace
        def fail_manifest(source, target):
            if Path(target) == self.store.manifest:
                raise OSError("simulated manifest commit failure")
            return real_replace(source, target)
        with patch.object(fonts.os, "replace", side_effect=fail_manifest), self.assertRaises(OSError):
            self.store.upload({"name": "new.ttf", "data": encode(self.second_font)})
        self.assertEqual(self.store.load(), original)
        self.assertEqual(list(self.store.root.glob("*.tmp-*")), [])

    def test_old_blob_cleanup_failure_does_not_report_a_committed_upload_as_failed(self):
        original = self.install()
        old_path = self.store.root / (original[0]["sha256"] + ".sfnt")
        real_unlink = Path.unlink
        def fail_old_blob(path, *args, **kwargs):
            if path == old_path:
                raise PermissionError("old blob busy")
            return real_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", autospec=True, side_effect=fail_old_blob):
            result = self.store.upload({"name": "new.ttf", "data": encode(self.second_font)})
        self.assertTrue(result["uploaded"])
        self.assertEqual(self.store.load()[0]["name"], "new.ttf")

    def test_damaged_font_blob_fails_integrity_checks(self):
        snapshot = self.install()
        (self.store.root / (snapshot[0]["sha256"] + ".sfnt")).write_bytes(b"tampered font")
        self.assertIsNone(self.store.load())

    def test_restore_reinstates_removed_original_blob_and_exact_metadata(self):
        original = self.install()
        self.install(self.second_font, "new.ttf")
        self.assertFalse((self.store.root / (original[0]["sha256"] + ".sfnt")).exists())
        self.store.restore(original)
        self.assertEqual(self.store.load(), original)
        self.assertEqual(len(list(self.store.root.glob("*.sfnt"))), 1)
        self.assert_readable(self.store.load())

    def test_restore_repairs_a_damaged_manifest(self):
        original = self.install()
        self.store.manifest.write_text("[]")
        self.assertFalse(self.store.info()["uploaded"])
        self.store.restore(original)
        self.assertEqual(self.store.load(), original)

    def test_restore_none_removes_font_and_invalidates_pending_upload(self):
        self.install()
        first = self.store.upload_chunk(chunk())
        self.store.restore(None)
        self.assertIsNone(self.store.load())
        self.assertFalse(self.store.info()["uploaded"])
        self.assertEqual(list(self.store.root.glob("*.sfnt")), [])
        with self.assertRaises(ValueError):
            self.store.upload_chunk(chunk(b"TAIL", offset=4, upload_id=first["upload_id"]))


if __name__ == "__main__":
    unittest.main()
