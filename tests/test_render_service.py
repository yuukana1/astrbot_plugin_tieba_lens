"""Renderer selection and preparation without real services or downloads."""
import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT=Path(__file__).resolve().parents[1]
PACKAGE='_tieba_render_service_tests'
package=types.ModuleType(PACKAGE);package.__path__=[str(ROOT)];sys.modules[PACKAGE]=package
RenderManager=importlib.import_module(PACKAGE+'.render_service').RenderManager

class RenderSelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings={'render_backend':'auto','browser_engine':'headless'}
        self.endpoint=''
        self.local=types.SimpleNamespace(render=AsyncMock(return_value='local.png'),prepare=AsyncMock(),close=AsyncMock(),
            progress=lambda:'正在下载',status=lambda:{'available':True,'browser':'精简 Chromium','engines':[
                {'id':'headless','installed':True},{'id':'chromium','installed':True}]})
        self.service=AsyncMock(return_value='service.png')
        self.manager=RenderManager(self.local,self.service,lambda:self.settings,lambda:self.endpoint)

    async def asyncTearDown(self):
        await self.manager.close()

    async def test_auto_uses_existing_service_without_starting_local_browser(self):
        self.endpoint='http://renderer.invalid'
        with patch.object(self.manager,'_service_available',new=AsyncMock(return_value=True)):
            self.assertEqual(await self.manager.render('html',{},{}),'service.png')
        self.local.render.assert_not_awaited()

    async def test_auto_without_service_uses_selected_local_engine(self):
        self.settings['browser_engine']='chromium'
        self.assertEqual(await self.manager.render('html',{},{}),'local.png')
        self.service.assert_not_awaited()
        self.assertEqual(self.local.render.await_args.args[2]['browser_engine'],'chromium')

    async def test_auto_service_failure_falls_back_but_size_limit_stays_visible(self):
        with patch.object(self.manager,'_service_available',new=AsyncMock(return_value=True)):
            self.service.side_effect=TimeoutError('service unavailable')
            self.assertEqual(await self.manager.render('html',{},{}),'local.png')
            self.service.side_effect=RuntimeError('screenshot is too large')
            with self.assertRaisesRegex(RuntimeError,'too large'):
                await self.manager.render('html',{}, {})
        self.assertEqual(self.local.render.await_count,1)

    async def test_explicit_service_does_not_silently_switch(self):
        self.settings['render_backend']='service'
        self.service.side_effect=TimeoutError('unavailable')
        with self.assertRaises(TimeoutError):await self.manager.render('html',{}, {})
        self.local.render.assert_not_awaited()

    async def test_cancelled_render_is_not_retried_elsewhere(self):
        self.settings['render_backend']='service'
        self.service.side_effect=__import__('asyncio').CancelledError()
        with self.assertRaises(__import__('asyncio').CancelledError):await self.manager.render('html',{}, {})
        self.local.render.assert_not_awaited()

    async def test_missing_environment_never_triggers_automatic_install(self):
        self.local.status=lambda:{'available':False,'browser':'','engines':[{'id':'headless','installed':False}]}
        self.assertEqual((await self.manager.status())['active'],'pending')
        self.assertFalse(await self.manager.is_ready())
        with self.assertRaisesRegex(RuntimeError,'未配置可用的渲染服务'):
            await self.manager.render('html',{}, {})
        self.local.render.assert_not_awaited()
        self.local.prepare.assert_not_awaited()

    async def test_auto_reuses_another_installed_engine_without_downloading(self):
        self.local.status=lambda:{'available':True,'browser':'完整 Chromium','engines':[
            {'id':'headless','installed':False},{'id':'chromium','installed':True}]}
        await self.manager.render('html',{}, {})
        self.assertEqual(self.local.render.await_args.args[2]['browser_engine'],'chromium')

    async def test_prepare_progress_success_and_failure_are_reported(self):
        self.manager.prepare('headless')
        self.assertEqual(self.manager.install['status'],'running')
        self.assertEqual((await self.manager.status())['install']['stage'],'正在下载')
        await self.manager.install_task
        self.assertEqual(self.manager.install['status'],'done')
        self.local.prepare.side_effect=RuntimeError('download failed')
        self.manager.prepare('chromium');await self.manager.install_task
        self.assertEqual(self.manager.install['status'],'failed')
        self.assertIn('download failed',self.manager.install['error'])
        with self.assertRaises(ValueError):self.manager.prepare('invalid')

    async def test_invalid_engine_types_are_validation_errors(self):
        for engine in ([], {}, None, 3):
            with self.subTest(engine=engine), self.assertRaises(ValueError):
                self.manager.prepare(engine)

    async def test_detected_but_broken_browser_directs_users_to_prepare(self):
        for message in ("Executable doesn't exist at /cache/chrome", "error while loading shared libraries: libnss3.so"):
            self.local.render.side_effect = RuntimeError(message)
            with self.subTest(message=message), self.assertRaisesRegex(RuntimeError, '渲染管理重新准备浏览器'):
                await self.manager.render('html', {}, {})
        self.local.prepare.assert_not_awaited()
