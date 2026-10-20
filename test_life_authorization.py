import asyncio
import importlib.util
import json
import sqlite3
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock
import uuid

from life_authorization import authenticate, consume, target_allowed


def host_stream(session="chat", target="123", kind="group", account="456"):
    return {"session_id": session, "stream_id": session, "platform": "qq", "chat_type": kind,
            "is_group_session": kind == "group", "group_id": target if kind == "group" else None,
            "user_id": "999" if kind == "group" else target, "account_id": account}


class FriendlyTargetTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_sdk_unwraps_and_checks_current_session_not_first_account(self):
        from maibot_sdk.context import PluginContext
        from maibot_sdk.capabilities.chat import ChatCapability
        async def call(capability, **kwargs):
            self.assertEqual(capability, "chat.get_all_streams")
            self.assertEqual(kwargs, {"platform": "qq"})
            return PluginContext._normalize_capability_result(capability, {"success": True, "streams": [
                host_stream("other-account", account="789"), host_stream()]})
        ctx = types.SimpleNamespace(chat=ChatCapability(types.SimpleNamespace(call_capability=call)))
        self.assertTrue(await target_allowed(ctx, "chat", ((), ("123",), (), "456")))
        self.assertFalse(await target_allowed(ctx, "other-account", ((), ("123",), (), "456")))
        manifest = json.loads(Path(__file__).with_name("_manifest.json").read_text(encoding="utf-8"))
        self.assertIn("chat.get_all_streams", manifest["capabilities"])

    async def test_same_numeric_group_and_private_do_not_cross_authorize(self):
        ctx = types.SimpleNamespace(chat=types.SimpleNamespace(get_all_streams=AsyncMock(return_value=[host_stream(), host_stream("private",kind="private")])))
        self.assertFalse(await target_allowed(ctx, "private", ((), ("123",), (), "")))
        self.assertTrue(await target_allowed(ctx, "private", ((), (), ("123",), "456")))
        self.assertFalse(await target_allowed(ctx, "chat", ((), (), ("123",), "")))

    async def test_invalid_numbers_account_or_empty_list_never_lookup(self):
        ctx=types.SimpleNamespace(chat=types.SimpleNamespace(get_all_streams=AsyncMock()))
        for policy in [((),(),(),""), ((),("123",)*17,(),""), ((),(" 123",),(),""),
                       ((),("0123",),(),""), ((),("qq:123",),(),""), ((),("123",),(),"bad")]:
            self.assertFalse(await target_allowed(ctx,"chat",policy))
        ctx.chat.get_all_streams.assert_not_called()
        self.assertTrue(await target_allowed(ctx,"legacy",(("legacy",),("bad",),(),"")))

    async def test_bad_shape_oversize_duplicate_and_wrong_metadata_fail_closed(self):
        ctx=types.SimpleNamespace(chat=types.SimpleNamespace(get_all_streams=AsyncMock()))
        for rows in [None, {"success":False}, {"streams":[host_stream()]}, [host_stream()]*513,
                     [host_stream(),host_stream()], [{**host_stream(),"platform":"other"}],
                     [{**host_stream(),"stream_id":"other"}], [{**host_stream(),"is_group_session":False}],
                     [{**host_stream(),"account_id":"789"}]]:
            ctx.chat.get_all_streams.return_value=rows
            self.assertFalse(await target_allowed(ctx,"chat",((),("123",),(),"456")))

    async def test_no_cross_request_cache_and_lookup_exception(self):
        method=AsyncMock(return_value=[host_stream()]);ctx=types.SimpleNamespace(chat=types.SimpleNamespace(get_all_streams=method))
        self.assertTrue(await target_allowed(ctx,"chat",((),("123",),(),"456")))
        method.return_value=[]
        self.assertFalse(await target_allowed(ctx,"chat",((),("123",),(),"456")))
        method.side_effect=RuntimeError("synthetic private error")
        self.assertFalse(await target_allowed(ctx,"chat",((),("123",),(),"456")))
        self.assertEqual(method.await_count,3)

    async def test_timeout_denies_and_cancellation_propagates(self):
        ctx=types.SimpleNamespace(chat=types.SimpleNamespace(get_all_streams=AsyncMock(side_effect=TimeoutError())))
        self.assertFalse(await target_allowed(ctx,"chat",((),("123",),(),"456")))
        ctx.chat.get_all_streams.side_effect=asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await target_allowed(ctx,"chat",((),("123",),(),"456"))


