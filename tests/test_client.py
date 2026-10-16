"""Provider contracts and hostile-input checks; no external network access."""
from __future__ import annotations

import asyncio
import json
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_tieba_lens import client as c
from astrbot_plugin_tieba_lens.links import extract_targets, parse_url
from astrbot_plugin_tieba_lens.vendor import protocol as p
from astrbot_plugin_tieba_lens.vendor.proto import Message, ProtobufError, encode_bytes as b, encode_uint as u


def user(uid, name, level=None):
    result = u(2, uid) + b(4, name) + b(5, f"tb.1.{uid}")
    if level is not None:
        result += u(23, level)
    return result


def text(value):
    return b(2, value)


def envelope(data):
    return b(1, u(1, 0)) + b(2, data)


def page(*, offset=0, count=3, empty=False):
    thread = u(1, 123) + b(3, "测试帖子") + u(4, 60) + u(5, 100) + u(56, 8) + u(40, 100)
    data = b(2, u(1, 77) + b(2, "原神")) + b(8, thread)
    data += b(13, user(8, "楼主", 12)) + b(13, user(9, "回复者"))
    if empty:
        return envelope(data)
    for idx in range(count):
        floor = offset + idx + 1
        uid = 8 if floor == 1 else 9
        post = u(1, 99 + floor) + u(19, uid) + u(4, 1700000000 + floor)
        # Omitted forum levels and omitted floor fields must not become zero.
        if floor == 1:
            post += u(3, 1)
            content = (b(5, text("前文")) + b(5, u(1, 3) + b(25, "https://imgsrc.baidu.com/a.jpg"))
                       + b(5, text("中间")) + b(5, u(1, 2) + b(2, "image_emoticon3") + b(11, "吐舌"))
                       + b(5, text("后文")))
            post += content
            sub = u(1, 701) + u(4, 9) + b(2, text("回复 ")) + b(2, u(1, 2) + b(11, "微笑"))
            sub += b(2, text("楼中楼"))
            post += u(13, 4) + b(15, b(2, sub))
        else:
            post += b(5, text(f"第{floor}条回复"))
        data += b(6, post)
    return envelope(data)


def wire_post(pid, floor=None, likes=None, *, uid=9, comments=(), empty_agree=False):
    result = u(p.POST_ID, pid) + u(p.POST_AUTHOR_ID, uid) + b(p.POST_CONTENT, text(str(pid)))
    if floor is not None:
        result += u(p.POST_FLOOR, floor)
    if likes is not None:
        result += b(p.POST_AGREE, u(1, likes))
    elif empty_agree:
        result += b(p.POST_AGREE, b"")
    if comments:
        result += u(p.POST_SUB_POST_NUMBER, len(comments))
        result += b(p.POST_SUB_POST_LIST, b"".join(b(p.SUB_LIST, comment) for comment in comments))
    return result


def wire_subcomment(pid, floor, likes=None, *, empty_agree=False):
    result = (u(p.SUBL_ID, pid) + u(p.SUBL_FLOOR, floor) + u(p.SUBL_AUTHOR_ID, 9)
              + b(p.SUBL_SHELL, text(str(pid))))
    if likes is not None:
        result += b(p.SUBL_AGREE, u(1, likes))
    elif empty_agree:
        result += b(p.SUBL_AGREE, b"")
    return result


def ranked_page(rows, *, total=None):
    thread = u(p.TH_ID, 123) + b(p.TH_TITLE, "点赞排序") + u(p.TH_AUTHOR_ID, 8) + u(40, 100)
    if total is not None:
        thread += u(p.TH_REPLY_NUM, total)
    data = b(p.PAGE_THREAD, thread) + b(p.PAGE_USER_LIST, user(8, "楼主"))
    data += b(p.PAGE_USER_LIST, user(9, "回复者"))
    data += b"".join(b(p.PAGE_POST_LIST, row) for row in rows)
    return c._data(envelope(data))


