"""Original-image attachment contracts; every HTTP response is in memory."""
from __future__ import annotations

import asyncio
import io
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_tieba_lens import media as m


def image_bytes(fmt="PNG", color="red"):
    output = io.BytesIO()
    with Image.new("RGB", (12, 10), color) as image:
        image.save(output, format=fmt)
    return output.getvalue()


PNG = image_bytes()
URL = "https://imgsrc.baidu.com/forum/one.png"


def document(urls):
    return {"posts": [{"floor": 1, "author": {"avatar": "https://himg.bdimg.com/avatar"},
        "segments": [{"type": "image", "url": url} for url in urls], "comments": []}], "threads": []}


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.real_client = httpx.AsyncClient

    def mock_client(self, handler, *, jar_cookie=False):
        def factory(**kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            client = self.real_client(**kwargs)
            if jar_cookie:
                client.cookies.set("BDUSS", "MUST_NOT_BE_SENT", domain=".baidu.com")
            return client
        return patch.object(m.httpx, "AsyncClient", factory)

    async def test_preserves_bytes_and_source_order_despite_completion_order(self):
        urls = [URL.replace("one", value) for value in ("first", "second", "third")]
        active = peak = 0
        async def handler(request):
            nonlocal active, peak
            active += 1
            peak = max(active, peak)
            await asyncio.sleep(.03 if "first" in str(request.url) else .001)
            active -= 1
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        with self.mock_client(handler):
            items, warnings = await m.download_originals(document(urls), {}, self.root)
        self.assertEqual(warnings, [])
        self.assertEqual([item["url"] for item in items], urls)
        self.assertLessEqual(peak, 3)
        for item in items:
            self.assertEqual(Path(item["path"]).read_bytes(), PNG)
            self.assertEqual(Path(item["path"]).parent, self.root / "originals")
        self.assertEqual(list((self.root / "originals").glob("*.part")), [])

    async def test_depth_order_excludes_avatar_emoji_and_deduplicates(self):
        urls = [URL.replace("one", str(i)) for i in range(4)]
        doc = document([urls[0], urls[0]])
        doc["posts"][0]["segments"].insert(0, {"type": "emoji", "url": "https://tb2.bdstatic.com/emoji.png"})
        doc["posts"][0]["comments"] = [{"floor": 0, "segments": [{"type": "image", "url": urls[1]}],
            "comments": [{"segments": [{"type": "image", "url": urls[2]}]}]}]
        doc["threads"] = [{"images": [urls[3], urls[0]]}]
        requested = []
        def handler(request):
            requested.append(str(request.url))
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        with self.mock_client(handler):
            items, warnings = await m.download_originals(doc, {}, self.root)
        self.assertEqual([item["url"] for item in items], urls)
        self.assertEqual(set(requested), set(urls))
        self.assertIn("楼中楼", items[1]["label"])
        self.assertIn("主页帖子", items[3]["label"])
        self.assertFalse(warnings)

    async def test_occurrence_budget_applies_before_dedup(self):
        calls = []
        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        with self.mock_client(handler):
            items, _ = await m.download_originals(document([URL, URL, URL + "?outside=1"]), {"image_limit": 2}, self.root)
        self.assertEqual(len(items), 1)
        self.assertEqual(calls, [URL])

    async def test_disabled_and_zero_budget_do_not_make_directories_or_requests(self):
        def handler(request):
            raise AssertionError("request must not be sent")
        with self.mock_client(handler):
            for options in ({"include_images": False}, {"image_limit": 0}):
                self.assertEqual(await m.download_originals(document([URL]), options, self.root), ([], []))
        self.assertFalse((self.root / "originals").exists())

    async def test_failed_first_keeps_later_image(self):
        second = URL + "?second=1"
        def handler(request):
            return (httpx.Response(404) if str(request.url) == URL else
                    httpx.Response(200, content=PNG, headers={"content-type": "image/png"}))
        with self.mock_client(handler):
            items, warnings = await m.download_originals(document([URL, second]), {}, self.root)
        self.assertEqual([item["url"] for item in items], [second])
        self.assertIn("1 张", warnings[0])
        self.assertIn("源站拒绝", warnings[0])

    async def test_octet_stream_gif_animation_exact_bytes(self):
        output = io.BytesIO()
        frames = [Image.new("RGB", (12, 10), color) for color in ("red", "blue")]
        try:
            frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=60, loop=0)
        finally:
            for image in frames:
                image.close()
        raw = output.getvalue()
        with self.mock_client(lambda request: httpx.Response(200, content=raw,
                                headers={"content-type": "application/octet-stream"})):
            items, warnings = await m.download_originals(document([URL]), {}, self.root)
        self.assertFalse(warnings)
        self.assertTrue(items[0]["path"].endswith(".gif"))
        self.assertEqual(Path(items[0]["path"]).read_bytes(), raw)
        with Image.open(items[0]["path"]) as image:
            self.assertEqual(image.n_frames, 2)

    async def test_supported_formats_and_magic_rejects_fake_mime(self):
        for fmt in ("PNG", "JPEG", "WEBP"):
            raw = image_bytes(fmt)
            with self.subTest(fmt=fmt), self.mock_client(lambda request: httpx.Response(
                    200, content=raw, headers={"content-type": "application/octet-stream"})):
                items, warnings = await m.download_originals(document([URL]), {}, self.root)
                self.assertEqual(Path(items[0]["path"]).read_bytes(), raw)
                self.assertFalse(warnings)
        for raw in (b"<html>not image</html>", PNG[:-8]):
            with self.subTest(raw=raw[:10]), self.mock_client(lambda request: httpx.Response(
                    200, content=raw, headers={"content-type": "image/png"})):
                items, warnings = await m.download_originals(document([URL]), {}, self.root)
                self.assertFalse(items)
                self.assertTrue(warnings)

    async def test_single_and_total_byte_budgets(self):
        def handler(request):
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        with self.mock_client(handler), patch.object(m, "_MAX_IMAGE_BYTES", len(PNG) - 1):
            items, warnings = await m.download_originals(document([URL]), {}, self.root)
        self.assertFalse(items)
        self.assertIn("单张原图", warnings[0])
        with self.mock_client(handler), patch.object(m, "_MAX_TOTAL_BYTES", len(PNG)):
            items, warnings = await m.download_originals(document([URL, URL + "?second"]), {}, self.root)
        self.assertEqual(len(items), 1)
        self.assertIn("总大小", warnings[0])

    async def test_streaming_size_limit_without_declared_length_writes_nothing(self):
        class Chunks(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield PNG[:30]
                await asyncio.sleep(0)
                yield PNG[30:]
        with self.mock_client(lambda request: httpx.Response(200, stream=Chunks(),
                headers={"content-type": "image/png"})), patch.object(m, "_MAX_IMAGE_BYTES", len(PNG) - 1):
            items, warnings = await m.download_originals(document([URL]), {}, self.root)
        self.assertFalse(items)
        self.assertIn("单张原图", warnings[0])
        self.assertFalse(list((self.root / "originals").iterdir()))

    async def test_declared_length_mismatch_and_pixel_budget(self):
        with self.mock_client(lambda request: httpx.Response(200, content=PNG,
                headers={"content-type": "image/png", "content-length": str(len(PNG) + 1)})):
            items, warnings = await m.download_originals(document([URL]), {}, self.root)
        self.assertFalse(items)
        self.assertIn("不完整", warnings[0])
        with self.mock_client(lambda request: httpx.Response(200, content=PNG,
                headers={"content-type": "image/png"})), patch.object(m, "_MAX_PIXELS", 100):
            items, warnings = await m.download_originals(document([URL]), {}, self.root)
        self.assertFalse(items)
        self.assertIn("像素", warnings[0])

    async def test_time_budget_and_first_failure_do_not_hang(self):
        async def handler(request):
            await asyncio.sleep(1)
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        started = time.monotonic()
        with self.mock_client(handler), patch.object(m, "_TOTAL_SECONDS", .04):
            items, warnings = await m.download_originals(document([URL, URL + "?second"]), {}, self.root)
        self.assertFalse(items)
        self.assertIn("超时", warnings[0])
        self.assertLess(time.monotonic() - started, .5)
        self.assertFalse(list((self.root / "originals").iterdir()))

    async def test_unsafe_urls_and_redirects_are_not_fetched(self):
        requests = []
        def handler(request):
            requests.append(str(request.url))
            return httpx.Response(302, headers={"location": "https://127.0.0.1/private"})
        with self.mock_client(handler):
            items, warnings = await m.download_originals(document(["https://imgsrc.baidu.com.evil.test/x", URL]), {}, self.root)
        self.assertFalse(items)
        self.assertEqual(requests, [URL])
        self.assertIn("安全地址", warnings[0])

    async def test_safe_https_redirect_retains_bytes_and_strips_cookiejar(self):
        second = "https://tiebapic.baidu.com/image.jpg"
        seen = []
        def handler(request):
            self.assertNotIn("cookie", request.headers)
            self.assertNotIn("authorization", request.headers)
            seen.append(str(request.url))
            if str(request.url) == URL:
                return httpx.Response(302, headers={"location": second, "set-cookie": "BDUSS=SECRET; Domain=.baidu.com"})
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        with self.mock_client(handler, jar_cookie=True):
            items, warnings = await m.download_originals(document([URL]), {"cookie": "BDUSS=NEVER_READ"}, self.root)
        self.assertEqual(seen, [URL, second])
        self.assertEqual(Path(items[0]["path"]).read_bytes(), PNG)
        self.assertFalse(warnings)

    async def test_invalid_root_does_not_create_outside_job(self):
        absent = self.root / "not-created-job"
        items, warnings = await m.download_originals(document([URL]), {}, absent)
        self.assertFalse(items)
        self.assertTrue(warnings)
        self.assertFalse(absent.exists())
        regular_file = self.root / "file"
        regular_file.write_bytes(b"x")
        items, warnings = await m.download_originals(document([URL]), {}, regular_file)
        self.assertFalse(items)
        self.assertEqual(regular_file.read_bytes(), b"x")

    def test_absolute_count_limit_and_legacy_http_dedup(self):
        urls = [URL + f"?n={i}" for i in range(150)]
        self.assertEqual(len(m._collect(document(urls), {"image_limit": 9999})), 100)
        self.assertEqual(len(m._collect(document([URL, URL.replace("https://", "http://")]), {})), 1)


class SafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_and_mixed_dns_answers_are_rejected(self):
        for address in ("127.0.0.1", "192.168.1.1", "169.254.169.254", "::1", "100.64.0.1"):
            rows = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]
            with self.subTest(address=address), patch.object(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=rows)):
                with self.assertRaises(m._MediaFailure):
                    await m._public_address("imgsrc.baidu.com")

    async def test_transport_connects_to_pinned_address_with_original_tls_name(self):
        seen = []
        class FakePool:
            async def handle_async_request(self, request):
                seen.append(request)
                return httpx.Response(200)
            async def aclose(self):
                pass
        transport = m._PinnedMediaTransport()
        transport.pools["imgsrc.baidu.com"] = FakePool()
        with patch.object(m, "_public_address", AsyncMock(return_value="8.8.8.8")):
            await transport.handle_async_request(httpx.Request("GET", URL, headers={"cookie": "SECRET"}))
        self.assertEqual(seen[0].url.host, "8.8.8.8")
        self.assertEqual(seen[0].headers["host"], "imgsrc.baidu.com")
        self.assertEqual(seen[0].extensions["sni_hostname"], "imgsrc.baidu.com")
        self.assertNotIn("cookie", seen[0].headers)
        await transport.aclose()


if __name__ == "__main__":
    unittest.main()
