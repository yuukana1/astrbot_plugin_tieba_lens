"""在真实 AstrBot SDK 中执行，消息仅由内存接收器记录，不发送到任何群。"""
import asyncio
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_tieba_lens import main
from astrbot_plugin_tieba_lens.settings import DEFAULTS
from astrbot_plugin_tieba_lens.links import parse_url
import astrbot.api.message_components as C

DOC = {"kind": "thread", "id": "12345678", "title": "完整帖子", "url": "https://tieba.baidu.com/p/12345678",
    "forum": "测试", "description": "", "author": {"id": "1", "name": "楼主", "avatar": "", "level": 12},
    "stats": {}, "posts": [{"id": "1", "floor": 1, "author": {"name": "楼主", "level": 12},
    "segments": [{"type": "text", "text": "完整正文"}], "comments": [], "time": ""}], "threads": [], "warnings": []}


class Config(dict):
    def save_config(self):
        pass


class FakeContext:
    def __init__(self):
        self.routes = []

    def register_web_api(self, *args):
        self.routes.append(args)


class Receiver:
    def __init__(self, messages=None):
        self.message_obj = SimpleNamespace(message_id="100001")
        self.message_str = "https://tieba.baidu.com/p/12345678"
        self.unified_msg_origin = "aiocqhttp:GroupMessage:12345"
        self.sent = []
        self.stopped = False
        self.messages = messages if messages is not None else [C.Plain(self.message_str)]
        self.bot = SimpleNamespace(call_action=AsyncMock())

    def get_group_id(self): return "12345"
    def get_self_id(self): return "987654321"
    def get_platform_name(self): return "aiocqhttp"
    def get_messages(self): return self.messages
    def chain_result(self, chain): return chain
    def plain_result(self, text): return text
    def stop_event(self): self.stopped = True
    async def send(self, data): self.sent.append(data)