class LinkTests(unittest.TestCase):
    def test_http_https_canonical_dedup(self):
        targets = extract_targets("http://tieba.baidu.com/p/00123?foo=1 https://m.tieba.baidu.com/p/123")
        self.assertEqual(targets, [{"kind": "thread", "id": "123", "url": "https://tieba.baidu.com/p/123"}])

    def test_forum_and_user(self):
        forum = parse_url("https://tieba.baidu.com/f?kw=%E5%8E%9F%E7%A5%9E%26A")
        self.assertEqual(forum["id"], "原神&A")
        self.assertEqual(parse_url(forum["url"]), forum)
        self.assertEqual(parse_url("http://tieba.baidu.com/home/main?id=tb.1.aBC_2")["kind"], "user")
        self.assertEqual(parse_url("https://tieba.baidu.com/home/main?un=%E6%B5%8B%E8%AF%95")["name"], "测试")

    def test_unsafe_urls(self):
        for value in ("https://tieba.baidu.com.evil.com/p/123", "https://tieba.baidu.com@evil.com/p/123",
                      "https://x@tieba.baidu.com/p/123", "https://tieba.baidu.com:999/p/123",
                      "https://127.0.0.1/p/123", "file://tieba.baidu.com/p/123",
                      "https://tieba.baidu.com\\@evil.com/p/123", "https://tieba.baidu.com/p/123?tid=124",
                      "https://tieba.baidu.com/p/0", "https://tieba.baidu.com/p/123?tid=123&tid=124",
                      "https://tieba.baidu.com/p/99999999999999999999",
                      "https://tieba.baidu.com/home/main?id=99999999999999999999",
                      "https://tb.baidu.com/", "https://tieba.baidu.com./p/123"):
            with self.subTest(value=value):
                self.assertIsNone(parse_url(value))

    def test_current_text_excludes_history_quotes_and_forward(self):
        message = {"message": [
            {"type": "reply", "data": {"content": "https://tieba.baidu.com/p/999"}},
            {"type": "text", "data": {"text": "https://tieba.baidu.com/p/123"}},
            {"type": "forward", "data": {"content": "https://tieba.baidu.com/p/888"}},
        ], "history": [{"content": "https://tieba.baidu.com/p/777"}],
            "quote_message": {"message": "https://tieba.baidu.com/p/666"}}
        self.assertEqual([t["id"] for t in extract_targets(message)], ["123"])

    def test_json_xml_cards_miniapp(self):
        card = {"app": "com.tencent.miniapp_01", "meta": {"detail_1": {
            "title": "百度贴吧", "pagePath": "pages/pb/pb?tid=123"}}}
        self.assertEqual(extract_targets({"type": "json", "data": {"data": json.dumps(card)}})[0]["id"], "123")
        xml = '<msg><source name="百度贴吧"/><weappinfo pagepath="pages/pb/pb?tid=456"/></msg>'
        self.assertEqual(extract_targets(xml)[0]["id"], "456")
        self.assertEqual(extract_targets({"type": "miniapp", "data": {
            "appName": "其他应用", "pagePath": "pages/pb/pb?tid=123"}}), [])
        share = '{"url":"http://tieba.baidu.com/p/123","quote":{"url":"https://tieba.baidu.com/p/999"}}'
        self.assertEqual([t["id"] for t in extract_targets(share)], ["123"])
        card["meta"]["detail_1"]["pagePath"] = "pages%2Fpb%2Fpb%3Ftid%3D123"
        self.assertEqual(extract_targets(card)[0]["id"], "123")
        self.assertEqual(extract_targets("[分享] https://tieba.baidu.com/p/123")[0]["id"], "123")

    def test_bounded_recursion_and_xml_entities(self):
        cycle = {}
        cycle["data"] = cycle
        self.assertEqual(extract_targets(cycle), [])
        self.assertEqual(extract_targets("x" * 65537 + "https://tieba.baidu.com/p/123"), [])
        xml = '<!DOCTYPE x [<!ENTITY e "https://tieba.baidu.com/p/123">]><msg url="&e;"/>'
        self.assertEqual(extract_targets(xml), [])
        self.assertEqual(len(extract_targets(" ".join(f"https://tieba.baidu.com/p/{i}" for i in range(1, 50)), 999)), 8)


