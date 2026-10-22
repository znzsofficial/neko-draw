import copy
import unittest

from focus_scope import items_for_scope, planner_scopes
import test_task_feedback


def routed(scopes):
    return [{'type': 'function', 'function': {'name': 'reply', 'parameters': {
        'required': ['session_id'], 'properties': {'session_id': {'type': 'string', 'enum': scopes}}}}}]


class FocusFeedback(unittest.IsolatedAsyncioTestCase):
    setUp = test_task_feedback.TaskFeedback.setUp

    async def test_focus_status_is_scoped_and_never_includes_other_tasks(self):
        other = self.mod.DrawTask('other-id', 'unrelated', 'private prompt', 'user')
        self.bot._remember_task(other)
        source = []
        result = await self.bot.configure_planner(items=source, tool_definitions=routed(['chat-a', 'chat-b']))
        text = '\n'.join(self.bot._item_text(item) for item in result['modified_kwargs']['items'])
        self.assertIn('session_id="chat-a"', text)
        self.assertNotIn('other-id', text)
        self.assertNotIn('private prompt', text)
        self.assertEqual(source, [])

    async def test_focus_terminal_consumed_once_only_when_payload_exists(self):
        self.task.touch('succeeded')
        await self.bot.configure_planner(tool_definitions=routed(['chat-a']))
        self.assertFalse(self.task.terminal_hint_used)
        result = await self.bot.configure_planner(items=[], tool_definitions=routed(['chat-a']))
        self.assertIn('已完成', str(result))
        self.assertTrue(self.task.terminal_hint_used)
        result = await self.bot.configure_planner(**result['modified_kwargs'])
        self.assertEqual(result['modified_kwargs']['items'], [])

    async def test_focus_drawing_hint_uses_only_each_real_source_not_roster_or_quote(self):
        def msg(scope, text):
            return {'item_type': 'UserMessageItem', 'parts': [
                {'type': 'text', 'text': f'<message msg_id="m" session_id="{scope}">\n'},
                {'type': 'text', 'text': text}]}
        items = [msg('chat-a', '帮我画一张风景'), msg('chat-b', '不需要画图'), msg('outside', '帮我画一张')]
        before = copy.deepcopy(items)
        result = await self.bot.configure_planner(items=items, tool_definitions=routed(['chat-a', 'chat-b']))
        hints = [self.bot._item_text(item) for item in result['modified_kwargs']['items'] if item['item_type'] == 'SystemMessageItem']
        route = next(text for text in hints if '不是自动执行授权' in text)
        self.assertIn('["chat-a"]', route)
        self.assertNotIn('chat-b', route)
        self.assertNotIn('outside', route)
        self.assertEqual(items, before)
        self.assertIsNone(await self.bot.configure_planner(items=[msg('chat-a', '帮我画一张')]))

    def test_bounded_host_scope_schema_and_prefix(self):
        for scopes in [[], ['a'] * 33, ['x\ny'], [None]]:
            self.assertEqual(planner_scopes('', routed(scopes)), ())
        self.assertEqual(planner_scopes('', routed(['a']) * 2), ())
        self.assertEqual(items_for_scope([{'item_type': 'UserMessageItem', 'parts': [{'text': '在群(a)中的注意：帮我画一张'}]}], 'a'), [])
