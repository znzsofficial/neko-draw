# -*- coding: utf-8 -*-
"""只用 OpenAI 兼容接口和 Gemini 的 MaiBot 生图插件。"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Tuple
from urllib.parse import urlsplit

import aiohttp

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType


MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*]\(([^)\s]+)\)")
DATA_URL = re.compile(r"data:image/[\w.+-]+;base64,([A-Za-z0-9+/=\s]+)", re.IGNORECASE)


class PromptRejected(RuntimeError):
    """接口返回 HTTP 400。生图里这通常是提示词违规，不应该重试。"""


class PluginSection(PluginConfigBase):
    __ui_label__ = "插件"

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.1.0", description="配置版本")


class GeneralConfig(PluginConfigBase):
    __ui_label__ = "通用"

    provider: Literal["openai", "gemini"] = Field(default="openai", description="优先使用的接口")
    fallback_provider: Literal["none", "openai", "gemini"] = Field(default="gemini", description="失败后改试的接口。none 表示不备选")
    timeout: int = Field(default=180, ge=10, le=300, description="单次请求超时秒数")
    retry_times: int = Field(default=2, ge=0, le=5, description="同一个接口额外重试次数。0 表示不重试")
    retry_delay_seconds: float = Field(default=2.0, ge=0.5, le=30.0, description="首次重试前等待秒数，之后翻倍，最多 30 秒")


class CommandConfig(PluginConfigBase):
    __ui_label__ = "测试命令"

    allowed_user_ids: List[str] = Field(
        default_factory=list,
        description="允许使用 /draw 的 QQ 号。留空时命令不生效",
    )


class OpenAIConfig(PluginConfigBase):
    __ui_label__ = "OpenAI"

    enabled: bool = Field(default=True, description="是否启用")
    base_url: str = Field(default="https://api.openai.com/v1", description="兼容接口地址")
    api_key: str = Field(default="", description="API Key")
    model: str = Field(default="gpt-image-2", description="生图模型")
    size: str = Field(default="1024x1024", description="图片尺寸，例如 1024x1024")
    mode: Literal["auto", "images", "chat"] = Field(
        default="auto",
        description="auto=官方生图接口失败后再试聊天补全",
    )
    proxy: str = Field(default="", description="HTTP 代理。国内中转留空")


class GeminiConfig(PluginConfigBase):
    __ui_label__ = "Gemini"

    enabled: bool = Field(default=False, description="是否启用")
    base_url: str = Field(default="https://generativelanguage.googleapis.com", description="Gemini 或兼容网关地址")
    api_key: str = Field(default="", description="API Key")
    model: str = Field(default="gemini-3.1-flash-image-preview", description="图片模型")
    aspect_ratio: str = Field(default="1:1", description="宽高比，例如 1:1 或 16:9")
    mode: Literal["native", "openai"] = Field(
        default="native",
        description="native=generateContent；openai=把地址当 OpenAI 兼容网关",
    )
    proxy: str = Field(default="http://127.0.0.1:7890", description="访问 Google 用的代理")


class PluginConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    command: CommandConfig = Field(default_factory=CommandConfig)
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)
    gemini: GeminiConfig = Field(default_factory=GeminiConfig)


class DrawTask:
    """一次生图的进度。只放在内存里，重启后清空。"""

    def __init__(self, task_id: str, stream_id: str, prompt: str, user_id: str) -> None:
        now = time.time()
        self.task_id = task_id
        self.stream_id = stream_id
        self.prompt = prompt
        self.user_id = user_id
        self.status = "queued"
        self.attempt = 0
        self.max_attempts = 1
        self.provider = ""
        self.error = ""
        self.started = now
        self.updated = now

    def touch(self, status: str, **fields: Any) -> None:
        self.status = status
        self.updated = time.time()
        for key, value in fields.items():
            setattr(self, key, value)

    def summary(self) -> str:
        labels = {
            "queued": "排队",
            "running": "生成中",
            "retrying": "重试中",
            "succeeded": "已发送",
            "failed": "失败",
        }
        lines = [
            f"任务 {self.task_id}",
            f"状态：{labels.get(self.status, self.status)}（第 {max(self.attempt, 1)}/{self.max_attempts} 次）",
            f"提示词：{self.prompt[:80]}",
        ]
        if self.provider:
            lines.append(f"接口：{self.provider}")
        if self.error:
            lines.append(f"原因：{self.error[:180]}")
        return "\n".join(lines)


class NekoDraw(MaiBotPlugin):
    """文生图。OpenAI 不稳定时自动改走 Gemini。"""

    config_model = PluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._session: Optional[aiohttp.ClientSession] = None
        self._tasks: Dict[str, DrawTask] = {}
        self._latest_by_stream: Dict[str, str] = {}
        self._runners: Dict[str, asyncio.Task] = {}
        self._policy_notice: Dict[str, str] = {}
        self._preferred_openai_mode = ""

    async def on_load(self) -> None:
        if not self.config.plugin.enabled:
            self.ctx.logger.info("生图插件已禁用")
            return
        self.ctx.logger.info(
            "生图插件已加载，优先=%s，备选=%s",
            self.config.general.provider,
            self.config.general.fallback_provider,
        )

    async def on_unload(self) -> None:
        for runner in self._runners.values():
            runner.cancel()
        self._runners.clear()
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        del scope, config_data
        await self.on_unload()
        self._preferred_openai_mode = ""
        self.ctx.logger.info("生图配置已热重载：version=%s", version)

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.config.general.timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    @staticmethod
    def _root(url: str) -> str:
        normalized = url.strip().rstrip("/")
        if normalized.endswith("/v1"):
            return normalized[:-3].rstrip("/")
        return normalized

    async def _post_json(self, url: str, payload: Dict[str, Any], headers: Dict[str, str], proxy: str) -> Dict[str, Any]:
        session = await self._http()
        request_proxy = proxy.strip() or None
        async with session.post(url, json=payload, headers=headers, proxy=request_proxy) as response:
            text = await response.text()
            if response.status == 400:
                raise PromptRejected(f"HTTP 400：{text[:500]}")
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}：{text[:500]}")
            try:
                data = await response.json(content_type=None)
            except Exception as exc:
                raise RuntimeError("接口返回了非 JSON") from exc
        if not isinstance(data, dict):
            raise RuntimeError("接口返回的 JSON 不是对象")
        return data

    async def _download(self, url: str, proxy: str) -> bytes:
        session = await self._http()
        async with session.get(url, proxy=proxy.strip() or None) as response:
            body = await response.read()
            if response.status != 200 or len(body) < 32:
                raise RuntimeError(f"下载图片失败：HTTP {response.status}")
            return body

    async def _bytes_from_reference(self, value: str, proxy: str) -> Optional[bytes]:
        text = value.strip()
        if not text:
            return None
        data_match = DATA_URL.search(text)
        if data_match:
            return base64.b64decode(re.sub(r"\s+", "", data_match.group(1)))
        if text.startswith("http://") or text.startswith("https://"):
            return await self._download(text, proxy)
        compact = re.sub(r"\s+", "", text)
        if len(compact) > 200 and re.fullmatch(r"[A-Za-z0-9+/=]+", compact):
            try:
                decoded = base64.b64decode(compact, validate=True)
            except Exception:
                return None
            if decoded.startswith(b"\x89PNG") or decoded.startswith(b"\xff\xd8") or decoded.startswith(b"RIFF"):
                return decoded
        return None

    async def _images_from_openai_body(self, body: Dict[str, Any], proxy: str) -> List[bytes]:
        images: List[bytes] = []
        data = body.get("data")
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                raw = item.get("b64_json") or item.get("url")
                if isinstance(raw, str):
                    parsed = await self._bytes_from_reference(raw, proxy)
                    if parsed:
                        images.append(parsed)
        if images:
            return images

        choices = body.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message")
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                blobs = [content] if isinstance(content, str) else []
                if isinstance(content, list):
                    blobs.extend(content)
                extra = message.get("images")
                if isinstance(extra, list):
                    blobs.extend(extra)
                for blob in blobs:
                    images.extend(await self._images_from_content(blob, proxy))
        if not images:
            raise RuntimeError("响应里没有图片")
        return images

    async def _images_from_content(self, item: Any, proxy: str) -> List[bytes]:
        if isinstance(item, str):
            found: List[bytes] = []
            references = MARKDOWN_IMAGE.findall(item) or [item]
            for reference in references:
                parsed = await self._bytes_from_reference(reference, proxy)
                if parsed:
                    found.append(parsed)
            return found
        if not isinstance(item, dict):
            return []
        for key in ("b64_json", "url", "image_url", "data"):
            value = item.get(key)
            if isinstance(value, dict):
                value = value.get("url") or value.get("data")
            if isinstance(value, str):
                parsed = await self._bytes_from_reference(value, proxy)
                if parsed:
                    return [parsed]
        return []

    async def _openai_images(self, prompt: str, cfg: OpenAIConfig) -> bytes:
        if not cfg.api_key.strip():
            raise RuntimeError("未配置 OpenAI API Key")
        root = self._root(cfg.base_url)
        headers = {"Authorization": f"Bearer {cfg.api_key.strip()}", "Content-Type": "application/json"}
        payload: Dict[str, Any] = {"model": cfg.model.strip(), "prompt": prompt, "n": 1}
        if cfg.size.strip():
            payload["size"] = cfg.size.strip()
        if "gpt-image" not in cfg.model.lower():
            payload["response_format"] = "b64_json"
        body = await self._post_json(f"{root}/v1/images/generations", payload, headers, cfg.proxy)
        return (await self._images_from_openai_body(body, cfg.proxy))[0]

    async def _openai_chat(self, prompt: str, cfg: OpenAIConfig) -> bytes:
        if not cfg.api_key.strip():
            raise RuntimeError("未配置 OpenAI API Key")
        root = self._root(cfg.base_url)
        headers = {"Authorization": f"Bearer {cfg.api_key.strip()}", "Content-Type": "application/json"}
        payload = {
            "model": cfg.model.strip(),
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        body = await self._post_json(f"{root}/v1/chat/completions", payload, headers, cfg.proxy)
        return (await self._images_from_openai_body(body, cfg.proxy))[0]

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        if isinstance(exc, (asyncio.TimeoutError, aiohttp.ClientError)):
            return True
        text = str(exc)
        return any(code in text for code in ("HTTP 408", "HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504"))

    async def _call_with_retry(self, task: Optional[DrawTask], label: str, func: Any) -> Any:
        attempts = self.config.general.retry_times + 1
        delay = self.config.general.retry_delay_seconds
        if task is not None:
            task.max_attempts = max(task.max_attempts, attempts)
        last: Exception = RuntimeError(f"{label} 没有执行")
        for attempt in range(1, attempts + 1):
            if task is not None:
                task.touch("running" if attempt == 1 else "retrying", attempt=attempt, provider=label)
            try:
                return await func()
            except Exception as exc:
                last = exc
                if not self._retryable(exc) or attempt == attempts:
                    raise
                self.ctx.logger.info("%s 第 %s 次失败，%.1f 秒后重试：%s", label, attempt, delay, str(exc)[:300])
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
        raise last

    async def _openai(self, prompt: str, task: Optional[DrawTask] = None) -> bytes:
        cfg = self.config.openai
        if not cfg.enabled:
            raise RuntimeError("OpenAI 未启用")
        if cfg.mode == "images":
            route_names = ["images"]
        elif cfg.mode == "chat":
            route_names = ["chat"]
        elif self._preferred_openai_mode == "chat":
            route_names = ["chat", "images"]
        else:
            route_names = ["images", "chat"]
        methods = [(name, self._openai_images if name == "images" else self._openai_chat) for name in route_names]
        errors: List[str] = []
        for name, method in methods:
            try:
                image = await self._call_with_retry(task, f"openai/{name}", lambda method=method: method(prompt, cfg))
                self._preferred_openai_mode = name
                return image
            except PromptRejected:
                raise
            except Exception as exc:
                errors.append(str(exc))
                if cfg.mode != "auto":
                    break
        raise RuntimeError("；".join(errors) or "OpenAI 生图失败")

    async def _gemini_native(self, prompt: str) -> bytes:
        cfg = self.config.gemini
        if not cfg.api_key.strip():
            raise RuntimeError("未配置 Gemini API Key")
        root = cfg.base_url.strip().rstrip("/")
        if root.endswith("/v1beta"):
            url = f"{root}/models/{cfg.model.strip()}:generateContent"
        else:
            url = f"{root}/v1beta/models/{cfg.model.strip()}:generateContent"
        host = urlsplit(root).hostname or ""
        headers = {"Content-Type": "application/json"}
        if "googleapis.com" in host:
            headers["x-goog-api-key"] = cfg.api_key.strip()
        else:
            headers["Authorization"] = f"Bearer {cfg.api_key.strip()}"
        payload: Dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        }
        if cfg.aspect_ratio.strip():
            payload["generationConfig"]["imageConfig"] = {"aspectRatio": cfg.aspect_ratio.strip()}
        body = await self._post_json(url, payload, headers, cfg.proxy)
        images: List[bytes] = []
        for candidate in body.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content") or {}
            for part in content.get("parts") or []:
                if not isinstance(part, dict):
                    continue
                inline = part.get("inlineData") or part.get("inline_data") or {}
                raw = inline.get("data") if isinstance(inline, dict) else None
                if isinstance(raw, str) and raw:
                    images.append(base64.b64decode(raw))
        if not images:
            raise RuntimeError("Gemini 响应里没有图片")
        return images[0]

    async def _gemini(self, prompt: str, task: Optional[DrawTask] = None) -> bytes:
        cfg = self.config.gemini
        if not cfg.enabled:
            raise RuntimeError("Gemini 未启用")
        if cfg.mode == "openai":
            mirrored = OpenAIConfig(
                enabled=True,
                base_url=cfg.base_url,
                api_key=cfg.api_key,
                model=cfg.model,
                size="",
                mode="chat",
                proxy=cfg.proxy,
            )
            return await self._call_with_retry(task, "gemini/openai", lambda: self._openai_chat(prompt, mirrored))
        return await self._call_with_retry(task, "gemini", lambda: self._gemini_native(prompt))

    async def _generate(self, prompt: str, task: Optional[DrawTask] = None) -> Tuple[bytes, str]:
        order = [self.config.general.provider]
        fallback = self.config.general.fallback_provider
        if fallback != "none" and fallback not in order:
            order.append(fallback)
        errors: List[str] = []
        for name in order:
            try:
                if name == "openai":
                    return await self._openai(prompt, task), name
                if name == "gemini":
                    return await self._gemini(prompt, task), name
            except PromptRejected:
                raise
            except Exception as exc:
                message = self._redact(str(exc))
                errors.append(f"{name}: {message}")
                self.ctx.logger.warning("生图接口失败：%s", errors[-1][:500])
        raise RuntimeError("；".join(errors) or "没有可用的生图接口")

    async def _send(self, image: bytes, stream_id: str, prompt: str) -> None:
        encoded = base64.b64encode(image).decode("ascii")
        sent = await self.ctx.send.image(
            encoded,
            stream_id,
            processed_plain_text=prompt[:80],
            sync_to_maisaka_history=True,
        )
        if not sent:
            raise RuntimeError("图片没有发送出去")

    def _stream_id(self, kwargs: Dict[str, Any]) -> str:
        return str(kwargs.get("stream_id") or kwargs.get("session_id") or "").strip()

    def _remember_task(self, task: DrawTask) -> None:
        self._tasks[task.task_id] = task
        self._latest_by_stream[task.stream_id] = task.task_id
        if len(self._tasks) > 30:
            oldest = sorted(self._tasks.values(), key=lambda item: item.started)[:-30]
            for item in oldest:
                if item.status in {"queued", "running", "retrying"}:
                    continue
                self._tasks.pop(item.task_id, None)

    def _active_task(self, stream_id: str) -> Optional[DrawTask]:
        task_id = self._latest_by_stream.get(stream_id, "")
        task = self._tasks.get(task_id)
        if task is not None and task.status in {"queued", "running", "retrying"}:
            return task
        return None

    def _status_text(self, stream_id: str) -> str:
        task_id = self._latest_by_stream.get(stream_id, "")
        task = self._tasks.get(task_id)
        if task is None:
            return "这个聊天还没有生图任务"
        return task.summary()

    def _start_task(self, stream_id: str, prompt: str, user_id: str) -> DrawTask:
        active = self._active_task(stream_id)
        if active is not None:
            return active
        task = DrawTask(uuid.uuid4().hex[:8], stream_id, prompt, user_id)
        task.max_attempts = self.config.general.retry_times + 1
        self._policy_notice.pop(stream_id, None)
        self._remember_task(task)
        self._runners[task.task_id] = asyncio.create_task(self._run_task(task))
        return task

    async def _run_task(self, task: DrawTask) -> None:
        try:
            image, provider = await self._generate(task.prompt, task)
            await self._send(image, task.stream_id, task.prompt)
            task.touch("succeeded", provider=provider, error="")
            self.ctx.logger.info("任务 %s 已用 %s 发送", task.task_id, provider)
        except asyncio.CancelledError:
            task.touch("failed", error="插件已卸载，任务取消")
            raise
        except PromptRejected as exc:
            message = self._policy_feedback(task.prompt, str(exc))
            task.touch("failed", error=message)
            self._policy_notice[task.stream_id] = message
            self.ctx.logger.info("任务 %s 被接口以 400 拒绝：%s", task.task_id, message[:300])
        except Exception as exc:
            message = self._redact(str(exc))
            task.touch("failed", error=message)
            self.ctx.logger.error("任务 %s 失败：%s", task.task_id, message[:500])
        finally:
            self._runners.pop(task.task_id, None)

    @HookHandler(
        "maisaka.planner.before_request",
        name="neko_draw_planner",
        description="画图时用自然语言写画面，不要堆标签",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def configure_planner(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        changed = self._consume_policy_notice(kwargs)
        items = kwargs.get("items")
        messages = kwargs.get("messages")
        if self._is_draw_turn(items if isinstance(items, list) else None, messages if isinstance(messages, list) else None):
            instruction = self._planner_instruction()
            draw_changed = False
            if isinstance(items, list) and instruction not in "\n".join(self._item_text(item) for item in items):
                items.append(self._system_item(instruction))
                kwargs["items"] = items
                draw_changed = True
            if isinstance(messages, list) and instruction not in "\n".join(self._message_text(message) for message in messages):
                messages.append({"role": "system", "content": instruction})
                kwargs["messages"] = messages
                draw_changed = True
            if draw_changed:
                changed = True
                self.ctx.logger.info("画画这轮已要求：画自己先查长期记忆，再用完整句子写画面")
        if not changed:
            return None
        return {"action": "continue", "modified_kwargs": kwargs}

    def _redact(self, message: str) -> str:
        for secret in (self.config.openai.api_key.strip(), self.config.gemini.api_key.strip()):
            if secret:
                message = message.replace(secret, "***")
        return message

    @staticmethod
    def _api_error_detail(raw: str) -> str:
        body = raw.split("：", 1)[-1].strip()
        try:
            data = json.loads(body)
        except Exception:
            return ""
        if not isinstance(data, dict):
            return ""
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or "").strip()
        if isinstance(error, str):
            return error.strip()
        return str(data.get("message") or "").strip()

    def _policy_feedback(self, prompt: str, raw: str) -> str:
        detail = self._api_error_detail(self._redact(raw))
        lines = [
            "生图接口返回 HTTP 400，一般是提示词违规，这张没有画成。",
            "不要用同一句提示词再试。改掉可能违规的内容后，重新调用 neko_draw。",
            f"被拒绝的提示词：{prompt[:180]}",
        ]
        if detail:
            lines.append(f"接口说明：{detail[:180]}")
        return "\n".join(lines)

    def _consume_policy_notice(self, kwargs: Dict[str, Any]) -> bool:
        stream_id = str(kwargs.get("session_id") or kwargs.get("stream_id") or "").strip()
        notice = self._policy_notice.get(stream_id, "")
        if not stream_id or not notice:
            return False
        delivered = False
        items = kwargs.get("items")
        if isinstance(items, list):
            items.append(self._system_item(notice))
            kwargs["items"] = items
            delivered = True
        messages = kwargs.get("messages")
        if isinstance(messages, list):
            messages.append({"role": "system", "content": notice})
            kwargs["messages"] = messages
            delivered = True
        if delivered:
            self._policy_notice.pop(stream_id, None)
            self.ctx.logger.info("已把生图 400 反馈交给规划器：%s", stream_id)
        return delivered

    @staticmethod
    def _message_text(message: Any) -> str:
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or ""))
            return "\n".join(parts)
        return ""

    @staticmethod
    def _item_text(item: Any) -> str:
        if not isinstance(item, dict):
            return ""
        parts = item.get("parts")
        if not isinstance(parts, list):
            return str(item.get("text") or item.get("content") or "")
        chunks: List[str] = []
        for part in parts:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                chunks.append(str(part.get("text") or ""))
        return "\n".join(chunks)

    def _is_draw_turn(self, items: Optional[List[Any]], messages: Optional[List[Any]]) -> bool:
        texts: List[str] = []
        if items:
            user_items = [
                item for item in items if isinstance(item, dict) and item.get("item_type") == "UserMessageItem"
            ]
            texts.extend(self._item_text(item) for item in user_items[-6:])
        if messages:
            texts.extend(self._message_text(message) for message in messages[-8:])
        text = "\n".join(texts)
        return any(word in text for word in ("画", "生图", "画图", "出一张", "自画像", "draw"))

    @staticmethod
    def _system_item(text: str) -> Dict[str, Any]:
        return {
            "item_type": "SystemMessageItem",
            "meta": {
                "item_id": uuid.uuid4().hex,
                "logical_turn_id": None,
                "timestamp": datetime.now().isoformat(),
            },
            "parts": [{"type": "text", "text": text}],
        }

    @staticmethod
    def _planner_instruction() -> str:
        return (
            "这轮有人要求画画。"
            "如果画的是你自己、你的样子或自画像，先调用 query_memory，查询长期记忆里的外貌，包括发色、发型、五官、衣服和配饰。"
            "把查到的具体样子写成一段完整的话，再调用 neko_draw。不要编造记忆里没有的外貌，也不要只看人格设定。"
            "画别人，或用户已经把画面说清楚时，按当前对话写，不必为了画画去查记忆。"
            "prompt 用自然语言的完整句子。不要用逗号把短词串起来，也不要使用 masterpiece、1girl、solo 这类标签。"
        )

    @staticmethod
    def _tag_prompt_feedback() -> str:
        return (
            "提示词是一串逗号短词，不能拿去生图。请改成一段完整的话。"
            "如果画的是你自己，先调用 query_memory 查询长期记忆里的外貌，再把查到的发色、发型、五官和衣服写进句子，然后重新调用 neko_draw。"
        )

    @staticmethod
    def _is_tag_prompt(text: str) -> bool:
        lowered = text.lower()
        if any(marker in lowered for marker in ("masterpiece", "best quality", "1girl", "1boy", "solo", "highres")):
            return True
        normalized = text.replace("，", ",").replace("、", ",")
        parts = [part.strip() for part in normalized.split(",") if part.strip()]
        if len(parts) < 8 or any(len(part) >= 18 for part in parts):
            return False
        return (sum(len(part) for part in parts) / len(parts)) < 12

    @Tool(
        "neko_draw",
        brief_description="根据已经写好的画面描述生成一张图片",
        detailed_description=(
            "用户要求画图时调用。画你自己或自画像时，必须先调用 query_memory 查询长期记忆里的外貌，再把查到的样子写进 prompt。"
            "prompt 必须是一段完整的话，写明发色、发型、五官、衣服、动作和场景。不要编造记忆里没有的外貌。"
            "不要使用 masterpiece、1girl、solo，也不要用逗号把短词串起来。"
        ),
        parameters=[
            ToolParameterInfo(
                name="prompt",
                param_type=ToolParamType.STRING,
                description="一段完整的画面描述。画自己时先查长期记忆，再写入查到的具体外貌，不要堆逗号短词",
                required=True,
            ),
        ],
    )
    async def draw(self, prompt: str = "", **kwargs: Any) -> Dict[str, Any]:
        text = prompt.strip()
        stream_id = self._stream_id(kwargs)
        if not self.config.plugin.enabled:
            return {"success": False, "content": "生图插件未启用"}
        if not text:
            return {"success": False, "content": "提示词是空的"}
        if self._is_tag_prompt(text):
            return {
                "success": False,
                "content": self._tag_prompt_feedback(),
            }
        if not stream_id:
            return {"success": False, "content": "找不到当前聊天"}
        active = self._active_task(stream_id)
        if active is not None:
            return {"success": False, "content": "上一张还没画完。\n" + active.summary()}
        task = self._start_task(stream_id, text, str(kwargs.get("user_id") or ""))
        return {
            "success": True,
            "content": "已经开始画了，画好会直接发到聊天里。可以过一会儿再查状态。\n" + task.summary(),
        }

    @Tool(
        "neko_draw_status",
        brief_description="查看当前聊天最近一次生图的进度",
        detailed_description="用户问画好了没有、生图进度、上一张图的状态时调用。不要用它开始新的绘图。",
        parameters=[],
    )
    async def draw_status(self, **kwargs: Any) -> Dict[str, Any]:
        stream_id = self._stream_id(kwargs)
        if not stream_id:
            return {"success": False, "content": "找不到当前聊天"}
        return {"success": True, "content": self._status_text(stream_id)}

    def _allowed(self, user_id: str) -> bool:
        allowed = {str(item).strip() for item in self.config.command.allowed_user_ids if str(item).strip()}
        return bool(user_id) and user_id in allowed

    @Command(
        "neko_draw_cmd",
        description="手动生图或查看状态",
        pattern=r"^/(?:draw|生图|画图)(?:\s+(?P<payload>[\s\S]+))?$",
    )
    async def cmd_draw(
        self,
        stream_id: str = "",
        user_id: str = "",
        matched_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Tuple[bool, str, bool]:
        sender = str(user_id or kwargs.get("user_id") or "").strip()
        if not self._allowed(sender):
            self.ctx.logger.info("拒绝 /draw：用户 %s 不在白名单", sender or "未知")
            return False, "生图命令未对你开放", True
        if not stream_id:
            return False, "找不到当前聊天", True
        payload = str((matched_groups or {}).get("payload") or "").strip()
        if not payload or payload in {"status", "状态"}:
            return True, self._status_text(stream_id), True
        if self._is_tag_prompt(payload):
            return False, self._tag_prompt_feedback(), True
        active = self._active_task(stream_id)
        if active is not None:
            return False, "上一张还没画完。\n" + active.summary(), True
        task = self._start_task(stream_id, payload, sender)
        return True, "已经开始画了，画好会直接发图。\n" + task.summary(), True


def create_plugin() -> NekoDraw:
    return NekoDraw()