class CodecTests(unittest.TestCase):
    def test_reject_overflow_and_truncation(self):
        for raw in (b"\x08" + b"\xff" * 9 + b"\x02", b"\x12\x10abc", b"\x00"):
            with self.assertRaises(ProtobufError):
                Message.load(raw)

    def test_fixed_wire_types_are_skippable(self):
        msg = Message.load(b"\x09" + b"\x00" * 8 + u(2, 7))
        self.assertEqual(msg.uint(2), 7)


class DocumentSortingTests(unittest.TestCase):
    def test_cached_document_sort_pins_op_uses_real_floors_and_recurses(self):
        def post(pid, floor, likes, available=True, **extra):
            return {"id": pid, "floor": floor, "likes": likes,
                    "likes_available": available, "comments": [], **extra}

        nested = [post("deep-unknown", 1, 0, False), post("deep-zero", 2, 0),
                  post("deep-high", 4, 8), post("deep-tie", 3, 8)]
        comments = [post("unknown-comment", 1, 999, False), post("zero-comment", 2, 0),
                    post("high-comment", 8, 7, comments=nested), post("tie-comment", 3, 7)]
        doc = {"posts": [
            post("unknown-first", 99, 0, False), post("floor-nine", 9, 4),
            post("op", 1, None, False, comments=comments), post("floor-two", 2, 4),
            post("unknown-last", 3, 999, False),
            post("inferred-first", 8, 4, floor_inferred=True),
            post("inferred-last", 4, 4, floor_inferred=True),
        ], "warnings": [], "pagination": {"fetched": 7}}
        self.assertIs(c.sort_document(doc, {"sort": "hot"}), doc)
        self.assertEqual([row["id"] for row in doc["posts"]],
                         ["op", "floor-two", "floor-nine", "inferred-first", "inferred-last", "unknown-first", "unknown-last"])
        self.assertEqual([row["id"] for row in doc["posts"][0]["comments"]],
                         ["tie-comment", "high-comment", "zero-comment", "unknown-comment"])
        descendants = doc["posts"][0]["comments"][1]["comments"]
        self.assertEqual([row["id"] for row in descendants],
                         ["deep-tie", "deep-high", "deep-zero", "deep-unknown"])
        self.assertIsNone(doc["posts"][-1]["likes"])
        self.assertIsNone(descendants[-1]["likes"])
        self.assertEqual(descendants[-2]["likes"], 0)
        self.assertEqual(doc["pagination"], {"fetched": 7})
        c.sort_document(doc, {"sort": "hot"})
        self.assertEqual(doc["warnings"], [c._MISSING_LIKES_WARNING])

    def test_unknown_subcomments_keep_original_order_without_inventing_likes(self):
        doc = {"posts": [{"floor": 1, "likes": None, "comments": [
            {"id": "later-floor-first", "floor": 8, "likes": 0, "likes_available": False},
            {"id": "earlier-floor-last", "floor": 2, "likes": None, "likes_available": False},
        ]}], "warnings": []}
        c.sort_document(doc, {"sort": "hot"})
        self.assertEqual([row["id"] for row in doc["posts"][0]["comments"]],
                         ["later-floor-first", "earlier-floor-last"])
        self.assertTrue(all(row["likes"] is None for row in doc["posts"][0]["comments"]))

    def test_cached_explicit_modes_preserve_top_level_and_comment_order(self):
        for mode in ["default", "latest"]:
            doc = {"posts": [{"id": "op", "floor": 1, "likes": 0, "comments": [
                {"id": "low-sub", "likes": 1}, {"id": "high-sub", "likes": 9}]},
                {"id": "low", "likes": 1}, {"id": "high", "likes": 9}], "warnings": []}
            c.sort_document(doc, {"sort": mode})
            self.assertEqual([row["id"] for row in doc["posts"]], ["op", "low", "high"])
            self.assertEqual([row["id"] for row in doc["posts"][0]["comments"]], ["low-sub", "high-sub"])
            self.assertEqual(doc["warnings"], [])


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        c._SHARED_BLOCK_UNTIL = 0
        c._SHARED_LAST_START = 0
        # Existing provider contracts use one chronological request explicitly.
        self.client = c.TiebaClient({"request_delay": .1, "retries": 0, "sort": "default"})
        self.addAsyncCleanup(self.client.close)

    async def mock_http(self, handler):
        await self.client._http.aclose()
        self.client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_ordered_content_null_level_and_subcomment(self):
        await self.mock_http(lambda request: httpx.Response(200, content=page()))
        doc = await self.client.parse(parse_url("http://tieba.baidu.com/p/123"))
        self.assertEqual(doc["author"]["name"], "楼主")
        self.assertEqual(doc["author"]["level"], 12)
        self.assertTrue(doc["author"]["avatar"].startswith("https://himg.bdimg.com/"))
        self.assertEqual([part["type"] for part in doc["posts"][0]["segments"]],
                         ["text", "image", "text", "emoji", "text"])
        self.assertIsNone(doc["posts"][1]["author"]["level"])
        self.assertEqual(doc["posts"][0]["comments"][0]["segments"][2]["text"], "楼中楼")
        self.assertIsNone(doc["posts"][0]["comments"][0]["author"]["level"])
        self.assertEqual(doc["posts"][0]["comment_total"], 4)
        self.assertTrue(doc["pagination"]["has_more"])
        self.assertEqual(doc["forum"], "原神")
        json.dumps(doc, ensure_ascii=False)

    async def test_reply_limit_excludes_first_floor_and_images_preserve_position(self):
        await self.mock_http(lambda request: httpx.Response(200, content=page()))
        doc = await self.client.parse(parse_url("https://tieba.baidu.com/p/123"),
                                      {"comment_limit": 0, "image_limit": 0, "include_emoji": False})
        self.assertEqual(len(doc["posts"]), 1)
        self.assertEqual(doc["posts"][0]["segments"][1]["type"], "image")
        self.assertEqual(doc["posts"][0]["segments"][1]["url"], "")
        self.assertEqual(doc["posts"][0]["segments"][3]["url"], "")

    async def test_pagination_dedup_and_real_post_ids(self):
        calls = []
        def handler(request):
            calls.append(request)
            raw = page(count=30) if len(calls) == 1 else page(offset=29, count=15)
            return httpx.Response(200, content=raw)
        await self.mock_http(handler)
        doc = await self.client.parse(parse_url("https://tieba.baidu.com/p/123"), {"comment_limit": 35})
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(doc["posts"]), 36)
        self.assertEqual(len({post["id"] for post in doc["posts"]}), 36)

    async def test_hot_is_default_and_sorts_complete_batch_before_reply_limit(self):
        first = ranked_page([wire_post(100, 1, 0, uid=8)])
        # High-like rows arrive after the requested count; the OP also appears
        # out of order in this response and must not displace the real first floor.
        hot = ranked_page([
            wire_post(108, 8), wire_post(107, 7, 0), wire_post(106, 6, 5),
            wire_post(105, 5, 20), wire_post(102, 2, 20), wire_post(104, 4, 1),
            wire_post(100, 1, 999, uid=8), wire_post(103, 3, 80),
        ])
        self.client.options.pop("sort")
        with patch.object(self.client, "_proto", AsyncMock(side_effect=[first, hot])) as request:
            doc = await self.client.parse(parse_url("https://tieba.baidu.com/p/123"), {"comment_limit": 3})
        self.assertEqual([post["id"] for post in doc["posts"]], ["100", "103", "102", "105"])
        self.assertEqual([post["likes"] for post in doc["posts"]], [0, 80, 20, 20])
        self.assertEqual(doc["pagination"]["fetched"], 4)
        self.assertTrue(doc["pagination"]["has_more"])
        sorts = [Message.load(call.args[2]).message(p.REQ_PAGE_DATA).uint(p.REQ_PAGE_SORT)
                 for call in request.call_args_list]
        self.assertEqual(sorts, [0, 2])
        self.assertEqual(doc["warnings"].count(c._MISSING_LIKES_WARNING), 1)

    async def test_hot_keeps_zero_known_and_missing_likes_unknown_in_source_order(self):
        first = ranked_page([wire_post(100, 1, uid=8)])
        hot = ranked_page([
            wire_post(109, 9), wire_post(103, 3, empty_agree=True),
            wire_post(107, 7, 0), wire_post(104, 4, 2),
        ])
        with patch.object(self.client, "_proto", AsyncMock(side_effect=[first, hot])):
            doc = await self.client.parse(parse_url("https://tieba.baidu.com/p/123"),
                                          {"sort": "hot", "comment_limit": 10})
        self.assertEqual([post["id"] for post in doc["posts"]], ["100", "104", "107", "109", "103"])
        self.assertEqual([post["likes"] for post in doc["posts"]], [None, 2, 0, None, None])
        self.assertEqual([post["likes_available"] for post in doc["posts"]], [False, True, True, False, False])
        self.assertEqual(doc["warnings"].count(c._MISSING_LIKES_WARNING), 1)

    async def test_hot_sorts_all_returned_subcomments_before_subcomment_limit(self):
        comments = [wire_subcomment(704, 4), wire_subcomment(702, 2, 0),
                    wire_subcomment(703, 3, 9), wire_subcomment(701, 1, 9)]
        first = ranked_page([wire_post(100, 1, 0, uid=8, comments=comments)])
        with patch.object(self.client, "_proto", AsyncMock(return_value=first)) as request:
            doc = await self.client.parse(parse_url("https://tieba.baidu.com/p/123"),
                {"sort": "hot", "comment_limit": 0, "subcomment_limit": 2})
        self.assertEqual(request.await_count, 1)
        self.assertEqual(len(doc["posts"]), 1)
        self.assertEqual([post["id"] for post in doc["posts"][0]["comments"]], ["701", "703"])
        self.assertEqual(doc["posts"][0]["comment_total"], 4)
        self.assertTrue(any("2 条楼中楼" in warning for warning in doc["warnings"]))
        self.assertEqual(doc["warnings"].count(c._MISSING_LIKES_WARNING), 1)

    async def test_hot_continues_when_filtering_first_batch_does_not_fill_limit(self):
        first = ranked_page([wire_post(100, 1, 0, uid=8)])
        filtered = ranked_page([wire_post(100, 1, 0, uid=8)] +
                              [wire_post(100 + floor, floor, floor) for floor in range(2, 31)])
        second = ranked_page([wire_post(132, 32, 1, uid=8), wire_post(131, 31, 8, uid=8)])
        with patch.object(self.client, "_proto", AsyncMock(side_effect=[first, filtered, second])) as request:
            doc = await self.client.parse(parse_url("https://tieba.baidu.com/p/123"),
                {"sort": "hot", "comment_limit": 2, "only_author": True})
        self.assertEqual([post["id"] for post in doc["posts"]], ["100", "131", "132"])
        self.assertEqual(request.await_count, 3)
        self.assertEqual(Message.load(request.call_args.args[2]).message(p.REQ_PAGE_DATA).uint(p.REQ_PAGE_PN), 2)

    async def test_explicit_default_and_latest_keep_interface_order(self):
        first = ranked_page([wire_post(100, 1, 0, uid=8), wire_post(107, 7, 1), wire_post(102, 2, 99)])
        latest = ranked_page([wire_post(109, 9, 1), wire_post(108, 8, 99)])
        for mode, responses, expected, sorts in [
            ("default", [first], ["100", "107", "102"], [0]),
            ("latest", [first, latest], ["100", "109", "108"], [0, 1]),
        ]:
            with self.subTest(mode=mode), patch.object(self.client, "_proto", AsyncMock(side_effect=responses)) as request:
                doc = await self.client.parse(parse_url("https://tieba.baidu.com/p/123"),
                    {"sort": mode, "comment_limit": 2})
            self.assertEqual([post["id"] for post in doc["posts"]], expected)
            self.assertEqual([Message.load(call.args[2]).message(p.REQ_PAGE_DATA).uint(p.REQ_PAGE_SORT)
                              for call in request.call_args_list], sorts)

    async def test_failures_not_empty_success(self):
        samples = [b"", b"<html>changed</html>", envelope(b(2, b(2, "吧"))),
                   b(1, u(1, 100) + b(2, "NO SECRET SHOULD APPEAR")), page(empty=True)]
        for sample in samples:
            with self.subTest(sample=sample[:20]):
                await self.mock_http(lambda request: httpx.Response(200, content=sample))
                with self.assertRaises(c.TiebaError) as context:
                    await self.client.parse(parse_url("https://tieba.baidu.com/p/123"))
                self.assertNotIn("NO SECRET", str(context.exception))

    async def test_shared_block_no_retry_or_followup(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(429, content=b"rate limit")
        await self.mock_http(handler)
        with self.assertRaises(c.TiebaBlockedError):
            await self.client.parse(parse_url("https://tieba.baidu.com/p/123"), {"retries": 3})
        other = c.TiebaClient({})
        try:
            with self.assertRaises(c.TiebaBlockedError):
                await other.parse(parse_url("https://tieba.baidu.com/p/123"))
        finally:
            await other.close()
        self.assertEqual(len(calls), 1)

    async def test_html_verification_marks_cooldown(self):
        await self.mock_http(lambda request: httpx.Response(200, content="<html>百度安全验证</html>".encode()))
        with self.assertRaises(c.TiebaBlockedError):
            await self.client.parse(parse_url("https://tieba.baidu.com/p/123"))

    async def test_short_redirect_validates_before_fetch(self):
        for location in ("http://tieba.baidu.com/p/123", "https://127.0.0.1/a",
                         "https://tieba.baidu.com.evil.test/p/123"):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(302, headers={"location": location})
            await self.mock_http(handler)
            with self.assertRaises(c.TiebaError):
                await self.client.parse(parse_url("https://tb.baidu.com/AbCd"))
            self.assertEqual(len(calls), 1)

    async def test_short_redirect_thread_via_https_api(self):
        def handler(request):
            if request.url.host == "tb.baidu.com":
                return httpx.Response(302, headers={"location": "https://tieba.baidu.com/p/123?share=1"})
            self.assertEqual(request.url.host, "tiebac.baidu.com")
            return httpx.Response(200, content=page())
        await self.mock_http(handler)
        doc = await self.client.parse(parse_url("http://tb.baidu.com/AbCd"))
        self.assertEqual(doc["url"], "https://tieba.baidu.com/p/123")

    async def test_short_static_html_and_js_redirects(self):
        samples = [
            '<meta content="0; URL=\'https://tieba.baidu.com/p/123?x=1&amp;y=2\'" http-equiv="refresh">',
            '<meta http-equiv="refresh" content="0;url=//tieba.baidu.com/p/123">',
            '<script>window.location.replace("https:\\/\\/tieba.baidu.com\\/p\\/123");</script>',
            "<script>window.location.href = 'https://tieba.baidu.com/p/123';</script>",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                def handler(request):
                    if request.url.host == "tb.baidu.com":
                        return httpx.Response(200, content=sample.encode())
                    return httpx.Response(200, content=page())
                await self.mock_http(handler)
                doc = await self.client.parse(parse_url("https://tb.baidu.com/AbCd"))
                self.assertEqual(doc["id"], "123")

    async def test_cookiejar_domain_cookie_never_reaches_short_host(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(302, headers={"location": "https://evil.example/p/123"})
        await self.mock_http(handler)
        self.client._http.cookies.set("BDUSS", "JAR_SECRET", domain=".baidu.com")
        with self.assertRaises(c.TiebaError):
            await self.client.parse(parse_url("https://tb.baidu.com/AbCd"))
        self.assertNotIn("cookie", seen[0].headers)

    async def test_reject_response_byte_budget(self):
        await self.mock_http(lambda request: httpx.Response(200, content=b"x" * 1025))
        with patch.object(c, "_MAX_RESPONSE", 1024):
            with self.assertRaises(c.TiebaError) as context:
                await self.client.parse(parse_url("https://tieba.baidu.com/p/123"))
        self.assertEqual(context.exception.code, "response_budget")

    async def test_cookie_not_forwarded_to_short_host_or_diagnostics(self):
        self.client.options["cookie"] = "BDUSS=SECRET"
        def handler(request):
            if request.url.host == "tb.baidu.com":
                self.assertNotIn("cookie", request.headers)
                return httpx.Response(302, headers={"location": "https://tieba.baidu.com/p/123"})
            self.assertEqual(request.headers["cookie"], "BDUSS=SECRET")
            return httpx.Response(200, content=page())
        await self.mock_http(handler)
        await self.client.parse(parse_url("https://tb.baidu.com/AbCd"))
        self.assertNotIn("SECRET", json.dumps(self.client.diagnostics()))

    async def test_retry_network_error_only(self):
        calls = []
        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                raise httpx.ConnectError("not exposed", request=request)
            return httpx.Response(200, content=page())
        await self.mock_http(handler)
        doc = await self.client.parse(parse_url("https://tieba.baidu.com/p/123"), {"retries": 1})
        self.assertEqual(doc["kind"], "thread")
        self.assertEqual(len(calls), 2)

    async def test_forum_metadata_and_empty_failure(self):
        def handler(request):
            thread = u(1, 123) + b(3, "测试帖") + u(56, 8) + b(142, text("摘要"))
            data = b(2, u(1, 77) + b(2, "原神") + u(9, 300) + u(10, 100))
            data += b(7, thread) + b(17, user(8, "作者", 7))
            return httpx.Response(200, content=envelope(data))
        await self.mock_http(handler)
        doc = await self.client.parse(parse_url("https://tieba.baidu.com/f?kw=原神"))
        self.assertEqual(doc["stats"]["members"], 300)
        self.assertEqual(doc["threads"][0]["author"]["name"], "作者")
        self.assertEqual(doc["threads"][0]["excerpt"], "摘要")
        await self.mock_http(lambda request: httpx.Response(200,
            content=envelope(b(2, b(2, "原神") + u(10, 100)))))
        with self.assertRaises(c.TiebaError):
            await self.client.parse(parse_url("https://tieba.baidu.com/f?kw=原神"))

    async def test_user_profile_no_forum_level_invention(self):
        await self.mock_http(lambda request: httpx.Response(200, content=envelope(
            b(1, user(8, "作者", 12) + u(30, 77) + b(34, "签名"))
            + b(4, u(2, 123) + b(7, "个人帖") + b(49, text("首段"))))))
        doc = await self.client.parse(parse_url("https://tieba.baidu.com/home/main?id=8"))
        self.assertIsNone(doc["author"]["level"])
        self.assertEqual(doc["stats"]["followers"], 77)
        self.assertEqual(doc["threads"][0]["title"], "个人帖")
        self.assertEqual(doc["description"], "签名")


class NetworkSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_reject_private_dns_even_if_public_answer_also_present(self):
        rows = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=rows)):
            with self.assertRaises(c.TiebaError):
                await c._resolve_public("tiebac.baidu.com")

    async def test_transport_pins_ip_and_tls_identity(self):
        seen = []
        class FakeTransport:
            async def handle_async_request(self, request):
                seen.append(request)
                return httpx.Response(200)
            async def aclose(self):
                pass
        transport = c._PinnedTransport()
        transport._transports["tiebac.baidu.com"] = FakeTransport()
        with patch.object(c, "_resolve_public", AsyncMock(return_value="8.8.8.8")):
            await transport.handle_async_request(httpx.Request("POST", "https://tiebac.baidu.com/c/f/pb/page"))
        self.assertEqual(seen[0].url.host, "8.8.8.8")
        self.assertEqual(seen[0].headers["host"], "tiebac.baidu.com")
        self.assertEqual(seen[0].extensions["sni_hostname"], "tiebac.baidu.com")
        await transport.aclose()


if __name__ == "__main__":
    unittest.main()
