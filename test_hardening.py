import asyncio
import base64
import importlib.util
import io
import pathlib
import sys
import types
import unittest
from unittest.mock import AsyncMock

from PIL import Image
from result_images import decode_image, validate_image, read_response, MAX_RESPONSE_BYTES


class ImageLimits(unittest.IsolatedAsyncioTestCase):
    def test_valid_png_and_fake_image(self):
        buffer = io.BytesIO()
        Image.new('RGB', (2, 2)).save(buffer, format='PNG')
        data = buffer.getvalue()
        self.assertEqual(decode_image(base64.b64encode(data).decode()), data)
        for bad in (b'<html>not image</html>', b'\x89PNG\r\n\x1a\n' + b'x'*100):
            with self.assertRaises(ValueError): validate_image(bad)

    def test_base64_limit_before_decode(self):
        with self.assertRaises(ValueError): decode_image('A' * (24*1024*1024))

    async def test_streaming_response_limit(self):
        class Content:
            async def iter_chunked(self, size):
                for _ in range(MAX_RESPONSE_BYTES // 65536 + 1): yield b'x'*65536
        with self.assertRaises(ValueError):
            await read_response(types.SimpleNamespace(content=Content()))


@unittest.skipUnless(importlib.util.find_spec('maibot_sdk'), 'requires MaiBot SDK')
class Lifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_before_start_releases_chat_and_input_images(self):
        root = pathlib.Path(__file__).parent
        package = types.ModuleType('draw_hardening')
        package.__path__ = [str(root)]
        sys.modules[package.__name__] = package
        spec = importlib.util.spec_from_file_location('draw_hardening.plugin', root/'plugin.py')
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        class TestBot(mod.NekoDraw):
            config = mod.PluginConfig()
        bot = object.__new__(TestBot)
        bot._tasks = {}; bot._latest_by_stream = {}; bot._runners = {}; bot._policy_notice = {}; bot._session = None
        bot._generate = AsyncMock()
        task = bot._start_task('chat', 'prompt', 'user', [b'image'])
        await bot.on_unload()
        self.assertEqual(task.status, 'failed')
        self.assertFalse(task.images)
        self.assertIsNone(bot._active_task('chat'))
        bot._generate.assert_not_awaited()
