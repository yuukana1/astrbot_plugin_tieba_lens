"""Sanitised QQ mini-program fixtures; no real users, tokens or network calls."""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_tieba_lens import miniapp as m
from astrbot_plugin_tieba_lens.links import (
    extract_targets,
    parse_miniapp_share_url,
    parse_url,
)
from astrbot_plugin_tieba_lens.vendor.proto import Message
from astrbot_plugin_tieba_lens.vendor.proto import encode_bytes as b
from astrbot_plugin_tieba_lens.vendor.proto import encode_uint as u

SHARE = 'https://m.q.qq.com/a/s/' + '0123456789abcdef' * 2
TARGET = parse_miniapp_share_url(SHARE)
POST = parse_url('https://tieba.baidu.com/p/12345678')


def card():
    return {'app': 'com.tencent.miniapp.lua', 'view': 'miniapp', 'meta': {'miniapp': {
        'source': '贴吧', 'title': '示例帖子', 'jumpUrl': SHARE,
        'pcJumpUrl': 'miniapp://open/1036?url=' + quote(SHARE, safe=''),
        'ark_reserved1': json.dumps({'mini_app_id': m.TIEBA_APP_ID, 'applet_source': 2})}}}


def response(appid=m.TIEBA_APP_ID, path='pages/uni-pb/uni-pb.html?tid=12345678', code=0):
    return (u(2, code) + b(4, b(2, b(1, appid) + b(11, b(1, path))))).hex()


class RecognitionTests(unittest.TestCase):
    def test_real_card_shape_and_sdk_json_wrappers(self):
        self.assertEqual(extract_targets(card()), [TARGET])
        wrapped = {'type': 'json', 'data': {'data': json.dumps(card(), ensure_ascii=False)}}
        self.assertEqual(extract_targets(wrapped), [TARGET])
        self.assertEqual(extract_targets({'type': 'Json', 'data': json.dumps(card())}), [TARGET])

    def test_reserved_app_id_is_sufficient_and_malformed_reserved_is_ignored(self):
        value = card()
        mini = value['meta']['miniapp']
        del mini['source']
        self.assertEqual(extract_targets(value), [TARGET])
        mini['ark_reserved1'] = '{bad json'
        self.assertEqual(extract_targets(value), [])
        mini['ark_reserved1'] = '[' * 5000
        self.assertEqual(extract_targets(value), [])

    def test_pc_wrapper_and_legacy_page_paths(self):
        value = card()
        del value['meta']['miniapp']['jumpUrl']
        self.assertEqual(extract_targets(value), [TARGET])
        for path in ('pages/uni-pb/uni-pb.html?tid=12345678', 'pages/pb/pb?tid=12345678'):
            value['meta']['miniapp']['pagePath'] = path
            self.assertEqual(extract_targets(value), [POST])

    def test_explicit_tieba_identity_required_for_opaque_shares(self):
        value = card()
        value['meta']['miniapp'].update(source='其他应用', ark_reserved1='{}')
        self.assertEqual(extract_targets(value), [])
        self.assertEqual(extract_targets(SHARE), [])
        self.assertEqual(extract_targets({'title': '贴吧', 'jumpUrl': SHARE}), [])
        self.assertEqual(extract_targets({'app': 'com.tencent.miniapp.lua', 'title': '贴吧', 'url': SHARE}), [])

    def test_direct_link_preferred_and_quoted_cards_excluded(self):
        value = card()
        value['meta']['miniapp']['qqdocurl'] = POST['url']
        self.assertEqual(extract_targets(value), [POST])
        for kind in ('reply', 'node', 'forward', 'history'):
            self.assertEqual(extract_targets({'type': kind, 'data': card()}), [])
        self.assertEqual(extract_targets({'history': [card()], 'quote_message': card()}), [])

    def test_only_fixed_https_shortlink_shape(self):
        for url in ('http://' + SHARE[8:], SHARE.replace('m.q.qq.com', 'm.q.qq.com.evil.test'),
                    SHARE.replace('m.q.qq.com', 'u@m.q.qq.com'), SHARE + '#fragment',
                    SHARE.replace('m.q.qq.com', 'm.q.qq.com:444'), SHARE + '/extra',
                    SHARE + '\\x', SHARE + '\n', SHARE.replace('/a/s/', '/other/'),
                    SHARE[:-1], 'file://' + SHARE[8:], 'miniapp://open/1036?url=https://evil.test'):
            with self.subTest(url=url):
                self.assertIsNone(parse_miniapp_share_url(url))
        self.assertEqual(parse_miniapp_share_url(SHARE + '?from=share'), TARGET)
        self.assertIsNone(parse_url(SHARE))


class ResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_request_and_response(self):
        action = AsyncMock(return_value=response())
        result = await m.resolve_target(TARGET, SimpleNamespace(bot=SimpleNamespace(call_action=action)))
        self.assertEqual(result, POST)
        self.assertEqual(action.await_args.args, ('send_packet',))
        kwargs = action.await_args.kwargs
        self.assertEqual(kwargs['cmd'], m.COMMAND)
        self.assertTrue(kwargs['rsp'])
        request = Message.load(bytes.fromhex(kwargs['data']))
        self.assertEqual(request.message(4).text(2), SHARE)
        self.assertEqual(request.message(4).uint(3), 0)

    async def test_regular_links_never_query_qq(self):
        action = AsyncMock()
        self.assertIs(await m.resolve_target(POST, SimpleNamespace(bot=SimpleNamespace(call_action=action))), POST)
        action.assert_not_awaited()

    async def test_bad_target_rejected_before_action(self):
        action = AsyncMock()
        for target in ({**TARGET, 'url': 'https://evil.test'}, {**TARGET, 'id': 'other'}):
            with self.assertRaises(ValueError):
                await m.resolve_target(target, SimpleNamespace(bot=SimpleNamespace(call_action=action)))
        action.assert_not_awaited()

    async def test_unavailable_timeout_and_cancellation(self):
        with self.assertRaisesRegex(ValueError, '接入端'):
            await m.resolve_target(TARGET, None)
        event = SimpleNamespace(bot=SimpleNamespace(call_action=AsyncMock(side_effect=RuntimeError('private secret'))))
        with self.assertRaisesRegex(ValueError, '接入端') as caught:
            await m.resolve_target(TARGET, event)
        self.assertNotIn('secret', str(caught.exception))
        event.bot.call_action = AsyncMock(side_effect=TimeoutError())
        with self.assertRaisesRegex(ValueError, '超时'):
            await m.resolve_target(TARGET, event)
        event.bot.call_action = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await m.resolve_target(TARGET, event)

    async def test_reply_bounds_types_and_identity(self):
        good = bytes.fromhex(response())
        self.assertEqual(m.decode_target((len(good)+4).to_bytes(4, 'big').hex() + good.hex()), POST)
        for value in ('', 'x', 'aa ', None, {}, '00' * (m.MAX_RESPONSE+1), '80',
                      response(appid='other'), response(code=1),
                      response(path='https://evil.test/?tid=12345678'),
                      response(path='pages/uni-pb/uni-pb.html?tid=123&tid=456'),
                      (u(2, 0)+u(2, 1)).hex(), b(2, 'wrong type').hex()):
            with self.subTest(value=str(value)[:60]), self.assertRaises(ValueError):
                m.decode_target(value)
