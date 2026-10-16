"""QQ upload retry contracts; no AstrBot runtime, network, or real delays."""
from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, call


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "_tieba_delivery_tests"
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package
delivery = importlib.import_module(f"{PACKAGE}.delivery")


SNOWLUMA = (
    "highway upload transport failed after 3 attempts "
    "(cmdId=1004 chunk=1048576/4280946 offset=0): Error: TCP connect timeout"
)
NAPCAT = "图片上传失败: Error: connect ETIMEDOUT 192.0.2.1:443"


class ActionFailed(Exception):
    """The structured error boundary exposed by aiocqhttp, without its SDK."""

    def __init__(self, wording, *, retcode=100):
        self.info = {"status": "failed", "retcode": retcode, "wording": wording}
        super().__init__(str(self.info))


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def test_snowluma_explicit_upload_connect_failure_is_retryable(self):
        self.assertTrue(delivery.is_upload_failure(ActionFailed(SNOWLUMA)))

    def test_napcat_explicit_upload_connect_failures_are_retryable(self):
        for wording in (
            NAPCAT,
            "图片上传失败: Error: connect timeout",
            "富媒体上传失败: Error: connect ETIMEDOUT 192.0.2.1:443",
            "富媒体上传失败: Error: TCP connect timeout",
        ):
            with self.subTest(wording=wording):
                self.assertTrue(delivery.is_upload_failure(ActionFailed(wording)))

    def test_unconfirmed_delivery_and_permanent_failures_are_not_retryable(self):
        for error in (
            TimeoutError("OneBot API response timeout"),
            ConnectionError("WebSocket disconnected while waiting for response"),
            ActionFailed("send_group_msg API response timeout"),
            ActionFailed("WebSocket connection closed"),
            ActionFailed("群内已禁言"),
            ActionFailed("权限不足，无法发送消息"),
            ActionFailed("账号受到风控，消息发送失败"),
            ActionFailed("图片上传失败: 文件格式不支持"),
            ActionFailed("富媒体上传失败: file not found"),
            ActionFailed("TCP connect timeout"),
            RuntimeError(SNOWLUMA),
            ActionFailed("image upload completed; send_group_msg failed: connect timeout"),
        ):
            with self.subTest(error=str(error)):
                self.assertFalse(delivery.is_upload_failure(error))

    def test_upload_signature_with_another_retcode_is_not_retryable(self):
        self.assertFalse(delivery.is_upload_failure(ActionFailed(SNOWLUMA, retcode=1200)))

    async def test_success_is_sent_once_and_preserves_return_value(self):
        result = object()
        send = AsyncMock(return_value=result)
        sleep = AsyncMock()
        self.assertIs(await delivery.send_with_retry(send, sleep=sleep), result)
        send.assert_awaited_once_with()
        sleep.assert_not_awaited()

    async def test_transient_upload_failure_rebuilds_only_current_message(self):
        messages = []
        result = object()

        async def send():
            # The caller owns message reconstruction, including stream handles.
            message = object()
            messages.append(message)
            if len(messages) == 1:
                raise ActionFailed(SNOWLUMA)
            return result

        sleep = AsyncMock()
        self.assertIs(await delivery.send_with_retry(send, sleep=sleep), result)
        self.assertEqual(len(messages), 2)
        self.assertIsNot(messages[0], messages[1])
        sleep.assert_awaited_once_with(1)

    async def test_two_upload_failures_allow_third_attempt_after_one_and_three_seconds(self):
        result = object()
        send = AsyncMock(side_effect=[ActionFailed(NAPCAT), ActionFailed(SNOWLUMA), result])
        sleep = AsyncMock()
        self.assertIs(await delivery.send_with_retry(send, sleep=sleep), result)
        self.assertEqual(send.await_count, 3)
        self.assertEqual(sleep.await_args_list, [call(1), call(3)])

    async def test_third_upload_failure_is_reraised_unchanged(self):
        final = ActionFailed(SNOWLUMA)
        send = AsyncMock(side_effect=[ActionFailed(NAPCAT), ActionFailed(SNOWLUMA), final])
        sleep = AsyncMock()
        with self.assertRaises(ActionFailed) as raised:
            await delivery.send_with_retry(send, sleep=sleep)
        self.assertIs(raised.exception, final)
        self.assertEqual(send.await_count, 3)
        self.assertEqual(sleep.await_args_list, [call(1), call(3)])

    async def test_non_upload_errors_are_reraised_once_unchanged(self):
        for error in (
            TimeoutError("API response timeout"),
            ConnectionError("WebSocket disconnected"),
            ActionFailed("群内已禁言"),
            ActionFailed("权限不足"),
            ActionFailed("账号受到风控"),
            ActionFailed("图片上传失败: file not found"),
            ActionFailed(SNOWLUMA, retcode=1200),
        ):
            with self.subTest(error=str(error)):
                send = AsyncMock(side_effect=error)
                sleep = AsyncMock()
                with self.assertRaises(type(error)) as raised:
                    await delivery.send_with_retry(send, sleep=sleep)
                self.assertIs(raised.exception, error)
                send.assert_awaited_once_with()
                sleep.assert_not_awaited()

    async def test_ambiguous_error_after_upload_retry_stops_immediately(self):
        final = TimeoutError("API result is unknown")
        send = AsyncMock(side_effect=[ActionFailed(SNOWLUMA), final, object()])
        sleep = AsyncMock()
        with self.assertRaises(TimeoutError) as raised:
            await delivery.send_with_retry(send, sleep=sleep)
        self.assertIs(raised.exception, final)
        self.assertEqual(send.await_count, 2)
        sleep.assert_awaited_once_with(1)

    async def test_cancellation_during_send_propagates_without_retry(self):
        cancelled = asyncio.CancelledError("plugin stopping")
        send = AsyncMock(side_effect=cancelled)
        sleep = AsyncMock()
        with self.assertRaises(asyncio.CancelledError) as raised:
            await delivery.send_with_retry(send, sleep=sleep)
        self.assertIs(raised.exception, cancelled)
        send.assert_awaited_once_with()
        sleep.assert_not_awaited()

    async def test_cancellation_during_backoff_propagates_without_another_send(self):
        cancelled = asyncio.CancelledError("plugin stopping during backoff")
        send = AsyncMock(side_effect=ActionFailed(SNOWLUMA))
        sleep = AsyncMock(side_effect=cancelled)
        with self.assertRaises(asyncio.CancelledError) as raised:
            await delivery.send_with_retry(send, sleep=sleep)
        self.assertIs(raised.exception, cancelled)
        send.assert_awaited_once_with()
        sleep.assert_awaited_once_with(1)


if __name__ == "__main__":
    unittest.main()
