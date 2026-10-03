import asyncio
import base64
import importlib.util
import pathlib
import sys
import types
import unittest
from unittest.mock import AsyncMock

ROOT = pathlib.Path(__file__).parent
sys.path.insert(0, str(ROOT))
from source_images import ImageCache, resolve_images, extract

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')


def message(mid='img'):
    return {'message_id': mid, 'message_segments': [
        {'type': 'image', 'binary_data_base64': base64.b64encode(PNG).decode()}]}


class Sources(unittest.IsolatedAsyncioTestCase):
    async def test_quote_and_stream_scope(self):
        ctx = types.SimpleNamespace(call_capability=AsyncMock(return_value={'message': message()}))
        result = await resolve_images(ctx, 'chat-a', current={'message_segments': [{'type': 'reply', 'data': {'id': 'img'}}]})
        self.assertEqual(result, [PNG])
        self.assertEqual(ctx.call_capability.call_args.kwargs['chat_id'], 'chat-a')

    async def test_missing_explicit_image_never_uses_other_message(self):
        ctx = types.SimpleNamespace(call_capability=AsyncMock(return_value=None))
        with self.assertRaises(ValueError):
            await resolve_images(ctx, 'chat-a', 'missing', current=message())

    async def test_cache_recovers_stripped_binary_and_isolates_chats(self):
        cache = ImageCache()
        cache.put('a', message())
        ctx = types.SimpleNamespace(call_capability=AsyncMock(return_value=None))
        self.assertEqual(await resolve_images(ctx, 'a', 'img', cache=cache), [PNG])
        with self.assertRaises(ValueError):
            await resolve_images(ctx, 'b', 'img', cache=cache)
        cache.entries[('a', 'img')] = (0, [PNG])
        self.assertEqual(cache.get('a', 'img'), [])

    def test_duplicate_segments_and_bad_data(self):
        m = message()
        m['raw_message'] = m['message_segments']
        self.assertEqual(extract(m), [PNG])
        m['message_segments'][0]['binary_data_base64'] = 'not an image'
        with self.assertRaises(ValueError):
            extract(m)


@unittest.skipUnless(importlib.util.find_spec('maibot_sdk'), 'requires MaiBot SDK (run on server)')
class Providers(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        package = types.ModuleType('neko_test_package')
        package.__path__ = [str(ROOT)]
        sys.modules[package.__name__] = package
        spec = importlib.util.spec_from_file_location('neko_test_package.plugin', ROOT / 'plugin.py')
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        self.mod = mod
        self.bot = object.__new__(mod.NekoDraw)

    async def test_chat_keeps_image_bytes(self):
        self.bot._post_json = AsyncMock(return_value={})
        self.bot._images_from_openai_body = AsyncMock(return_value=[PNG])
        cfg = self.mod.OpenAIConfig(api_key='test')
        await self.bot._openai_chat('change background', cfg, [PNG])
        content = self.bot._post_json.call_args.args[1]['messages'][0]['content']
        self.assertEqual(content[0]['text'], 'change background')
        self.assertEqual(content[1]['image_url']['url'], 'data:image/png;base64,' + base64.b64encode(PNG).decode())

    async def test_edits_multipart_is_recreated_on_each_attempt(self):
        class Response:
            status = 200
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def text(self): return '{}'
            async def json(self, **kwargs): return {}
        forms = []
        def post(url, **kwargs):
            self.assertTrue(url.endswith('/v1/images/edits'))
            forms.append(kwargs['data'])
            self.assertNotIn('Content-Type', kwargs['headers'])
            return Response()
        self.bot._http = AsyncMock(return_value=types.SimpleNamespace(post=post))
        self.bot._images_from_openai_body = AsyncMock(return_value=[PNG])
        for _ in range(2):
            await self.bot._openai_images('edit', self.mod.OpenAIConfig(api_key='test'), [PNG])
        self.assertIsNot(forms[0], forms[1])
        self.assertEqual(forms[0]._fields[-1][0]['name'], 'image[]')
        self.assertEqual(forms[0]._fields[-1][2], PNG)

    async def test_gemini_keeps_inline_image(self):
        encoded = base64.b64encode(PNG).decode()
        post = AsyncMock(return_value={'candidates': [{'content': {'parts': [{'inlineData': {'data': encoded}}]}}]})
        bot = types.SimpleNamespace(config=types.SimpleNamespace(gemini=self.mod.GeminiConfig(api_key='test')),
                                    _post_json=post)
        result = await self.mod.NekoDraw._gemini_native(bot, 'edit background', [PNG])
        self.assertEqual(result, PNG)
        parts = post.call_args.args[1]['contents'][0]['parts']
        self.assertEqual(parts[1]['inlineData'], {'mimeType': 'image/png', 'data': encoded})


if __name__ == '__main__':
    unittest.main()