def packet(body, nonce=None):
    import hashlib
    import hmac
    import time

    value = {
        "v": 1,
        "client": "neko.life",
        "audience": "neko.draw",
        "issued": int(time.time()),
        "operation": "submit",
        "body": body,
        "nonce": nonce or uuid.uuid4().hex,
    }
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    value["signature"] = hmac.new(
        bytes.fromhex("ab" * 32), raw, hashlib.sha256
    ).hexdigest()
    return value


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        p = Path(self.temp.name, "life-keys.json")
        p.write_text(json.dumps({"neko.life": "ab" * 32}), encoding="utf-8")
        p.chmod(0o600)
        self.body = {
            "session_id": "chat",
            "request_id": "12" * 16,
            "prompt": "窗边书桌环境插画",
        }

    def test_signed_request_binds_target_and_rejects_tampering(self):
        value = packet(self.body)
        self.assertEqual(authenticate(value, self.temp.name)[0], self.body)
        value["body"] = {**self.body, "session_id": "other"}
        with self.assertRaises(ValueError):
            authenticate(value, self.temp.name)

    def test_durable_replay_rejects_and_new_nonce_returns_existing_request(self):
        nonce = uuid.uuid4().hex
        self.assertTrue(consume(self.temp.name, self.body, nonce))
        with self.assertRaisesRegex(ValueError, "replay"):
            consume(self.temp.name, self.body, nonce)
        self.assertFalse(consume(self.temp.name, self.body, uuid.uuid4().hex))
        with self.assertRaisesRegex(ValueError, "conflict"):
            consume(
                self.temp.name, {**self.body, "session_id": "other"}, uuid.uuid4().hex
            )

    def test_expired_auth_and_corrupt_ledger_fail_closed(self):
        value = packet(self.body)
        value["issued"] -= 121
        with self.assertRaises(ValueError):
            authenticate(value, self.temp.name)
        p = Path(self.temp.name, "life-admission.db")
        p.write_bytes(b"corrupt fixture")
        with self.assertRaises(sqlite3.DatabaseError):
            consume(self.temp.name, self.body, uuid.uuid4().hex)
        self.assertEqual(p.read_bytes(), b"corrupt fixture")


