import asyncio
import importlib.util
import json
import pathlib
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock
from unittest.mock import patch


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
            payload = (await self.bot.configure_planner(**payload))['modified_kwargs']
            self.assertIn('已完成', json.dumps(payload, ensure_ascii=False))
            payload = (await self.bot.configure_planner(**payload))['modified_kwargs']
            self.assertEqual(payload[key], [])
            self.assertIsNone(await self.bot.configure_planner(**payload))

    async def test_failure_remains_visible_on_subsequent_planner_calls(self):
        self.task.touch('failed', error=self.bot._policy_feedback('', 'HTTP 400：{"error":{"message":"openai_error"}}'))
        result = await self.bot.configure_planner(session_id='chat-a', items=[])
        text = json.dumps(result, ensure_ascii=False)
        self.assertIn('已失败或取消', text)
        self.assertNotIn('有时是提示词被拦截', text)
        self.assertIn('原因不明', self.bot._status_text('chat-a'))
        self.assertIsNone(await self.bot.configure_planner(session_id='chat-a', items=[]))
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
        self.assertIsNone(result)
        self.assertIn('不能据历史提交回执', self.bot._status_text('chat-a'))

    async def test_expiry_dual_payload_and_new_task(self):
        self.task.touch('failed', error='private details')
        with patch.object(self.mod.time, 'monotonic', return_value=self.task.terminal_at + 900):
            self.assertIsNone(await self.bot.configure_planner(session_id='chat-a', items=[], messages=[]))
        self.assertFalse(self.task.terminal_hint_used)
        result = await self.bot.configure_planner(session_id='chat-a', items=[], messages=[])
        self.assertEqual(len(result['modified_kwargs']['items']), 1)
        self.assertEqual(len(result['modified_kwargs']['messages']), 1)
        self.assertNotIn('private details', json.dumps(result))
        cleaned = await self.bot.configure_planner(**result['modified_kwargs'])
        self.assertEqual(cleaned['modified_kwargs']['items'], [])
        self.assertEqual(cleaned['modified_kwargs']['messages'], [])
        new = self.mod.DrawTask('new-task', 'chat-a', 'new prompt', 'user')
        self.bot._remember_task(new)
        new.touch('succeeded')
        self.assertIsNotNone(await self.bot.configure_planner(session_id='chat-a', items=[]))

    async def test_cleanup_preserves_user_tool_and_other_system(self):
        marker = '[neko.draw 当前任务状态]'
        self.bot._tasks.clear()
        keep = [{'item_type': 'UserMessageItem', 'parts': [{'text': marker}]},
                {'item_type': 'ToolOutputItem', 'content': '状态：排队'}, self.bot._system_item('other '+marker)]
        result = await self.bot.configure_planner(session_id='chat-a', items=[self.bot._system_item(marker+' old'), *keep])
        self.assertEqual(result['modified_kwargs']['items'], keep)

    async def test_no_payload_does_not_consume_terminal_hint(self):
        self.task.touch('succeeded')
        self.assertIsNone(await self.bot.configure_planner(session_id='chat-a'))
        self.assertFalse(self.task.terminal_hint_used)

    def test_eviction_cleans_latest_reference(self):
        self.task.touch('failed')
        for n in range(31):
            task = self.mod.DrawTask(str(n), 'other-'+str(n), 'prompt', 'user')
            task.touch('succeeded')
            self.bot._remember_task(task)
        self.assertNotIn('chat-a', self.bot._latest_by_stream)

    def test_error_evidence_is_preserved_without_inventing_cause(self):
        for raw in ('HTTP 400：{"error":{"message":"openai_error"}}', 'HTTP 400：broken JSON',
                    'HTTP 400：{"error":{"code":"openai_error"}}'):
            text = self.bot._policy_feedback('prompt', raw)
            self.assertIn('原因不明', text)
            self.assertIn('不能仅凭 HTTP 400 断言源图 ID 错误', text)
            self.assertIn('有时是提示词被拦截', text)
            self.assertIn('修改提示词后重新生成', text)
            self.assertNotIn('只试一次', text)
        specific = self.bot._policy_feedback('', 'HTTP 400：{"error":{"message":"Invalid size: 999x999"}}')
        self.assertIn('Invalid size: 999x999', specific)
        self.assertNotIn('具体原因未知', specific)
