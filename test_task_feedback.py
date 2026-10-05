import asyncio
import importlib.util
import json
import pathlib
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock


@unittest.skipUnless(importlib.util.find_spec('maibot_sdk'), 'requires MaiBot SDK')
class TaskFeedback(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        root = pathlib.Path(__file__).parent
        pkg = types.ModuleType('draw_feedback_test'); pkg.__path__ = [str(root)]; sys.modules[pkg.__name__] = pkg
        spec = importlib.util.spec_from_file_location('draw_feedback_test.plugin', root/'plugin.py')
        mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = mod; spec.loader.exec_module(mod)
        self.mod = mod
        class Bot(mod.NekoDraw):
            config = mod.PluginConfig()
            ctx = types.SimpleNamespace(logger=Mock())
        self.bot = object.__new__(Bot)
        self.bot._tasks = {}; self.bot._latest_by_stream = {}; self.bot._runners = {}
        self.task = mod.DrawTask('1234abcd', 'chat-a', 'test prompt', 'user')
        self.bot._remember_task(self.task)

    async def test_current_and_legacy_snapshots_replace_old_queue_state(self):
        for key in ('items', 'messages'):
            self.task.touch('queued')
            payload = {'session_id':'chat-a', key:[]}
            payload = (await self.bot.configure_planner(**payload))['modified_kwargs']
            self.assertIn('尚未开始', json.dumps(payload, ensure_ascii=False))
            self.task.touch('succeeded')
            for _ in range(3):
                payload = (await self.bot.configure_planner(**payload))['modified_kwargs']
                self.assertEqual(len(payload[key]), 1)
                text = json.dumps(payload, ensure_ascii=False)
                self.assertIn('已完成', text)
                self.assertNotIn('尚未开始', text)

    async def test_failure_remains_visible_on_subsequent_planner_calls(self):
        self.task.touch('failed', error=self.bot._policy_feedback('', 'HTTP 400：{"error":{"message":"openai_error"}}'))
        for _ in range(2):
            result = await self.bot.configure_planner(session_id='chat-a', items=[])
            text = json.dumps(result, ensure_ascii=False)
            self.assertIn('已失败或取消', text)
            self.assertIn('具体原因未知', text)
            self.assertIn('不能排除审核拦截', text)
        self.assertIsNone(await self.bot.configure_planner(session_id='chat-b', items=[]))

    async def test_send_confirmation_controls_terminal_status(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def send(*args):
            entered.set()
            await release.wait()
        self.bot._generate = AsyncMock(return_value=(b'image', 'openai'))
        self.bot._send = send
        self.task.touch('running')
        runner = asyncio.create_task(self.bot._run_task(self.task))
        await entered.wait()
        self.assertEqual(self.task.status, 'running')
        release.set()
        await runner
        self.assertEqual(self.task.status, 'succeeded')
        self.bot._send = AsyncMock(side_effect=RuntimeError('delivery failed'))
        self.task.touch('running')
        await self.bot._run_task(self.task)
        self.assertEqual(self.task.status, 'failed')

    async def test_lost_state_after_reload_does_not_repeat_old_receipt(self):
        self.bot._tasks.clear(); self.bot._latest_by_stream.clear()
        result = await self.bot.configure_planner(session_id='chat-a', messages=[
            {'role':'tool', 'content':'neko_edit_image 提交成功；状态：排队'}])
        text = json.dumps(result, ensure_ascii=False)
        self.assertIn('没有可核实的任务记录', text)
        self.assertIn('不能把历史提交回执当作仍在执行的证据', text)

    def test_error_evidence_is_preserved_without_inventing_cause(self):
        for raw in ('HTTP 400：{"error":{"message":"openai_error"}}', 'HTTP 400：broken JSON',
                    'HTTP 400：{"error":{"code":"openai_error"}}'):
            text = self.bot._policy_feedback('prompt', raw)
            self.assertIn('具体原因未知', text)
            self.assertIn('不能仅凭 HTTP 400 断言源图 ID 错误', text)
            self.assertIn('不能排除审核拦截', text)
            self.assertIn('不能说已经确认违规', text)
            self.assertIn('允许主动调整提示词并重新调用一次原工具', text)
            self.assertIn('只试一次', text)
        specific = self.bot._policy_feedback('', 'HTTP 400：{"error":{"message":"Invalid size: 999x999"}}')
        self.assertIn('Invalid size: 999x999', specific)
        self.assertNotIn('具体原因未知', specific)
