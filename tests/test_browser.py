"""Local Chromium lifecycle contracts; mocks never install or contact a server.

Set TIEBA_BROWSER_REAL_TESTS=1 in the isolated container to add a real Chromium
screenshot check. It still blocks every page HTTP(S) request.
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("tieba_local_browser_tests", ROOT / "browser.py")
browser_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(browser_module)


class FakePage:
    def __init__(self):
        self.set_default_timeout = Mock()
        self.set_content = AsyncMock()
        self.add_style_tag = AsyncMock()
        self.evaluate = AsyncMock(return_value={"width": 960, "height": 500})
        self.screenshot = AsyncMock(side_effect=self.write_screenshot)

    async def write_screenshot(self, **options):
        Path(options["path"]).write_bytes(b"local-png")


class FakeContext:
    def __init__(self):
        self.page = FakePage()
        self.route = AsyncMock()
        self.new_page = AsyncMock(return_value=self.page)
        self.close = AsyncMock()


class FakeBrowser:
    def __init__(self):
        self.contexts = []
        self.is_connected = Mock(return_value=True)
        self.new_context = AsyncMock(side_effect=self.make_context)
        self.close = AsyncMock()

    async def make_context(self, **options):
        context = FakeContext()
        self.contexts.append(context)
        return context


class LocalRendererTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.renderer = browser_module.LocalRenderer(self.root)
        self.browser = FakeBrowser()
        self.playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=self.browser)), stop=AsyncMock())
        self.renderer._start_playwright = AsyncMock(return_value=self.playwright)
        self.renderer._render_html = Mock(return_value="<html><body>离线卡片</body></html>")
        self.renderer._find_executable = Mock(return_value=None)
        self.which = patch.object(browser_module.shutil, "which", return_value=None)
        self.which.start()
        self.addCleanup(self.which.stop)
        self.system_browser = patch.object(self.renderer, "_system_browser", return_value=None)
        self.system_browser.start()
        self.addCleanup(self.system_browser.stop)

    async def asyncTearDown(self):
        await self.renderer.close()

    async def render(self, **options):
        return await self.renderer.render("{{ text }}", {"text": "卡片", "width": 960}, options)

    async def test_reuses_one_browser_but_closes_each_isolated_context(self):
        first, second = await self.render(), await self.render()
        self.assertNotEqual(first, second)
        self.assertEqual(Path(first).parent, self.root / "render_tmp")
        self.assertRegex(Path(first).name, r"^[0-9a-f]{32}\.png$")
        self.assertTrue(Path(first).is_file())
        self.renderer._start_playwright.assert_awaited_once_with()
        self.playwright.chromium.launch.assert_awaited_once()
        self.assertEqual(len(self.browser.contexts), 2)
        for context in self.browser.contexts:
            context.close.assert_awaited_once_with()
        self.assertFalse(self.renderer._contexts)
        self.browser.close.assert_not_awaited()

    async def test_blocks_http_https_disables_page_javascript_and_preserves_png_contract(self):
        await self.render(viewport_width=800, timeout=10000)
        options = self.browser.new_context.call_args.kwargs
        self.assertEqual(options["viewport"], {"width": 800, "height": 1})
        self.assertEqual(options["device_scale_factor"], 1)
        self.assertFalse(options["java_script_enabled"])
        self.assertEqual(options["service_workers"], "block")
        self.assertTrue(options["offline"])
        self.assertFalse(options["accept_downloads"])
        context = self.browser.contexts[0]
        pattern, handler = context.route.call_args.args
        self.assertTrue(pattern.match("http://example.invalid/image.png"))
        self.assertTrue(pattern.match("HTTPS://example.invalid/font.woff2"))
        self.assertFalse(pattern.match("data:image/png;base64,eA=="))
        route = SimpleNamespace(abort=AsyncMock())
        await handler(route)
        route.abort.assert_awaited_once_with()
        html = context.page.set_content.call_args.args[0]
        self.assertIn("<html><body>离线卡片</body></html>", html)
        self.assertIn("animation:none!important", html)
        self.assertEqual(context.page.set_content.call_args.kwargs, {"wait_until": "load", "timeout": 10000})
        context.page.add_style_tag.assert_not_awaited()
        self.assertIn("document.fonts.ready", context.page.evaluate.call_args.args[0])
        self.assertIn("document.images", context.page.evaluate.call_args.args[0])
        screenshot = context.page.screenshot.call_args.kwargs
        self.assertEqual({key: screenshot[key] for key in ("type", "full_page", "animations", "scale")},
                         {"type": "png", "full_page": True, "animations": "disabled", "scale": "css"})

    async def test_low_memory_mode_serializes_render_contexts(self):
        release = asyncio.Event()
        one_started = asyncio.Event()
        active = 0
        peak = 0
        async def make_context(**options):
            nonlocal active, peak
            context = FakeContext()
            self.browser.contexts.append(context)
            active += 1
            peak = max(peak, active)
            if active == 1:
                one_started.set()
            async def screenshot(**arguments):
                await release.wait()
                Path(arguments["path"]).write_bytes(b"local-png")
            async def close():
                nonlocal active
                active -= 1
            context.page.screenshot.side_effect = screenshot
            context.close.side_effect = close
            return context
        self.browser.new_context.side_effect = make_context
        tasks = [asyncio.create_task(self.render()) for _ in range(3)]
        try:
            await asyncio.wait_for(one_started.wait(), 2)
            await asyncio.sleep(0.01)
            self.assertEqual(len(self.browser.contexts), 1)
            release.set()
            await asyncio.gather(*tasks)
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(peak, 1)
        self.assertEqual(active, 0)
        self.playwright.chromium.launch.assert_awaited_once()

    async def test_size_limits_fail_before_screenshot_and_close_context(self):
        for dimensions in ({"width": 960, "height": 25001}, {"width": 10, "height": 30001}):
            async def make_context(**options):
                context = FakeContext()
                context.page.evaluate.return_value = dimensions
                self.browser.contexts.append(context)
                return context
            self.browser.new_context.side_effect = make_context
            with self.subTest(dimensions=dimensions), self.assertRaisesRegex(RuntimeError, "截图尺寸超限"):
                await self.render()
            context = self.browser.contexts[-1]
            context.page.screenshot.assert_not_awaited()
            context.close.assert_awaited_once()
        self.assertEqual(list((self.root / "render_tmp").glob("*.png")), [])

    async def test_capture_error_is_unchanged_and_does_not_discard_a_healthy_browser(self):
        error = RuntimeError("screenshot transport failure")
        original_factory = self.browser.make_context
        async def failed_context(**options):
            context = await original_factory(**options)
            context.page.screenshot.side_effect = error
            return context
        self.browser.new_context.side_effect = failed_context
        with self.assertRaises(RuntimeError) as raised:
            await self.render()
        self.assertIs(raised.exception, error)
        self.browser.contexts[0].close.assert_awaited_once()
        self.browser.new_context.side_effect = original_factory
        await self.render()
        self.playwright.chromium.launch.assert_awaited_once()

    async def test_cancellation_closes_context_and_does_not_leave_partial_png(self):
        started = asyncio.Event()
        async def cancelled_context(**options):
            context = await self.browser.make_context(**options)
            async def screenshot(**arguments):
                Path(arguments["path"]).write_bytes(b"partial")
                started.set()
                await asyncio.Event().wait()
            context.page.screenshot.side_effect = screenshot
            return context
        self.browser.new_context.side_effect = cancelled_context
        task = asyncio.create_task(self.render())
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.browser.contexts[0].close.assert_awaited_once()
        self.assertFalse(self.renderer._contexts)
        self.assertFalse(self.renderer._active_paths)
        self.assertEqual(list((self.root / "render_tmp").glob("*.png")), [])

    async def test_disconnected_browser_is_rebuilt_without_restarting_playwright(self):
        await self.render()
        self.browser.is_connected.return_value = False
        replacement = FakeBrowser()
        self.playwright.chromium.launch.return_value = replacement
        await self.render()
        self.browser.close.assert_awaited_once()
        self.assertIs(self.renderer._browser, replacement)
        self.assertEqual(self.playwright.chromium.launch.await_count, 2)
        self.renderer._start_playwright.assert_awaited_once()

    async def test_prefers_a_system_browser_and_sets_root_launch_flags(self):
        self.system_browser.stop()
        self.which.stop()
        self.which = patch.object(browser_module.shutil, "which", side_effect=lambda name: "/usr/bin/chromium" if name == "chromium" else None)
        self.which.start()
        await self.render()
        options = self.playwright.chromium.launch.call_args.kwargs
        self.assertEqual(options["executable_path"], "/usr/bin/chromium")
        self.assertTrue(options["headless"])
        self.assertIn("--disable-dev-shm-usage", options["args"])

    async def test_explicit_prepare_installs_only_headless_chromium_once(self):
        missing = RuntimeError("Executable doesn't exist at /cache/chromium/chrome-headless-shell")
        self.playwright.chromium.launch.side_effect = [missing, self.browser]
        self.renderer._run_install = AsyncMock()
        self.renderer._find_executable.side_effect = lambda engine, own_only=False: "/plugin/cache/chrome-headless-shell" if own_only else None
        await self.renderer.prepare("headless")
        await self.render()
        self.renderer._run_install.assert_awaited_once_with("install", "--only-shell", "chromium")
        self.assertEqual(self.playwright.chromium.launch.await_count, 2)

    async def test_ordinary_render_never_downloads_a_missing_browser(self):
        missing = RuntimeError("Executable doesn't exist at /cache/chromium/chrome-headless-shell")
        self.playwright.chromium.launch.side_effect = missing
        self.renderer._run_install = AsyncMock()
        for _ in range(2):
            with self.assertRaises(RuntimeError) as raised:
                await self.render()
            self.assertIs(raised.exception, missing)
        self.renderer._run_install.assert_not_awaited()
        self.assertEqual(self.playwright.chromium.launch.await_count, 2)
        self.assertFalse(self.renderer._browser_install_tried["headless"])

    async def test_ordinary_render_never_installs_missing_libraries_even_as_root(self):
        missing = RuntimeError("Host system is missing dependencies to run browsers. Missing libraries: libnss3.so")
        self.playwright.chromium.launch.side_effect = missing
        self.renderer._find_executable.return_value = "/plugin/cache/chrome-headless-shell"
        self.renderer._run_install = AsyncMock()
        with patch.object(browser_module, "_can_install_libraries", return_value=True), self.assertRaises(RuntimeError) as raised:
            await self.render()
        self.assertIs(raised.exception, missing)
        self.renderer._run_install.assert_not_awaited()
        self.assertFalse(self.renderer._deps_install_tried)

    async def test_failed_system_browser_falls_back_to_prepared_engine_without_installing(self):
        system = "/usr/bin/failing-chromium"
        prepared = "/plugin/cache/chrome-headless-shell"
        self.renderer._system_browser.return_value = system
        self.renderer._find_executable.return_value = prepared
        self.playwright.chromium.launch.side_effect = [
            RuntimeError("BrowserType.launch: Target page, context or browser has been closed"), self.browser]
        self.renderer._run_install = AsyncMock()
        result = await self.render()
        self.assertTrue(Path(result).is_file())
        self.assertEqual([call.kwargs.get("executable_path") for call in self.playwright.chromium.launch.await_args_list],
                         [system, prepared])
        self.assertEqual(self.renderer._browser_engine, "headless")
        self.assertIn(system, self.renderer._bad_executables)
        self.renderer._run_install.assert_not_awaited()

    async def test_failed_system_browser_without_prepared_engine_does_not_download(self):
        self.renderer._system_browser.return_value = "/usr/bin/failing-chromium"
        missing = RuntimeError("Executable doesn't exist at /cache/chromium/chrome-headless-shell")
        self.playwright.chromium.launch.side_effect = [RuntimeError("Browser closed during launch"), missing]
        self.renderer._run_install = AsyncMock()
        with self.assertRaises(RuntimeError) as raised:
            await self.render()
        self.assertIs(raised.exception, missing)
        self.assertEqual(self.playwright.chromium.launch.await_count, 2)
        self.renderer._run_install.assert_not_awaited()

    async def test_missing_libraries_install_only_when_explicit_and_privileged(self):
        missing = RuntimeError("Host system is missing dependencies to run browsers. Missing libraries: libnss3.so")
        self.playwright.chromium.launch.side_effect = [missing, self.browser]
        self.renderer._run_install = AsyncMock()
        with patch.object(browser_module, "_can_install_libraries", return_value=True):
            await self.renderer.prepare("headless")
        self.renderer._run_install.assert_awaited_once_with("install-deps", "chromium")

    async def test_missing_library_in_chrome_launch_log_never_redownloads_browser(self):
        executable = '/plugin/cache/chrome-headless-shell'
        missing = RuntimeError(executable + ': error while loading shared libraries: libnspr4.so: cannot open shared object file: No such file or directory')
        self.assertFalse(browser_module._missing_browser(missing))
        self.renderer._find_executable.return_value = executable
        self.playwright.chromium.launch.side_effect = [missing, self.browser]
        self.renderer._run_install = AsyncMock()
        with patch.object(browser_module, '_can_install_libraries', return_value=True):
            await self.renderer.prepare('headless')
        self.renderer._run_install.assert_awaited_once_with('install-deps', 'chromium')
        self.assertNotIn(executable, self.renderer._bad_executables)

    async def test_browser_prepare_has_finite_budget_and_render_does_not_renew_it(self):
        missing = RuntimeError("Executable doesn't exist at /cache/chromium/chrome")
        self.playwright.chromium.launch.side_effect = missing
        self.renderer._run_install = AsyncMock()
        self.renderer._find_executable.side_effect = lambda engine, own_only=False: "/plugin/cache/chrome-headless-shell" if own_only else None
        with self.assertRaises(RuntimeError) as raised:
            await self.renderer.prepare("headless")
        self.assertIs(raised.exception, missing)
        with self.assertRaises(RuntimeError) as raised:
            await self.render()
        self.assertIs(raised.exception, missing)
        self.renderer._run_install.assert_awaited_once_with("install", "--only-shell", "chromium")
        self.assertEqual(self.playwright.chromium.launch.await_count, 4)

    async def test_library_prepare_has_finite_budget_and_render_does_not_renew_it(self):
        missing = RuntimeError("error while loading shared libraries: libX11.so")
        self.playwright.chromium.launch.side_effect = missing
        self.renderer._run_install = AsyncMock()
        with patch.object(browser_module, "_can_install_libraries", return_value=True):
            with self.assertRaises(RuntimeError) as raised:
                await self.renderer.prepare("headless")
            self.assertIs(raised.exception, missing)
            with self.assertRaises(RuntimeError):
                await self.render()
        self.renderer._run_install.assert_awaited_once_with("install-deps", "chromium")
        self.assertEqual(self.playwright.chromium.launch.await_count, 3)

    async def test_unknown_launch_error_is_unchanged_and_never_installs(self):
        error = RuntimeError("Browser closed due to invalid startup flags")
        self.playwright.chromium.launch.side_effect = error
        self.renderer._run_install = AsyncMock()
        with self.assertRaises(RuntimeError) as raised:
            await self.render()
        self.assertIs(raised.exception, error)
        self.renderer._run_install.assert_not_awaited()
        self.playwright.chromium.launch.assert_awaited_once()

    async def test_unprivileged_missing_libraries_are_not_installed(self):
        error = RuntimeError("error while loading shared libraries: libX11.so")
        self.playwright.chromium.launch.side_effect = error
        self.renderer._run_install = AsyncMock()
        with patch.object(browser_module, "_can_install_libraries", return_value=False), self.assertRaises(RuntimeError) as raised:
            await self.renderer.prepare("headless")
        self.assertIs(raised.exception, error)
        self.renderer._run_install.assert_not_awaited()

    async def test_cleanup_removes_only_old_inactive_pngs(self):
        self.renderer.tmp.mkdir(parents=True)
        old = self.renderer.tmp / ("a" * 32 + ".png")
        active = self.renderer.tmp / ("b" * 32 + ".png")
        fresh = self.renderer.tmp / ("c" * 32 + ".png")
        for path in (old, active, fresh):
            path.write_bytes(b"old")
        past = time.time() - 601
        for path in (old, active):
            os.utime(path, (past, past))
        self.renderer._active_paths.add(active)
        await self.render()
        self.assertFalse(old.exists())
        self.assertTrue(active.exists())
        self.assertTrue(fresh.exists())

    async def test_close_releases_browser_driver_and_rejects_new_work(self):
        await self.render()
        await self.renderer.close()
        await self.renderer.close()
        self.browser.close.assert_awaited_once()
        self.playwright.stop.assert_awaited_once()
        with self.assertRaisesRegex(RuntimeError, "已关闭"):
            await self.render()

    async def test_close_cancels_in_progress_browser_preparation(self):
        started = asyncio.Event()
        async def install(*arguments):
            started.set()
            await asyncio.Event().wait()
        self.renderer._run_install = AsyncMock(side_effect=install)
        self.playwright.chromium.launch.side_effect = RuntimeError("Executable doesn't exist at /cache/chromium/chrome")
        task = asyncio.create_task(self.renderer.prepare("headless"))
        await asyncio.wait_for(started.wait(), 2)
        await self.renderer.close()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.playwright.stop.assert_awaited_once()

    async def test_installer_cancellation_terminates_process_and_keeps_argv_separate(self):
        entered = asyncio.Event()
        async def read(size):
            entered.set()
            await asyncio.Event().wait()
        process = SimpleNamespace(stdout=SimpleNamespace(read=AsyncMock(side_effect=read)), wait=AsyncMock(), returncode=None)
        self.renderer._terminate_installer = AsyncMock()
        with patch.object(browser_module.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=process)) as create:
            task = asyncio.create_task(self.renderer._run_install("install", "--only-shell", "chromium"))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.renderer._terminate_installer.assert_awaited_once_with(process)
        self.assertEqual(create.call_args.args, (browser_module.sys.executable, "-m", "playwright", "install", "--only-shell", "chromium"))
        self.assertNotIn("shell", create.call_args.kwargs)
        self.assertEqual(create.call_args.kwargs["env"]["PLAYWRIGHT_BROWSERS_PATH"], str(self.renderer.browser_cache))
        process.stdout.read.assert_awaited_once_with(4096)
        self.assertIsNone(self.renderer._installer)

    async def test_installer_timeout_terminates_process_with_a_chinese_error(self):
        async def read(size):
            await asyncio.Event().wait()
        process = SimpleNamespace(stdout=SimpleNamespace(read=AsyncMock(side_effect=read)), wait=AsyncMock(), returncode=None)
        self.renderer._terminate_installer = AsyncMock()
        with patch.object(browser_module.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=process)), \
             patch.object(browser_module, "INSTALL_TIMEOUT", 0.01), self.assertRaisesRegex(RuntimeError, "初始化超时"):
            await self.renderer._run_install("install", "--only-shell", "chromium")
        self.renderer._terminate_installer.assert_awaited_once_with(process)

    async def test_install_output_reports_percentage_before_process_exit(self):
        waiting, release = asyncio.Event(), asyncio.Event()
        stages = []
        chunks = iter([b"Downloading Chrome Headless Shell\n", b"|==== | 42% of 123.4 MiB\r",
                       b"Chromium downloaded to /plugin/cache\n", b""])
        async def read(size):
            self.assertEqual(size, 4096)
            stages.append(self.renderer.progress())
            return next(chunks)
        async def wait():
            waiting.set()
            await release.wait()
            return 0
        process = SimpleNamespace(stdout=SimpleNamespace(read=AsyncMock(side_effect=read)), wait=AsyncMock(side_effect=wait))
        task = asyncio.create_task(self.renderer._read_install_output(process))
        try:
            await asyncio.wait_for(waiting.wait(), 2)
            self.assertFalse(task.done())
            self.assertTrue(any("42%" in stage and "123.4 MiB" in stage for stage in stages))
            self.assertTrue(any("下载源" in stage for stage in stages))
            self.assertIn("下载完成", self.renderer.progress())
        finally:
            release.set()
            await task
        process.wait.assert_awaited_once_with()

    async def test_install_output_retains_only_a_bounded_diagnostic_tail(self):
        first = b"EARLY-LOG-MARKER" + b"x" * 4000
        last = b"LATEST-DIAGNOSTIC-MARKER\n"
        process = SimpleNamespace(stdout=SimpleNamespace(read=AsyncMock(side_effect=[first, b"y" * 4096,
                                  b"z" * 4096, last, b""])), wait=AsyncMock(return_value=1))
        output = await self.renderer._read_install_output(process)
        self.assertLessEqual(len(output), 6000)
        self.assertTrue(output.endswith(last))
        self.assertNotIn(b"EARLY-LOG-MARKER", output)
        process.wait.assert_awaited_once_with()

    async def test_failed_installer_reports_recent_bounded_output(self):
        process = SimpleNamespace(stdout=SimpleNamespace(read=AsyncMock(side_effect=[b"EARLY-LOG-MARKER" + b"x" * 4000,
                                  b"y" * 4096, b"LATEST-DIAGNOSTIC-MARKER\n", b""])),
                                  wait=AsyncMock(return_value=1), returncode=1)
        self.renderer._terminate_installer = AsyncMock()
        with patch.object(browser_module.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=process)), \
             self.assertRaises(RuntimeError) as raised:
            await self.renderer._run_install("install", "--only-shell", "chromium")
        message = str(raised.exception)
        self.assertIn("LATEST-DIAGNOSTIC-MARKER", message)
        self.assertNotIn("EARLY-LOG-MARKER", message)
        self.assertLess(len(message), 1400)
        self.assertIsNone(self.renderer._installer)
        self.renderer._terminate_installer.assert_not_awaited()

    async def test_idle_shutdown_releases_runtime_and_next_render_restarts_it(self):
        with patch.object(browser_module, "IDLE_TIMEOUT", 0.01):
            await self.render()
            self.renderer._browser_install_tried["headless"] = True
            await asyncio.sleep(0.04)
            self.browser.close.assert_awaited_once()
            self.playwright.stop.assert_awaited_once()
            self.assertIsNone(self.renderer._browser)
            self.assertIsNone(self.renderer._playwright)
            self.assertFalse(self.renderer._closed)
            await self.render()
            self.assertTrue(self.renderer._browser_install_tried["headless"])
            self.assertEqual(self.renderer._start_playwright.await_count, 2)

    async def test_new_render_cancels_idle_shutdown_and_active_page_is_never_closed(self):
        entered, release = asyncio.Event(), asyncio.Event()
        with patch.object(browser_module, "IDLE_TIMEOUT", 0.03):
            await self.render()
            async def context_factory(**options):
                context = await self.browser.make_context(**options)
                async def screenshot(**arguments):
                    entered.set()
                    await release.wait()
                    Path(arguments["path"]).write_bytes(b"local-png")
                context.page.screenshot.side_effect = screenshot
                return context
            self.browser.new_context.side_effect = context_factory
            task = asyncio.create_task(self.render())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await asyncio.sleep(0.05)
                self.browser.close.assert_not_awaited()
                self.playwright.stop.assert_not_awaited()
            finally:
                release.set()
                await task

    async def test_new_render_waits_until_an_in_progress_idle_close_finishes(self):
        closing, release = asyncio.Event(), asyncio.Event()
        async def close_browser():
            closing.set()
            await release.wait()
        self.browser.close.side_effect = close_browser
        with patch.object(browser_module, "IDLE_TIMEOUT", 0.01):
            await self.render()
            await asyncio.wait_for(closing.wait(), 2)
            task = asyncio.create_task(self.render())
            try:
                await asyncio.sleep(0.01)
                self.renderer._start_playwright.assert_awaited_once()
            finally:
                release.set()
                await task
            self.assertEqual(self.renderer._start_playwright.await_count, 2)

    async def test_status_reports_engines_without_starting_a_browser(self):
        status = self.renderer.status()
        self.assertFalse(status["available"])
        self.assertEqual([engine["id"] for engine in status["engines"]], ["headless", "chromium"])
        self.renderer._start_playwright.assert_not_awaited()
        await self.renderer.prepare("chromium")
        self.assertEqual(self.renderer.status()["browser"], "完整 Chromium")
        self.assertTrue(self.renderer.status()["engines"][1]["installed"])
        self.assertIsInstance(self.renderer.progress(), str)

    async def test_prepare_full_chromium_installs_full_engine_and_bypasses_system_browser(self):
        self.system_browser.stop()
        self.which.stop()
        self.which = patch.object(browser_module.shutil, "which", return_value="/usr/bin/chromium")
        self.which.start()
        missing = RuntimeError("Executable doesn't exist at /cache/chromium/chrome")
        self.playwright.chromium.launch.side_effect = [missing, self.browser]
        self.renderer._find_executable.side_effect = lambda engine, own_only=False: "/plugin/cache/chrome" if own_only else None
        self.renderer._run_install = AsyncMock()
        await self.renderer.prepare("chromium")
        self.renderer._run_install.assert_awaited_once_with("install", "--no-shell", "chromium")
        self.assertEqual(self.playwright.chromium.launch.call_args_list[0].kwargs["channel"], "chromium")
        self.assertEqual(self.playwright.chromium.launch.call_args.kwargs["executable_path"], "/plugin/cache/chrome")

    async def test_engine_switch_waits_for_the_current_render_to_finish(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def context_factory(**options):
            context = await self.browser.make_context(**options)
            async def screenshot(**arguments):
                entered.set()
                await release.wait()
                Path(arguments["path"]).write_bytes(b"local-png")
            context.page.screenshot.side_effect = screenshot
            return context
        self.browser.new_context.side_effect = context_factory
        task = asyncio.create_task(self.render(browser_engine="headless"))
        await asyncio.wait_for(entered.wait(), 2)
        prepare = asyncio.create_task(self.renderer.prepare("chromium"))
        try:
            await asyncio.sleep(0.01)
            self.browser.close.assert_not_awaited()
            self.playwright.chromium.launch.assert_awaited_once()
        finally:
            release.set()
            await asyncio.gather(task, prepare)
        self.assertEqual(self.renderer._browser_engine, "chromium")
        self.assertEqual(self.playwright.chromium.launch.await_count, 2)

    async def test_persistent_version_cache_is_detected_and_used_without_installing(self):
        path = self.renderer.browser_cache / "chromium_headless_shell-123" / "chrome-headless-shell-linux64" / "chrome-headless-shell"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"browser-fixture")
        self.renderer._find_executable = browser_module.LocalRenderer._find_executable.__get__(self.renderer)
        self.assertTrue(self.renderer.status()["engines"][0]["installed"])
        self.renderer._run_install = AsyncMock()
        await self.renderer.prepare("headless")
        self.assertEqual(self.playwright.chromium.launch.call_args.kwargs["executable_path"], str(path))
        self.renderer._run_install.assert_not_awaited()

    async def test_manual_prepare_can_retry_a_previous_explicit_download_failure(self):
        missing = RuntimeError("Executable doesn't exist at /cache/chromium/chrome")
        self.playwright.chromium.launch.side_effect = [missing, missing, self.browser]
        self.renderer._run_install = AsyncMock(side_effect=[RuntimeError("simulated download failure"), None])
        self.renderer._find_executable.side_effect = lambda engine, own_only=False: "/plugin/cache/core" if own_only else None
        with self.assertRaisesRegex(RuntimeError, "download failure"):
            await self.renderer.prepare("headless")
        await self.renderer.prepare("headless")
        self.assertEqual(self.renderer._run_install.await_count, 2)
        self.assertEqual(self.renderer._browser_engine, "headless")

    async def test_explicit_local_engine_can_disable_system_browser_preference(self):
        self.renderer._system_browser.return_value = "/usr/bin/chromium"
        await self.render(browser_engine="chromium", prefer_system=False)
        options = self.playwright.chromium.launch.call_args.kwargs
        self.assertEqual(options["channel"], "chromium")
        self.assertNotIn("executable_path", options)
        self.assertEqual(self.renderer._browser_engine, "chromium")

    async def test_windows_system_edge_is_discovered_from_standard_environment_paths(self):
        self.system_browser.stop()
        local = self.root / "local"
        edge = local / "Microsoft/Edge/Application/msedge.exe"
        edge.parent.mkdir(parents=True)
        edge.write_bytes(b"system-browser-fixture")
        environment = {"ProgramFiles": str(self.root / "program"), "ProgramFiles(x86)": str(self.root / "program-x86"), "LOCALAPPDATA": str(local)}
        with patch.dict(os.environ, environment), patch.object(browser_module.sys, "platform", "win32"):
            self.assertEqual(self.renderer._system_browser(), str(edge))


@unittest.skipUnless(importlib.util.find_spec("jinja2") is not None, "Jinja2 is not installed in this test runtime")
class TemplateTests(unittest.TestCase):
    def test_jinja_autoescapes_untrusted_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            renderer = browser_module.LocalRenderer(temporary)
            html = renderer._render_html("<p>{{ text }}</p>", {"text": '<script src="https://example.invalid/x"></script>'})
            self.assertNotIn("<script", html)
            self.assertIn("&lt;script", html)


@unittest.skipUnless(os.environ.get("TIEBA_BROWSER_REAL_TESTS") == "1", "set TIEBA_BROWSER_REAL_TESTS=1 for isolated real Chromium validation")
class RealChromiumTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_local_png_and_no_loopback_http_request(self):
        from PIL import Image
        requests = []
        async def handle(reader, writer):
            requests.append(await reader.read(1024))
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        with tempfile.TemporaryDirectory() as temporary:
            renderer = browser_module.LocalRenderer(temporary)
            try:
                template = '<html><style>html,body{margin:0;width:640px}body{height:160px;background:linear-gradient(red,blue)}</style><body>{{ text }}<img src="http://127.0.0.1:{{ port }}/blocked"></body></html>'
                path = await renderer.render(template, {"text": "本地截图", "port": port}, {"viewport_width": 640})
                with Image.open(path) as image:
                    self.assertEqual(image.format, "PNG")
                    self.assertEqual(image.width, 640)
                    self.assertGreaterEqual(image.height, 160)
                await asyncio.sleep(0.05)
                self.assertEqual(requests, [])
            finally:
                await renderer.close()
                server.close()
                await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