@unittest.skipUnless(importlib.util.find_spec("maibot_sdk"), "SDK required")
class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = Path(__file__).parent
        pkg = types.ModuleType("draw_life_test")
        pkg.__path__ = [str(root)]
        sys.modules[pkg.__name__] = pkg
        spec = importlib.util.spec_from_file_location(
            pkg.__name__ + ".plugin", root / "plugin.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        directory = self.temp.name

        class Bot(mod.NekoDraw):
            config = mod.PluginConfig()
            ctx = types.SimpleNamespace(
                paths=types.SimpleNamespace(data_dir=directory), logger=Mock()
            )

        self.bot = Bot()
        self.addAsyncCleanup(self.bot.on_unload)
        self.bot.config.life.enabled = True
        self.bot.config.life.sessions = ["chat"]
        self.bot._generate = AsyncMock(return_value=(b"fake-image", "fake-provider"))
        self.bot._send = AsyncMock()
        p = Path(directory, "life-keys.json")
        p.write_text(json.dumps({"neko.life": "ab" * 32}), encoding="utf-8")
        p.chmod(0o600)
        self.body = {
            "session_id": "chat",
            "request_id": "34" * 16,
            "prompt": "合成场景插画",
        }

    async def test_real_execution_boundary_deduplicates_and_reload_does_not_replay(
        self,
    ):
        result = await self.bot.life_v1(packet=packet(self.body))
        self.assertEqual(result["status"], "submitted")
        await asyncio.gather(*list(self.bot._runners.values()))
        self.bot._generate.assert_awaited_once()
        self.bot._send.assert_awaited_once()
        result = await self.bot.life_v1(packet=packet(self.body))
        self.assertEqual(result["status"], "succeeded")
        self.bot._tasks.clear()
        self.assertEqual(
            (await self.bot.life_v1(packet=packet(self.body)))["status"], "unknown"
        )
        self.bot._generate.assert_awaited_once()

    async def test_disabled_wrong_session_and_forged_call_cannot_generate(self):
        self.bot.config.life.enabled = False
        self.assertEqual(
            (await self.bot.life_v1(packet=packet(self.body)))["status"], "disabled"
        )
        self.bot.config.life.enabled = True
        self.assertEqual(
            (
                await self.bot.life_v1(
                    packet=packet({**self.body, "session_id": "other"})
                )
            )["status"],
            "session_denied",
        )
        self.assertEqual(
            (await self.bot.life_v1(packet={"caller_plugin_id": "neko.life"}))[
                "status"
            ],
            "admission_rejected",
        )
        self.bot._generate.assert_not_awaited()

    def friendly(self):
        self.bot.config.life.sessions=[]
        self.bot.config.life.group_ids=["123"]
        self.bot.config.life.account_id="456"
        self.bot.ctx.chat=types.SimpleNamespace(get_all_streams=AsyncMock(return_value=[host_stream()]))

    async def test_friendly_group_generates_once_and_reload_keeps_unknown(self):
        self.friendly()
        self.assertEqual((await self.bot.life_v1(packet=packet(self.body)))["status"],"submitted")
        await asyncio.gather(*list(self.bot._runners.values()))
        self.bot._tasks.clear()
        self.assertEqual((await self.bot.life_v1(packet=packet(self.body)))["status"],"unknown")
        self.bot._generate.assert_awaited_once()

    async def test_friendly_wrong_account_and_revoked_target_never_consume(self):
        self.friendly()
        self.bot.config.life.account_id="789"
        self.assertEqual((await self.bot.life_v1(packet=packet(self.body)))["status"],"session_denied")
        self.bot.config.life.account_id="456";self.bot.config.life.group_ids=[]
        self.assertEqual((await self.bot.life_v1(packet=packet(self.body)))["status"],"session_denied")
        self.assertFalse(Path(self.temp.name,"life-admission.db").exists())
        self.bot._generate.assert_not_awaited()

    async def test_revocation_during_host_lookup_cancels_without_consuming(self):
        self.friendly()
        async def lookup(**kwargs):
            self.bot.config.life.group_ids.clear()
            return [host_stream()]
        self.bot.ctx.chat.get_all_streams.side_effect=lookup
        self.assertEqual((await self.bot.life_v1(packet=packet(self.body)))["status"],"cancelled")
        self.assertFalse(Path(self.temp.name,"life-admission.db").exists())
        self.bot._generate.assert_not_awaited()

    async def test_revocation_during_durable_admission_does_not_start_generation(self):
        from unittest.mock import patch
        self.friendly();original=asyncio.to_thread
        async def revoke(*args):
            result=await original(*args)
            self.bot.config.life.account_id="789"
            return result
        with patch("draw_life_test.plugin.asyncio.to_thread",side_effect=revoke):
            self.assertEqual((await self.bot.life_v1(packet=packet(self.body)))["status"],"cancelled")
        self.assertTrue(Path(self.temp.name,"life-admission.db").exists())
        self.bot._generate.assert_not_awaited()

    async def test_friendly_private_target_uses_separate_whitelist(self):
        self.friendly();self.bot.config.life.group_ids=[];self.bot.config.life.user_ids=["123"]
        self.bot.ctx.chat.get_all_streams.return_value=[host_stream(kind="private")]
        self.assertEqual((await self.bot.life_v1(packet=packet(self.body)))["status"],"submitted")
        await asyncio.gather(*list(self.bot._runners.values()))
        self.bot._generate.assert_awaited_once()

    async def test_forged_request_cannot_query_host_or_generate(self):
        self.friendly()
        self.assertEqual((await self.bot.life_v1(packet={"caller_plugin_id":"neko.life"}))["status"],"admission_rejected")
        self.bot.ctx.chat.get_all_streams.assert_not_called()
        self.bot._generate.assert_not_awaited()

    async def test_unload_during_admission_does_not_start_task(self):
        original = asyncio.to_thread

        async def close_during(*args):
            result = await original(*args)
            self.bot._life_closed = True
            return result

        from unittest.mock import patch

        with patch("draw_life_test.plugin.asyncio.to_thread", side_effect=close_during):
            self.assertEqual(
                (await self.bot.life_v1(packet=packet(self.body)))["status"],
                "cancelled",
            )
        self.bot._generate.assert_not_awaited()
