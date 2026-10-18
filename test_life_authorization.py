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

from life_authorization import authenticate, consume


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
