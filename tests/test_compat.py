"""Backend compatibility checks without an AstrBot or QQ connection."""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


COMPAT_PATH = Path(__file__).resolve().parents[1] / "compat.py"


class FakeResponse:
    def __init__(self, content, *, content_type=None, status=200, headers=None):
        self.content = content
        self.content_type = content_type
        self.status_code = status
        self.headers = dict(headers or {})


class FakeQuery(dict):
    def get(self, key, default=None, type=None):
        value = super().get(key, default)
        if type is None:
            return value
        try:
            return type(value)
        except (TypeError, ValueError):
            return default


class FakeQuartRequest:
    method = "POST"
    args = FakeQuery({"page": "3"})
    headers = {"Content-Type": "application/json", "Content-Length": "15"}

    def __init__(self):
        self.payload = {"enabled": True}
        self.raw = b'{"enabled":true}'
        self.error = None

    async def get_json(self):
        if self.error:
            raise self.error
        return self.payload

    async def get_data(self):
        return self.raw


def load_compat(web_module, quart_module=None):
    astrbot_module = types.ModuleType("astrbot")
    astrbot_module.__path__ = []
    api_module = types.ModuleType("astrbot.api")
    api_module.__path__ = []
    modules = {
        "astrbot": astrbot_module,
        "astrbot.api": api_module,
        "astrbot.api.web": web_module,
        "quart": quart_module,
    }
    spec = importlib.util.spec_from_file_location("_tieba_web_compat_test", COMPAT_PATH)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


def load_quart_compat():
    quart_module = types.ModuleType("quart")
    quart_module.request = FakeQuartRequest()
    quart_module.Response = FakeResponse
    quart_module.jsonify = lambda data: FakeResponse(data, content_type="application/json")
    return load_compat(None, quart_module), quart_module.request


class ModernBackendTests(unittest.TestCase):
    def test_official_helpers_are_reexported_without_quart(self):
        web_module = types.ModuleType("astrbot.api.web")
        for name in ("error_response", "json_response", "request", "stream_response"):
            setattr(web_module, name, object())
        module = load_compat(web_module)
        for name in module.__all__:
            self.assertIs(getattr(module, name), getattr(web_module, name))


class QuartBackendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module, self.quart_request = load_quart_compat()

    async def test_request_reads_current_quart_context(self):
        request = self.module.request
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.query.get("page", 0, type=int), 3)
        self.assertEqual(request.query.get("missing", 0, type=int), 0)
        self.assertEqual(request.headers.get("Content-Length"), "15")
        self.assertEqual(await request.json(default={}), {"enabled": True})
        self.assertEqual(await request.body(), b'{"enabled":true}')
        self.quart_request.payload = {"enabled": False}
        self.assertEqual(await request.json(default={}), {"enabled": False})

    async def test_invalid_json_preserves_default_and_valid_null(self):
        fallback = {"fallback": True}
        self.quart_request.error = ValueError("invalid JSON")
        self.assertIs(await self.module.request.json(default=fallback), fallback)
        self.assertIsNone(await self.module.request.json())
        self.quart_request.error = None
        self.quart_request.payload = None
        self.assertIsNone(await self.module.request.json(default=fallback))

    async def test_json_keeps_payload_and_status_headers(self):
        response = self.module.json_response(
            {"settings": {"enabled": False}}, status_code=201,
            headers={"X-Result": "saved"},
        )
        self.assertEqual(response.content, {"settings": {"enabled": False}})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.headers, {"X-Result": "saved"})
        self.assertEqual(self.module.json_response().content, {})
        self.assertEqual(self.module.json_response([]).content, [])

    async def test_error_uses_official_envelope(self):
        response = self.module.error_response(
            "missing", status_code=404, data={"id": "example"},
            headers={"X-Result": "missing"},
        )
        self.assertEqual(response.content, {
            "status": "error", "message": "missing", "data": {"id": "example"},
        })
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers["X-Result"], "missing")

    async def test_download_stream_keeps_async_chunks_and_metadata(self):
        async def chunks():
            yield b"first"
            yield b"second"

        response = self.module.stream_response(
            chunks(), content_type="application/zip", status_code=206,
            headers={"Content-Disposition": 'attachment; filename="export.zip"'},
        )
        self.assertEqual(response.content_type, "application/zip")
        self.assertEqual(response.status_code, 206)
        self.assertIn("export.zip", response.headers["Content-Disposition"])
        self.assertEqual(b"".join([chunk async for chunk in response.content]), b"firstsecond")


if __name__ == "__main__":
    unittest.main()