class FakeRenderer:
    def __init__(self, root, pages=1):
        self.root = root
        self.pages = pages

    async def render(self, document, options, job_id):
        folder = self.root / "artifacts" / job_id
        folder.mkdir(parents=True, exist_ok=True)
        result = []
        for i in range(self.pages):
            p = folder / f"page-{i}.png"
            p.write_bytes(b"render-result")
            result.append(str(p))
        return result


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_theme_font_and_long_image_options_are_valid_settings(self):
        for label, theme in (("达妮娅", "denia"), ("初音", "miku"), ("B站", "bilibili")):
            text, patch = main.TiebaLens._parse_args(DOC["url"] + f" --主题 {label} --字体 文楷 --长图 --热门")
            self.assertEqual(text, DOC["url"])
            self.assertEqual(patch, {"theme": theme, "font_override": "lxgw", "single_image": True, "sort": "hot"})
            self.assertEqual(main.validate_patch(patch), patch)
        self.assertEqual(main.TiebaLens._parse_args(DOC["url"] + " --分页 --字体 自定义")[1],
                         {"single_image": False, "font_override": "custom"})

    async def test_font_routes_apply_reset_and_preserve_saved_font_on_config_error(self):
        from fontTools.fontBuilder import FontBuilder
        from fontTools.pens.ttGlyphPen import TTGlyphPen
        import io
        import base64
        fb = FontBuilder(1000,isTTF=True)
        fb.setupGlyphOrder(['.notdef','space','A'])
        fb.setupCharacterMap({32:'space',65:'A'})
        glyphs={}
        for name in ['.notdef','space','A']:
            pen=TTGlyphPen(None)
            if name=='A':
                pen.moveTo((0,0));pen.lineTo((250,700));pen.lineTo((500,0));pen.closePath()
            glyphs[name]=pen.glyph()
        fb.setupGlyf(glyphs)
        fb.setupHorizontalMetrics({n:(600,0) for n in glyphs})
        fb.setupHorizontalHeader(ascent=800,descent=-200)
        fb.setupNameTable({'familyName':'Sample Font','styleName':'Regular','uniqueFontIdentifier':'sample','fullName':'Sample Font','psName':'SampleFont'})
        fb.setupOS2(sTypoAscender=800,sTypoDescender=-200,usWinAscent=800,usWinDescent=200)
        fb.setupPost();fb.setupMaxp()
        output=io.BytesIO();fb.save(output)
        raw=output.getvalue()
        payload={'name':'sample.ttf','data':base64.b64encode(raw).decode(),'offset':0,'total_size':len(raw)}
        async def request_for(payload):
            return json.dumps(payload).encode()
        response=SimpleNamespace(method='POST',headers={},body=AsyncMock(return_value=await request_for(payload)))
        with patch.object(main,'request',response):
            result=await self.plugin.web_font()
        self.assertEqual(self.plugin.config['font_override'],'custom')
        self.assertTrue(self.plugin.fonts.info()['uploaded'])
        saved=self.plugin.fonts.load()
        payload['name']='replacement.ttf'
        response.body=AsyncMock(return_value=await request_for(payload))
        with patch.object(main,'request',response),patch.object(self.plugin,'_update_settings',new=AsyncMock(side_effect=OSError('cannot save'))):
            with self.assertRaises(OSError):
                await self.plugin.web_font()
        self.assertEqual(self.plugin.fonts.load(),saved)
        await self.plugin.web_font_reset()
        self.assertEqual(self.plugin.config['font_override'],'lxgw')
        self.assertFalse(self.plugin.fonts.info()['uploaded'])

    def test_markdown_distinguishes_inferred_and_missing_floor_numbers(self):
        doc = copy.deepcopy(DOC)
        doc["posts"].append({"floor": 7, "floor_inferred": True, "author": {"name": "回复者"},
                             "segments": [], "comments": [{"floor": value, "author": {"name": f"子评论{index}"}, "segments": []}
                                                         for index, value in enumerate((None, 0))]})
        text = main.markdown(doc)
        self.assertIn("### 1楼 · 楼主", text)
        self.assertIn("### 展示序号 7 · 回复者", text)
        self.assertIn("#### 楼中楼 · 子评论0", text)
        self.assertIn("#### 楼中楼 · 子评论1", text)
        self.assertNotIn("None楼", text)
        self.assertNotIn("7楼", text)
        self.assertNotIn("0楼", text)

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path_patch = patch.object(main, "get_astrbot_plugin_data_path", return_value=str(self.root))
        self.path_patch.start()
        self.plugin = main.TiebaLens(FakeContext(), Config(copy.deepcopy(DEFAULTS)))
        self.plugin.renderer = FakeRenderer(self.plugin.data_root)
        self.plugin.render_manager.is_ready = AsyncMock(return_value=True)
        self.client = SimpleNamespace(parse=AsyncMock(return_value=copy.deepcopy(DOC)), close=AsyncMock(), diagnostics=lambda: {})
        self.client_patch = patch.object(main, "TiebaClient", return_value=self.client)
        self.client_patch.start()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.path_patch.stop()
        self.client_patch.stop()
        self.tmp.cleanup()

    async def finish(self, job):
        task = self.plugin.tasks.get(job["id"])
        if task:
            await task
        return await self.plugin._get_job(job["id"])

    async def test_card_reactions_and_cache(self):
        receiver = Receiver()
        job = await self.plugin.submit(parse_url(DOC["url"]), self.plugin.settings(), event=receiver)
        result = await self.finish(job)
        self.assertEqual(result["status"], "done")
        self.assertEqual(len(receiver.sent), 1)
        self.assertIsInstance(receiver.sent[0][0], C.Image)
        ids = [call.kwargs["emoji_id"] for call in receiver.bot.call_action.await_args_list if call.kwargs["set"]]
        self.assertIn("289", ids)
        self.assertIn("124", ids)
        other = await self.plugin.submit(parse_url(DOC["url"]), {**self.plugin.settings(), "theme": "midnight"})
        self.assertTrue((await self.finish(other))["cache_hit"])
        self.assertEqual(self.client.parse.await_count, 1)

    async def test_miniapp_automatic_parse_shares_canonical_cache(self):
        from astrbot_plugin_tieba_lens.tests.test_miniapp import card, response, POST
        receiver = Receiver([C.Json(data=card())])
        receiver.message_str = ''
        receiver.bot.call_action.return_value = response()
        await self.plugin.on_message(receiver)
        self.assertTrue(receiver.stopped)
        tasks = list(self.plugin.tasks.values())
        self.assertEqual(len(tasks), 1)
        await asyncio.gather(*tasks)
        result = list(self.plugin.jobs.values())[0]
        self.assertEqual(result['status'], 'done')
        self.assertEqual(result['source_url'], POST['url'])
        self.assertEqual(self.client.parse.await_args.args[0], POST)
        direct = await self.finish(await self.plugin.submit(POST, self.plugin.settings()))
        self.assertTrue(direct['cache_hit'])
        self.assertEqual(self.client.parse.await_count, 1)
        self.assertTrue(receiver.sent)

    async def test_failed_miniapp_query_allows_retry_and_never_parses(self):
        from astrbot_plugin_tieba_lens.tests.test_miniapp import card, TARGET
        receiver = Receiver([C.Json(data=card())])
        receiver.message_str = ''
        receiver.bot.call_action.side_effect = RuntimeError('unsupported')
        await self.plugin.on_message(receiver)
        await asyncio.gather(*list(self.plugin.tasks.values()))
        result = list(self.plugin.jobs.values())[0]
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn(receiver.unified_msg_origin + '|' + TARGET['url'], self.plugin.recent)
        self.client.parse.assert_not_awaited()
        self.assertEqual(self.plugin.store.cache_info()['entries'], 0)

    async def test_disabled_or_quoted_miniapp_performs_no_query(self):
        from astrbot_plugin_tieba_lens.tests.test_miniapp import card
        receiver = Receiver([C.Json(data=card())])
        self.plugin.config['blacklist'] = [receiver.get_group_id()]
        await self.plugin.on_message(receiver)
        self.assertFalse(self.plugin.tasks)
        receiver.bot.call_action.assert_not_awaited()
        self.plugin.config['blacklist'] = []
        receiver.messages = [C.Reply(id='123', chain=[C.Json(data=card())])]
        await self.plugin.on_message(receiver)
        self.assertFalse(self.plugin.tasks)
        receiver.bot.call_action.assert_not_awaited()

    async def test_old_hot_cache_is_sorted_for_both_render_and_export(self):
        settings = {**self.plugin.settings(), "sort": "hot"}
        target = parse_url(DOC["url"])
        doc = copy.deepcopy(DOC)
        doc["posts"] += [{"id": str(floor), "floor": floor, "likes": likes, "likes_available": True,
                         "author": {"name": str(floor)}, "segments": [{"type": "text", "text": str(floor)}], "comments": []}
                        for floor, likes in ((2, 1), (8, 30), (5, 30))]
        self.plugin.store.put_cache(main.cache_key(target, settings), doc, 30)
        received = []
        render = self.plugin.renderer.render
        async def capture(document, options, job_id):
            received.extend(post["id"] for post in document["posts"])
            return await render(document, options, job_id)
        self.plugin.renderer.render = capture
        result = await self.finish(await self.plugin.submit(target, settings))
        self.assertEqual(result["status"], "done")
        self.assertTrue(result["cache_hit"])
        self.assertEqual(received, ["1", "5", "8", "2"])
        exported = json.loads(self.plugin._export_bytes("json", 0, result))
        self.assertEqual([p["id"] for p in exported["posts"]], received)
        self.assertEqual([p["id"] for p in self.plugin.store.get_cache(main.cache_key(target, settings))["posts"]], ["1", "2", "8", "5"])
        self.client.parse.assert_not_awaited()

    async def test_forward_uses_real_nodes_and_local_file_paths(self):
        self.plugin.renderer.pages = 2
        receiver = Receiver()
        job = await self.plugin.submit(parse_url(DOC["url"]), self.plugin.settings(), event=receiver)
        self.assertEqual((await self.finish(job))["status"], "done")
        nodes = receiver.sent[0][0]
        self.assertIsInstance(nodes, C.Nodes)
        self.assertEqual(len(nodes.nodes), 3)
        self.assertIsInstance(nodes.nodes[1].content[0], C.Image)
        self.assertEqual(nodes.nodes[0].uin, "987654321")

    async def test_image_results_keep_warnings_in_job_without_text_messages_or_forward_nodes(self):
        warning = "部分回复接口未提供点赞数；有 58 条楼中楼未展开。"
        self.client.parse.return_value = {**copy.deepcopy(DOC), "warnings": [warning]}
        self.plugin.renderer.pages = 2
        for mode, platform in (("card", "aiocqhttp"), ("auto", "aiocqhttp"),
                               ("forward", "aiocqhttp"), ("forward", "other")):
            for show_source in (True, False):
                with self.subTest(mode=mode, platform=platform, show_source=show_source):
                    receiver = Receiver()
                    receiver.get_platform_name = lambda: platform
                    options = {**self.plugin.settings(), "output_mode": mode, "show_source": show_source}
                    result = await self.finish(await self.plugin.submit(parse_url(DOC["url"]), options, event=receiver))
                    self.assertEqual(result["status"], "done")
                    self.assertIn(warning, result["warnings"])
                    self.assertIn(warning, self.plugin._export_bytes("json", 0, result).decode())
                    if platform == "aiocqhttp" and mode in {"auto", "forward"}:
                        self.assertEqual(len(receiver.sent), 1)
                        nodes = receiver.sent[0][0].nodes
                        self.assertEqual(len(nodes), 3)
                        self.assertEqual([node.name for node in nodes], ["贴吧拾光", "贴吧拾光 · 1/2", "贴吧拾光 · 2/2"])
                    else:
                        self.assertEqual(len(receiver.sent), 2)
                        self.assertTrue(all(isinstance(chain[0], C.Image) for chain in receiver.sent))

    async def test_source_setting_applies_to_new_parse_using_cached_content(self):
        calls = []
        render = self.plugin.renderer.render
        async def capture(document, options, job_id):
            calls.append(options["show_source"])
            return await render(document, options, job_id)
        self.plugin.renderer.render = capture
        first = await self.finish(await self.plugin.submit(parse_url(DOC["url"]), self.plugin.settings()))
        saved = await self.plugin._update_settings({"show_source": False})
        self.assertFalse(saved["show_source"])
        second = await self.finish(await self.plugin.submit(parse_url(DOC["url"]), self.plugin.settings()))
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(calls, [True, False])
        self.assertEqual(self.client.parse.await_count, 1)

    async def test_blacklist_is_silent_and_higher_priority_than_override(self):
        self.plugin.config.update(blacklist=["12345"], group_overrides={"12345": {"enabled": True}})
        receiver = Receiver()
        await self.plugin.on_message(receiver)
        self.assertFalse(receiver.sent)
        self.assertFalse(self.plugin.tasks)

    async def test_missing_renderer_guides_to_panel_without_fetch_or_download(self):
        self.plugin.render_manager.is_ready.return_value = False
        self.plugin.render_manager.endpoint = lambda: ''
        receiver = Receiver()
        await self.plugin.on_message(receiver)
        self.assertEqual(len(receiver.sent), 1)
        self.assertIn('未配置可用的渲染服务', receiver.sent[0])
        self.assertIn('渲染管理', receiver.sent[0])
        self.assertFalse(self.plugin.tasks)
        self.client.parse.assert_not_awaited()

    async def test_broken_browser_keeps_specific_panel_guidance(self):
        message = '本地浏览器暂不可用，请前往贴吧控制台 → 渲染管理重新准备浏览器。'
        self.plugin.renderer.render = AsyncMock(side_effect=main.RendererUnavailable(message))
        receiver = Receiver()
        result = await self.finish(await self.plugin.submit(parse_url(DOC['url']), self.plugin.settings(), event=receiver))
        self.assertEqual(result['stage'], '渲染环境未就绪')
        self.assertEqual(receiver.sent[-1], message)

    async def test_auto_recognition_and_duplicate_suppression(self):
        receiver = Receiver()
        await self.plugin.on_message(receiver)
        self.assertTrue(receiver.stopped)
        tasks = list(self.plugin.tasks.values())
        await asyncio.gather(*tasks)
        await self.plugin.on_message(Receiver())
        self.assertEqual(self.client.parse.await_count, 1)

    async def test_concurrent_duplicate_admission_and_long_running_guard(self):
        release = asyncio.Event()
        entered = asyncio.Event()
        original = self.plugin.renderer.render
        async def slow_render(*args):
            entered.set()
            await release.wait()
            return await original(*args)
        self.plugin.renderer.render = slow_render
        self.plugin.config['cooldown_seconds'] = 0
        await asyncio.gather(self.plugin.on_message(Receiver()), self.plugin.on_message(Receiver()))
        await asyncio.wait_for(entered.wait(), 2)
        self.assertEqual(len(self.plugin.tasks), 1)
        key = Receiver().unified_msg_origin + '|' + DOC['url']
        self.plugin.recent[key] = -10000
        await self.plugin.on_message(Receiver())
        self.assertEqual(len(self.plugin.tasks), 1)
        release.set()
        await asyncio.gather(*list(self.plugin.tasks.values()))
        self.assertFalse(self.plugin.pending_links)
        self.assertGreater(self.plugin.recent[key], 0)

    async def test_full_render_pipeline_is_serial_across_distinct_jobs(self):
        active, peak = 0, 0
        original = self.plugin.renderer.render
        async def render(*args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.03)
            try:
                return await original(*args)
            finally:
                active -= 1
        self.plugin.renderer.render = render
        jobs = [await self.plugin.submit(parse_url(f'https://tieba.baidu.com/p/{value}'), self.plugin.settings())
                for value in ('12345678', '99999999')]
        results = await asyncio.gather(*(self.finish(job) for job in jobs))
        self.assertTrue(all(job['status'] == 'done' for job in results))
        self.assertEqual(peak, 1)

    async def test_success_counter_failure_cannot_reclassify_confirmed_delivery(self):
        original = self.plugin.store.increment
        def increment(name):
            if name == 'success':
                raise OSError('database is locked')
            return original(name)
        receiver = Receiver()
        with patch.object(self.plugin.store, 'increment', side_effect=increment):
            result = await self.finish(await self.plugin.submit(parse_url(DOC['url']), self.plugin.settings(), event=receiver))
        self.assertEqual(result['status'], 'done')
        self.assertEqual(result['delivery_status'], 'sent')
        self.assertEqual(len(receiver.sent), 1)
        self.assertTrue(self.plugin.recent)

    async def test_checkpoint_failure_does_not_resend_or_abort_remaining_pages(self):
        self.plugin.renderer.pages = 2
        original = self.plugin._save
        async def save(job):
            if job.get('status') == 'sending' and job.get('sent_messages'):
                raise OSError('database is locked')
            return await original(job)
        receiver = Receiver()
        with patch.object(self.plugin, '_save', side_effect=save):
            result = await self.finish(await self.plugin.submit(parse_url(DOC['url']),
                {**self.plugin.settings(), 'output_mode': 'card'}, event=receiver))
        self.assertEqual(result['status'], 'done')
        self.assertEqual(result['sent_messages'], 2)
        self.assertEqual(len(receiver.sent), 2)

    async def test_permanent_upload_error_releases_zero_send_guard_without_retry(self):
        from aiocqhttp.exceptions import ActionFailed
        receiver = Receiver()
        receiver.send = AsyncMock(side_effect=[ActionFailed({'retcode':100,'wording':'图片上传失败: file not found'}), None])
        result = await self.finish(await self.plugin.submit(parse_url(DOC['url']), self.plugin.settings(), event=receiver))
        self.assertEqual(result['status'], 'delivery_failed')
        self.assertEqual(result['delivery_status'], 'failed')
        self.assertFalse(self.plugin.recent)
        self.assertEqual(receiver.send.await_count, 2)
        self.assertTrue(result['result_ready'])

    async def test_uncertain_delivery_guard_is_restored_after_reload(self):
        receiver = Receiver()
        receiver.send = AsyncMock(side_effect=[TimeoutError('API timeout'), None])
        await self.finish(await self.plugin.submit(parse_url(DOC['url']), self.plugin.settings(), event=receiver))
        self.plugin.recent.clear()
        await self.plugin._restore_delivery_guards()
        self.assertIn(receiver.unified_msg_origin + '|' + DOC['url'], self.plugin.recent)

    async def test_optional_cache_failures_do_not_fail_a_parsed_document(self):
        with patch.object(self.plugin.store, 'get_cache', side_effect=OSError('database is locked')), \
                patch.object(self.plugin.store, 'put_cache', side_effect=OSError('disk full')):
            result = await self.finish(await self.plugin.submit(parse_url(DOC['url']), self.plugin.settings()))
        self.assertEqual(result['status'], 'done')
        self.assertTrue(result['result_ready'])

    async def test_recover_interrupted_send_restores_guard_with_fresh_expiry(self):
        receiver = Receiver()
        result = await self.finish(await self.plugin.submit(parse_url(DOC['url']), self.plugin.settings(), event=receiver))
        result.update(status='sending', delivery_status='sending', dedup_until=0)
        self.plugin.store.save_job(result)
        self.plugin.store.recover()
        self.plugin.recent.clear()
        await self.plugin._restore_delivery_guards()
        self.assertIn(receiver.unified_msg_origin + '|' + DOC['url'], self.plugin.recent)

    async def test_cancel_after_confirmed_send_keeps_done_and_guard(self):
        original = self.plugin._react
        async def reaction(event, settings, emoji, add):
            if emoji == settings['reaction_done'] and add:
                raise asyncio.CancelledError()
            return await original(event, settings, emoji, add)
        receiver = Receiver()
        with patch.object(self.plugin, '_react', side_effect=reaction):
            job = await self.plugin.submit(parse_url(DOC['url']), self.plugin.settings(), event=receiver)
            task = self.plugin.tasks[job['id']]
            with self.assertRaises(asyncio.CancelledError):
                await task
        result = self.plugin.jobs[job['id']]
        self.assertEqual(result['status'], 'done')
        self.assertEqual(result['delivery_status'], 'sent')
        self.assertTrue(self.plugin.recent)

    async def test_failure_is_visible_and_does_not_poison_cache(self):
        self.client.parse.side_effect = RuntimeError("读取失败")
        receiver = Receiver()
        job = await self.plugin.submit(parse_url(DOC["url"]), self.plugin.settings(), event=receiver)
        self.plugin.recent[receiver.unified_msg_origin + "|" + DOC["url"]] = 123
        result = await self.finish(job)
        self.assertEqual(result["status"], "failed")
        self.assertIn("读取失败", receiver.sent[-1])
        self.assertFalse(self.plugin.recent)
        self.assertEqual(self.plugin.store.cache_info()["entries"], 0)

    async def test_upload_retry_only_repeats_failed_page_for_both_onebot_backends(self):
        from aiocqhttp.exceptions import ActionFailed
        for wording in ("highway upload transport failed after 3 attempts: TCP connect timeout",
                        "图片上传失败: Error: connect ETIMEDOUT"):
            with self.subTest(wording=wording):
                receiver = Receiver()
                self.plugin.renderer.pages = 2
                calls = []
                async def send(chain):
                    calls.append(chain)
                    if len(calls) == 2:
                        raise ActionFailed(result={"status": "failed", "retcode": 100, "wording": wording})
                    receiver.sent.append(chain)
                receiver.send = send
                retry = main.send_with_retry
                async def fast_retry(send):
                    return await retry(send, sleep=AsyncMock())
                with patch.object(main, "send_with_retry", new=fast_retry):
                    result = await self.finish(await self.plugin.submit(parse_url(DOC["url"]),
                        {**self.plugin.settings(), "output_mode": "card"}, event=receiver))
                self.assertEqual(result["status"], "done")
                self.assertEqual(result["sent_messages"], 2)
                self.assertEqual(len(calls), 3)
                self.assertEqual(len(receiver.sent), 2)
                self.assertEqual(calls[1][0].file, calls[2][0].file)
                self.assertIsNot(calls[1][0], calls[2][0])
                self.assertNotEqual(calls[0][0].file, calls[1][0].file)

    async def test_exhausted_upload_keeps_artifacts_previewable_and_releases_zero_send_dedup(self):
        from aiocqhttp.exceptions import ActionFailed
        receiver = Receiver()
        calls = []
        async def send(chain):
            calls.append(chain)
            if not isinstance(chain, str):
                raise ActionFailed(result={"status": "failed", "retcode": 100,
                                          "wording": "highway upload transport failed: TCP connect timeout"})
            receiver.sent.append(chain)
        receiver.send = send
        retry = main.send_with_retry
        async def fast_retry(send):
            return await retry(send, sleep=AsyncMock())
        self.plugin.recent[receiver.unified_msg_origin + "|" + DOC["url"]] = 123
        with patch.object(main, "send_with_retry", new=fast_retry):
            result = await self.finish(await self.plugin.submit(parse_url(DOC["url"]), self.plugin.settings(), event=receiver))
        self.assertEqual(result["status"], "delivery_failed")
        self.assertTrue(result["result_ready"])
        self.assertEqual(result["delivery_status"], "failed")
        self.assertFalse(self.plugin.recent)
        self.assertEqual(len(calls), 4)
        self.assertIn("已解析并生成卡片", receiver.sent[-1])
        self.assertNotIn("贴吧解析失败", receiver.sent[-1])
        self.assertEqual(self.plugin._export_bytes("png", 0, result), b"render-result")
        with patch.object(main, "request", SimpleNamespace(query={"id": result["id"]})):
            self.assertEqual((await self.plugin._artifact())["id"], result["id"])
        self.plugin.store.recover()
        self.assertEqual(self.plugin.store.get_job(result["id"])["status"], "delivery_failed")

    async def test_ambiguous_send_failure_keeps_dedup_and_does_not_retry(self):
        receiver = Receiver()
        receiver.send = AsyncMock(side_effect=[TimeoutError("API response timeout"), None])
        self.plugin.recent[receiver.unified_msg_origin + "|" + DOC["url"]] = asyncio.get_running_loop().time()
        result = await self.finish(await self.plugin.submit(parse_url(DOC["url"]), self.plugin.settings(), event=receiver))
        self.assertEqual(result["status"], "delivery_failed")
        self.assertEqual(result["delivery_status"], "unknown")
        self.assertTrue(self.plugin.recent)
        self.assertEqual(receiver.send.await_count, 2)

    async def test_recover_interrupted_send_preserves_result_without_resending(self):
        result = await self.finish(await self.plugin.submit(parse_url(DOC["url"]), self.plugin.settings()))
        result.update(status="sending", delivery_status="sending", sent_messages=1)
        self.plugin.store.save_job(result)
        self.plugin.store.recover()
        recovered = self.plugin.store.get_job(result["id"])
        self.assertEqual(recovered["status"], "delivery_failed")
        self.assertEqual(recovered["delivery_status"], "unknown")
        self.assertTrue(recovered["result_ready"])
        self.assertEqual(recovered["sent_messages"], 1)

    async def test_config_validation_preserves_previous_values(self):
        before = dict(self.plugin.config)
        with self.assertRaises(ValueError):
            await self.plugin._update_settings({"comment_limit": 999})
        self.assertEqual(before, dict(self.plugin.config))
        public = await self.plugin._update_settings({"cookie": "BDUSS=secret", "comment_limit": 20})
        self.assertTrue(public["cookie_set"])
        self.assertNotIn("secret", json.dumps(public))

    async def test_source_hidden_in_forward_and_expired_result_not_in_memory(self):
        self.plugin.renderer.pages = 2
        receiver = Receiver()
        job = await self.plugin.submit(parse_url(DOC["url"]), {**self.plugin.settings(), "show_source": False}, event=receiver)
        result = await self.finish(job)
        first = receiver.sent[0][0].nodes[0].content[0].text
        self.assertNotIn("https://", first)
        await asyncio.sleep(0)
        self.plugin.store.delete_job(job["id"])
        self.assertIsNone(await self.plugin._get_job(job["id"]))

    async def test_download_snapshot_survives_history_clear(self):
        job = await self.plugin.submit(parse_url(DOC["url"]), self.plugin.settings())
        result = await self.finish(job)
        raw = self.plugin._export_bytes("zip", 0, result)
        self.plugin.store.clear_history(set())
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
            self.assertIn("原帖.md", bundle.namelist())
            self.assertEqual(bundle.read("page-0.png"), b"render-result")

    async def test_original_image_toggle_places_unchanged_image_after_card(self):
        folder = self.plugin.data_root / "originals-fixture"
        folder.mkdir()
        original = folder / "原图.gif"
        original.write_bytes(b"GIF-original-unchanged")
        receiver = Receiver()
        saved = [{"path": str(original), "label": "1楼 · 原图1", "url": "https://tiebapic.baidu.com/test.gif"}]
        with patch.object(main, "download_originals", new=AsyncMock(return_value=(saved, []))) as download:
            job = await self.plugin.submit(parse_url(DOC["url"]),
                {**self.plugin.settings(), "forward_original_images": True, "output_mode": "card"}, event=receiver)
            result = await self.finish(job)
            self.assertEqual(result["original_count"], 1)
            self.assertEqual(download.await_count, 1)
        nodes = receiver.sent[0][0]
        self.assertIsInstance(nodes, C.Nodes)
        self.assertEqual(len(nodes.nodes), 3)
        self.assertEqual(nodes.nodes[2].name, "1楼 · 原图1")
        self.assertEqual(original.read_bytes(), b"GIF-original-unchanged")
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(self.plugin._export_bytes("zip", 0, result))) as bundle:
            self.assertEqual(bundle.read("原图/原图.gif"), b"GIF-original-unchanged")

    async def test_original_images_off_does_not_download_or_add_nodes(self):
        receiver = Receiver()
        with patch.object(main, "download_originals", new=AsyncMock()) as download:
            job = await self.plugin.submit(parse_url(DOC["url"]), self.plugin.settings(), event=receiver)
            await self.finish(job)
            self.assertEqual(download.await_count, 0)
        self.assertIsInstance(receiver.sent[0][0], C.Image)

    def test_command_options_keep_all_input_words(self):
        text, patch = self.plugin._parse_args(DOC["url"] + " --评论 20 --楼中楼 4 --记录 --主题 夜")
        self.assertEqual(text, DOC["url"])
        self.assertEqual(patch, {"comment_limit": 20, "subcomment_limit": 4, "output_mode": "forward", "theme": "midnight"})


if __name__ == "__main__":
    unittest.main()
