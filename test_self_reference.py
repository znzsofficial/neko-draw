import asyncio
import importlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image
from self_reference import read_reference, MAX_REFERENCE_BYTES, portrait_prompt


def image_bytes(fmt='JPEG'):
    buf = io.BytesIO()
    Image.new('RGB', (24, 32), 'blue').save(buf, format=fmt)
    return buf.getvalue()


class ReferenceTests(unittest.TestCase):
    def test_static_formats_preserve_exact_original_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            for fmt in ['PNG', 'JPEG', 'WEBP']:
                path=Path(directory)/('ref.'+fmt.lower());data=image_bytes(fmt);path.write_bytes(data)
                self.assertEqual(read_reference(str(path)), data)

    def test_directory_missing_relative_corrupt_and_oversize_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'ref.jpg'
            for value in [directory, str(path), 'relative.jpg']:
                with self.assertRaises((ValueError,OSError)):read_reference(value)
            for data in [b'',b'not-an-image',b'x'*(MAX_REFERENCE_BYTES+1)]:
                path.write_bytes(data)
                with self.assertRaises((ValueError,OSError)):read_reference(str(path))

    def test_animation_and_excessive_pixels_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'ref.png'
            first=Image.new('RGB',(8,8),'blue');second=Image.new('RGB',(8,8),'red')
            first.save(path,format='PNG',save_all=True,append_images=[second],duration=100)
            with self.assertRaises(ValueError):read_reference(str(path))
            path.write_bytes(image_bytes())
            with patch('self_reference.MAX_REFERENCE_PIXELS',100):
                with self.assertRaises(ValueError):read_reference(str(path))

    def test_symlink_is_rejected_before_open_without_platform_privilege(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'ref.jpg';path.write_bytes(image_bytes())
            with patch.object(Path,'is_symlink',return_value=True),patch('self_reference.os.open',side_effect=AssertionError('must not read')):
                with self.assertRaises(ValueError):read_reference(str(path))

    def test_replaced_inode_or_changed_stat_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'ref.jpg';path.write_bytes(image_bytes())
            with patch('self_reference.os.path.samestat',return_value=False):
                with self.assertRaises(ValueError):read_reference(str(path))


class ToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        pkg=types.ModuleType('draw_reference_test');pkg.__path__=[str(Path(__file__).parent)]
        sys.modules[pkg.__name__]=pkg
        self.module=importlib.import_module(pkg.__name__+'.plugin')
        module=self.module
        class Bot(module.NekoDraw):
            config=module.PluginConfig()
            ctx=types.SimpleNamespace(logger=Mock())
        self.bot=Bot();self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.addAsyncCleanup(self.bot.on_unload)
        self.path=Path(self.temp.name)/'ref.jpg';self.data=image_bytes();self.path.write_bytes(self.data)
        self.bot.config.self_portrait.enabled=True;self.bot.config.self_portrait.reference_path=str(self.path)
        self.bot._start_task=Mock(return_value=types.SimpleNamespace(summary=lambda:'queued'))

    async def test_explicit_reference_attaches_original_and_uses_edit_path(self):
        result=await self.bot.draw(prompt='在窗边读书的自画像',use_self_reference=True,stream_id='chat')
        self.assertTrue(result['success'])
        self.assertIn('已附角色参考图',result['content'])
        args=self.bot._start_task.call_args.args
        self.assertEqual(args[3],[self.data]);self.assertIn('视觉依据',args[1]);self.assertIn('窗边',args[1])
        self.assertNotIn(str(self.path),args[1])

    async def test_normal_drawing_never_reads_reference_or_attaches_it(self):
        with patch.object(self.module,'read_reference',side_effect=AssertionError('ordinary drawing must not read')):
            result=await self.bot.draw(prompt='无人环境',stream_id='chat')
        self.assertTrue(result['success']);self.assertEqual(len(self.bot._start_task.call_args.args),3)

    async def test_missing_or_disabled_reference_denies_without_text_fallback(self):
        for disabled in [False,True]:
            self.bot.config.self_portrait.enabled=not disabled
            self.bot.config.self_portrait.reference_path=str(self.path)+'-missing'
            result=await self.bot.draw(prompt='self portrait',use_self_reference=True,stream_id='chat')
            self.assertFalse(result['success'])
        self.bot._start_task.assert_not_called()

    async def test_reconfiguration_during_file_read_denies(self):
        original=asyncio.to_thread
        async def change(*args):
            value=await original(*args)
            self.bot.config.self_portrait.enabled=False
            return value
        with patch.object(self.module.asyncio,'to_thread',side_effect=change):
            result=await self.bot.draw(prompt='self portrait',use_self_reference=True,stream_id='chat')
        self.assertFalse(result['success']);self.bot._start_task.assert_not_called()

    async def test_racing_active_task_after_file_read_does_not_start(self):
        self.bot._active_task=Mock(side_effect=[None,types.SimpleNamespace(summary=lambda:'running')])
        result=await self.bot.draw(prompt='self portrait',use_self_reference=True,stream_id='chat')
        self.assertFalse(result['success']);self.bot._start_task.assert_not_called()

    async def test_invalid_bool_and_cancellation_do_not_generate(self):
        for value in ['true',1,None]:
            self.assertFalse((await self.bot.draw('self',use_self_reference=value,stream_id='chat'))['success'])
        async def cancel(*args):raise asyncio.CancelledError
        with patch.object(self.module.asyncio,'to_thread',side_effect=cancel):
            with self.assertRaises(asyncio.CancelledError):
                await self.bot.draw('self',use_self_reference=True,stream_id='chat')
        self.bot._start_task.assert_not_called()

    async def test_planner_prefers_reference_without_path_or_memory_prerequisite(self):
        text=self.bot._planner_instruction(True)
        self.assertIn('use_self_reference=true',text);self.assertIn('不必先调用query_memory',text)
        self.assertNotIn(str(self.path),text)
        result=await self.bot.configure_planner(stream_id='chat',items=[{'item_type':'UserMessageItem','parts':[{'type':'text','text':'画你自己在读书'}]}])
        self.assertIn('use_self_reference=true',json.dumps(result,ensure_ascii=False))

    async def test_gemini_native_receives_reference_as_inline_image(self):
        self.bot.config.gemini.api_key='fake-key'
        self.bot._post_json=AsyncMock(return_value={'candidates':[{'content':{'parts':[{'inlineData':{'data':__import__('base64').b64encode(image_bytes('PNG')).decode()}}]}}]})
        await self.bot._gemini_native(portrait_prompt('读书'),[self.data])
        payload=self.bot._post_json.call_args.args[1]
        parts=payload['contents'][0]['parts']
        self.assertEqual(parts[1]['inlineData']['mimeType'],'image/jpeg')
        self.assertEqual(__import__('base64').b64decode(parts[1]['inlineData']['data']),self.data)
