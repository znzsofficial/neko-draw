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

from maibot_sdk import API, Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType
from .source_images import ImageCache, extract, mime_type, resolve_edit_images
from .public_http import PublicClient, download_public, local_addresses
from .result_images import MAX_IMAGE_BYTES, decode_image, read_response, validate_image
from .life_authorization import authenticate as authenticate_life, consume as consume_life
from .life_authorization import target_allowed as life_target_allowed, target_policy as life_target_policy
from .self_reference import read_reference, portrait_prompt, portrait_edit_prompt


MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*]\(([^)\s]+)\)")
DATA_URL = re.compile(r"data:image/[\w.+-]+;base64,([A-Za-z0-9+/=\s]+)", re.IGNORECASE)


class PromptRejected(RuntimeError):
    """接口返回 HTTP 400，不自动重试；具体原因以实际错误说明为准。"""


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


class LifeConfig(PluginConfigBase):
    __ui_label__ = "Life配图授权"

    enabled: bool = Field(default=False, description="允许已配对Life服务自主配图，独立于普通生图工具",
                          json_schema_extra={"label": "允许Life自主配图", "hint": "仍须Life侧允许同一聊天；不影响普通生图工具，也不选择另一套生成模型。"})
    group_ids: List[str] = Field(default_factory=list, max_length=16, description="允许Life配图的QQ群号，最多16项",
                                json_schema_extra={"label": "允许的QQ群号", "hint": "每项纯数字群号；只匹配宿主已有QQ会话，不创建聊天。与高级会话ID取并集，撤权须一并移除旧ID。"})
    user_ids: List[str] = Field(default_factory=list, max_length=16, description="允许Life配图的私聊QQ号，最多16项",
                               json_schema_extra={"label": "允许私聊的QQ号", "hint": "只授权私聊，不授权该用户所在群。与群号分开填写，查询失败拒绝。"})
    account_id: str = Field(default="", max_length=20, description="可选机器人QQ账号限制",
                            json_schema_extra={"label": "高级：限定机器人QQ账号", "hint": "多个机器人时限定原账号，留空不额外限定；不是聊天对象QQ号。"})
    sessions: List[str] = Field(default_factory=list, description="高级兼容：精确宿主会话ID，与群号/QQ号取并集",
                               json_schema_extra={"label": "高级：允许的宿主会话ID", "hint": "日常填群号/QQ号即可；迁移后移除对应旧ID，否则只删号码不能撤权。所有名单都空拒绝。"})


class SelfPortraitConfig(PluginConfigBase):
    __ui_label__ = "自画像参考图"

    enabled: bool = Field(default=False, description="画自己时允许使用管理员配置的本地角色参考图",
                          json_schema_extra={"label": "使用自画像参考图", "hint": "开启后画自己优先参考图，不再先查记忆重建外貌；普通画图和Life无人场景不受影响。"})
    reference_path: str = Field(default="", max_length=4096, description="管理员指定的一张本地PNG/JPEG/WebP静态参考图的绝对路径",
                                json_schema_extra={"label": "本地参考图文件", "hint": "填写图片文件而非目录；最大10MiB/4000万像素。图片作为生图输入交给当前供应商，不在QQ单独发送；不要填写私密文件。"})


class PluginConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    command: CommandConfig = Field(default_factory=CommandConfig)
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)
    gemini: GeminiConfig = Field(default_factory=GeminiConfig)
    self_portrait: SelfPortraitConfig = Field(default_factory=SelfPortraitConfig)
    life: LifeConfig = Field(default_factory=LifeConfig)


class DrawTask:
    """一次生图的进度。只放在内存里，重启后清空。"""

    def __init__(self, task_id: str, stream_id: str, prompt: str, user_id: str) -> None:
        now = time.time()
        self.task_id = task_id
        self.stream_id = stream_id
        self.prompt = prompt
        self.user_id = user_id
        self.status = "queued"
        self.images: List[bytes] = []
        self.attempt = 0
        self.max_attempts = 1
        self.provider = ""
        self.error = ""
        self.started = now
        self.updated = now
        self.terminal_at: Optional[float] = None
        self.terminal_hint_used = False

    def touch(self, status: str, **fields: Any) -> None:
        if status in {'succeeded', 'failed'} and self.status not in {'succeeded', 'failed'}:
            self.terminal_at = time.monotonic()
            self.terminal_hint_used = False
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
    """文生图与图生图，共用后台任务与配置的备选提供商。"""

    config_model = PluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._session: Optional[aiohttp.ClientSession] = None
        self._tasks: Dict[str, DrawTask] = {}
        self._latest_by_stream: Dict[str, str] = {}
        self._runners: Dict[str, asyncio.Task] = {}
        self._preferred_openai_mode = ""
        self._preferred_openai_edit_mode = ""
        self._image_cache = ImageCache()
        self._life_lock = asyncio.Lock()
        self._life_closed = False

    @HookHandler("chat.receive.before_process", name="neko_source_images",
                  description="缓存本聊天入站图片/表情包原图供图生图使用", mode=HookMode.OBSERVE)
    async def cache_source_images(self, message: Any = None, stream_id: str = "", **kwargs: Any) -> None:
        if not self.config.plugin.enabled or not isinstance(message, dict):
            return
        stream = str(stream_id or kwargs.get("session_id") or message.get("session_id") or message.get("chat_id") or message.get("stream_id") or "")
        if stream:
            try:
                images = await asyncio.to_thread(extract, message)
                self._image_cache.put(stream, message, images=images)
            except (ValueError, TypeError):
                self.ctx.logger.debug("入站图片无法缓存，图生图时可尝试按消息 ID 读取")

    async def on_load(self) -> None:
        self._life_closed = False
        if not self.config.plugin.enabled:
            self.ctx.logger.info("生图插件已禁用")
            return
        self.ctx.logger.info(
            "生图插件已加载，优先=%s，备选=%s",
            self.config.general.provider,
            self.config.general.fallback_provider,
        )

    async def on_unload(self) -> None:
        self._life_closed = True
        runners = list(self._runners.values())
        for runner in runners:
            runner.cancel()
        if runners:
            await asyncio.gather(*runners, return_exceptions=True)
        # A task cancelled before its coroutine starts never executes finally.
        for task in self._tasks.values():
            if task.status in {"queued", "running", "retrying"}:
                task.touch("failed", error="插件已卸载，任务取消")
            task.images = []
        self._runners.clear()
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        del scope, config_data
        await self.on_unload()
        self._preferred_openai_mode = ""
        self._preferred_openai_edit_mode = ""
        self.ctx.logger.info("生图配置已热重载：version=%s", version)
        self._life_closed = False

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
            raw = await read_response(response)
            text = raw.decode("utf-8", "replace")
            if response.status == 400:
                raise PromptRejected(f"HTTP 400：{text[:500]}")
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}：{text[:500]}")
            try:
                data = json.loads(raw)
            except Exception as exc:
                raise RuntimeError("接口返回了非 JSON") from exc
        if not isinstance(data, dict):
            raise RuntimeError("接口返回的 JSON 不是对象")
        return data

    async def _download(self, url: str, proxy: str) -> bytes:
        async with PublicClient(timeout=self.config.general.timeout, proxy=proxy.strip() or None) as client:
            body, _, _ = await download_public(client, url, max_image_bytes=MAX_IMAGE_BYTES,
                                                blocked_ips=local_addresses())
        return await asyncio.to_thread(validate_image, body)

    async def _bytes_from_reference(self, value: str, proxy: str) -> Optional[bytes]:
        text = value.strip()
        if not text:
            return None
        if len(text) > 24 * 1024 * 1024:
            raise ValueError("生成图片引用超过大小限制")
        data_match = DATA_URL.search(text)
        if data_match:
            return await asyncio.to_thread(decode_image, data_match.group(1))
        if text.startswith("http://") or text.startswith("https://"):
            return await self._download(text, proxy)
        compact = re.sub(r"\s+", "", text)
        if len(compact) > 200 and re.fullmatch(r"[A-Za-z0-9+/=]+", compact):
            try:
                decoded = await asyncio.to_thread(decode_image, compact)
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
                        return [parsed]
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
                    if images:
                        return images[:1]
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
                    return [parsed]
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

    async def _openai_images(self, prompt: str, cfg: OpenAIConfig, images: Optional[List[bytes]] = None) -> bytes:
        if not cfg.api_key.strip():
            raise RuntimeError("未配置 OpenAI API Key")
        root = self._root(cfg.base_url)
        if images:
            form = aiohttp.FormData()
            form.add_field("model", cfg.model.strip())
            form.add_field("prompt", prompt)
            form.add_field("n", "1")
            if cfg.size.strip():
                form.add_field("size", cfg.size.strip())
            for i, image in enumerate(images):
                mime = mime_type(image)
                form.add_field("image[]", image, filename=f"source-{i}.{mime.split('/')[-1]}", content_type=mime)
            session = await self._http()
            async with session.post(f"{root}/v1/images/edits", data=form,
                                    headers={"Authorization": f"Bearer {cfg.api_key.strip()}"},
                                    proxy=cfg.proxy.strip() or None) as response:
                raw = await read_response(response)
                text = raw.decode("utf-8", "replace")
                if response.status == 400:
                    raise PromptRejected(f"HTTP 400：{text[:500]}")
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}：{text[:500]}")
                body = json.loads(raw)
            return (await self._images_from_openai_body(body, cfg.proxy))[0]
        headers = {"Authorization": f"Bearer {cfg.api_key.strip()}", "Content-Type": "application/json"}
        payload: Dict[str, Any] = {"model": cfg.model.strip(), "prompt": prompt, "n": 1}
        if cfg.size.strip():
            payload["size"] = cfg.size.strip()
        if "gpt-image" not in cfg.model.lower():
            payload["response_format"] = "b64_json"
        body = await self._post_json(f"{root}/v1/images/generations", payload, headers, cfg.proxy)
        return (await self._images_from_openai_body(body, cfg.proxy))[0]

    async def _openai_chat(self, prompt: str, cfg: OpenAIConfig, images: Optional[List[bytes]] = None) -> bytes:
        if not cfg.api_key.strip():
            raise RuntimeError("未配置 OpenAI API Key")
        root = self._root(cfg.base_url)
        headers = {"Authorization": f"Bearer {cfg.api_key.strip()}", "Content-Type": "application/json"}
        payload = {
            "model": cfg.model.strip(),
            "messages": [{"role": "user", "content": (
                [{"type": "text", "text": prompt}] + [
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type(image)};base64,{base64.b64encode(image).decode('ascii')}"}}
                    for image in images
                ] if images else prompt
            )}],
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
        editing = bool(task and task.images)
        preferred = self._preferred_openai_edit_mode if editing else self._preferred_openai_mode
        if cfg.mode == "images":
            route_names = ["images"]
        elif cfg.mode == "chat":
            route_names = ["chat"]
        elif preferred == "chat":
            route_names = ["chat", "images"]
        else:
            route_names = ["images", "chat"]
        methods = [(name, self._openai_images if name == "images" else self._openai_chat) for name in route_names]
        errors: List[str] = []
        for name, method in methods:
            try:
                image = await self._call_with_retry(task, f"openai/{name}", lambda method=method: method(prompt, cfg, task.images if task else None))
                if editing:
                    self._preferred_openai_edit_mode = name
                else:
                    self._preferred_openai_mode = name
                return image
            except PromptRejected:
                raise
            except Exception as exc:
                errors.append(str(exc))
                if cfg.mode != "auto":
                    break
        raise RuntimeError("；".join(errors) or "OpenAI 生图失败")

    async def _gemini_native(self, prompt: str, images: Optional[List[bytes]] = None) -> bytes:
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
            "contents": [{"role": "user", "parts": [{"text": prompt}] + [
                {"inlineData": {"mimeType": mime_type(image), "data": base64.b64encode(image).decode("ascii")}}
                for image in (images or [])
            ]}],
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
                    return await asyncio.to_thread(decode_image, raw)
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
            return await self._call_with_retry(task, "gemini/openai", lambda: self._openai_chat(prompt, mirrored, task.images if task else None))
        return await self._call_with_retry(task, "gemini", lambda: self._gemini_native(prompt, task.images if task else None))

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
        await asyncio.to_thread(validate_image, image)
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
                if self._latest_by_stream.get(item.stream_id) == item.task_id:
                    self._latest_by_stream.pop(item.stream_id, None)

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
            return "当前没有本聊天可核实的内存任务记录；可能尚未创建、已重载或已淘汰。不能据历史提交回执判断仍在排队或已成功。"
        return task.summary()

    @API('life_v1', version='1', public=True, description='签名Life配图任务，只接收独立授权且配对的会话')
    async def life_v1(self, packet=None, **kwargs):
        del kwargs
        async with self._life_lock:
            if self._life_closed or not self.config.plugin.enabled or not self.config.life.enabled:
                return {'success': False, 'status': 'disabled'}
            try:
                body, nonce = authenticate_life(packet, self.ctx.paths.data_dir)
                session = body['session_id']
                policy = life_target_policy(self.config.life)
                if not await life_target_allowed(self.ctx, session, policy):
                    return {'success': False, 'status': 'session_denied'}
                if not self._life_policy_current(policy):
                    return {'success': False, 'status': 'cancelled'}
                fresh = await asyncio.to_thread(consume_life, self.ctx.paths.data_dir, body, nonce)
                task_id = body['request_id']
                if not fresh:
                    task = self._tasks.get(task_id)
                    return {'success': True, 'status': task.status if task else 'unknown', 'task_id': task_id}
                if not self._life_policy_current(policy):
                    return {'success': False, 'status': 'cancelled'}
                if self._active_task(session):
                    return {'success': False, 'status': 'busy'}
                task = DrawTask(task_id, session, body['prompt'], 'neko.life')
                task.max_attempts = self.config.general.retry_times + 1
                self._remember_task(task)
                self._runners[task_id] = asyncio.create_task(self._run_task(task))
                return {'success': True, 'status': 'submitted', 'task_id': task_id}
            except Exception:
                return {'success': False, 'status': 'admission_rejected'}

    def _life_policy_current(self, policy):
        return (not self._life_closed and self.config.plugin.enabled and self.config.life.enabled
                and life_target_policy(self.config.life) == policy)

    def _start_task(self, stream_id: str, prompt: str, user_id: str, images: Optional[List[bytes]] = None) -> DrawTask:
        active = self._active_task(stream_id)
        if active is not None:
            return active
        task = DrawTask(uuid.uuid4().hex[:8], stream_id, prompt, user_id)
        task.images = images or []
        task.max_attempts = self.config.general.retry_times + 1
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
            self.ctx.logger.info("任务 %s 被接口以 400 拒绝：%s", task.task_id, message[:300])
        except Exception as exc:
            message = self._redact(str(exc))
            task.touch("failed", error=message)
            self.ctx.logger.error("任务 %s 失败：%s", task.task_id, message[:500])
        finally:
            task.images = []
            self._runners.pop(task.task_id, None)

    @HookHandler(
        "maisaka.planner.before_request",
        name="neko_draw_planner",
        description="生图路由与自画像记忆提示",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def configure_planner(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        changed = self._inject_task_status(kwargs)
        items = kwargs.get("items")
        messages = kwargs.get("messages")
        if self._is_draw_turn(items if isinstance(items, list) else None, messages if isinstance(messages, list) else None):
            instruction = self._planner_instruction(self.config.self_portrait.enabled)
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
                self.ctx.logger.debug("已注入条件式生图说明（不表示当前有生图任务）")
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
            return str(error.get("message") or error.get("code") or error.get("type") or "").strip()
        if isinstance(error, str):
            return error.strip()
        return str(data.get("message") or "").strip()

    def _policy_feedback(self, prompt: str, raw: str) -> str:
        detail = self._api_error_detail(self._redact(raw))
        lines = [
            "已知事实：生图接口返回 HTTP 400；该任务失败，未收到可发送的新图片。",
            "源图消息 ID 仅用于本地读取原图，不会作为消息 ID 传给生图接口。不能仅凭 HTTP 400 断言源图 ID 错误。",
            "接口没有说明具体原因时，如实告诉用户原因不明，不要自行编造。",
        ]
        if detail:
            lines.append("接口说明：" + detail[:300])
        if detail.lower() in {"", "openai_error", "upstream_error", "invalid_request_error", "bad_request", "unknown_error", "error"}:
            lines.append("这种通用错误有时是提示词被拦截。可以告诉用户，并修改提示词后重新生成。")
        return "\n".join(lines)

    def _inject_task_status(self, kwargs: Dict[str, Any]) -> bool:
        """In-flight truth; terminal correction at most once within 15 minutes.

        Always remove our old system hint, even if no new hint is warranted.
        A consumed hint means prepared for RPC, not delivered/read by the model.
        """
        stream_id = self._stream_id(kwargs)
        marker = "[neko.draw 当前任务状态]"
        items = kwargs.get("items")
        messages = kwargs.get("messages")
        changed = False
        for key, role_key, role_value, read in (
            ('items', 'item_type', 'SystemMessageItem', self._item_text),
            ('messages', 'role', 'system', self._message_text),
        ):
            values = kwargs.get(key)
            if isinstance(values, list):
                fresh = [value for value in values if not (isinstance(value, dict)
                         and value.get(role_key) == role_value and read(value).startswith(marker))]
                if len(fresh) != len(values):
                    kwargs[key] = fresh
                    changed = True
        items, messages = kwargs.get('items'), kwargs.get('messages')
        if not stream_id or not self.config.plugin.enabled:
            return changed
        task_id = self._latest_by_stream.get(stream_id, "")
        task = self._tasks.get(task_id)
        if task is None:
            return changed
        else:
            terminal = task.status in {'succeeded', 'failed'}
            if terminal and (task.terminal_hint_used or task.terminal_at is None
                             or time.monotonic() >= task.terminal_at + 900):
                return changed
            if task.status not in {'queued', 'running', 'retrying', 'succeeded', 'failed'}:
                return changed
            states = {"queued": "已提交，尚未开始", "running": "正在执行", "retrying": "正在重试",
                      "succeeded": "已完成，图片发送接口已确认发送", "failed": "已失败或取消，不在排队，也不会继续执行"}
            state = f"任务 {task.task_id}：{states.get(task.status, '状态未知')}。"
            if task.status == "succeeded":
                state += " 不要再说还在画或等待出图；无需重复发送或仅为宣告完成而刷屏。"
            if task.status == 'failed':
                state += " 详细原因请按需查询 neko_draw_status；不要仅因这条状态重复道歉或重新生成。"
        notice = marker + "\n" + state + "\n这是本聊天在本次规划请求前核实的状态，优先于旧的排队/提交回执；不构成新的生图请求。"
        inserted = False
        if isinstance(items, list):
            fresh = [item for item in items if not (
                isinstance(item, dict) and item.get("item_type") == "SystemMessageItem" and self._item_text(item).startswith(marker))]
            fresh.append(self._system_item(notice))
            kwargs["items"] = fresh
            changed = True
            inserted = True
        if isinstance(messages, list):
            fresh = [message for message in messages if not (
                isinstance(message, dict) and message.get("role") == "system" and self._message_text(message).startswith(marker))]
            fresh.append({"role": "system", "content": notice})
            kwargs["messages"] = fresh
            changed = True
            inserted = True
        if inserted and terminal:
            task.terminal_hint_used = True
        return changed

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
            texts.extend(self._item_text(item) for item in user_items[-1:])
        if messages:
            user_messages = [message for message in messages if isinstance(message, dict) and message.get("role") == "user"]
            texts.extend(self._message_text(message) for message in user_messages[-1:])
        text = "\n".join(texts)
        if re.search(r"(?:不要|别|不用|停止|取消|不需要).{0,8}(?:画|生图|生成图片)", text):
            return False
        return bool(re.search(
            r"(?:^|\s)/(?:draw|生图|画图)\s+|(?:请|帮我|给我|能不能|可以).{0,8}(?:画|生成.{0,3}图)|"
            r"(?:画一[张幅个]|画你自己|画个自己|画一下|生成一[张幅].{0,4}图)|\bdraw\s+(?:me|a|an|yourself)\b",
            text, re.IGNORECASE,
        ))

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
    def _planner_instruction(self_reference_enabled: bool = False) -> str:
        if self_reference_enabled:
            return (
                "仅当当前用户确实要求生成图片时，以下生图说明才适用；历史提及、闲聊和取消不构成任务。"
                "画你自己、你的自拍、以你为主角或自画像时，用neko_draw并设use_self_reference=true，"
                "插件会附上管理员提供的角色参考图，按图生图生成；不必先调用query_memory查外貌，不能凭记忆描述替代已配置参考图。"
                "prompt写本次场景、动作、服装或画风，角色外貌以参考图为主；不要编造图片消息ID或本地文件路径。"
                "修改用户指定聊天原图用neko_edit_image；若该修改需要机器人角色外貌，再设use_self_reference=true，保留聊天原图并追加角色参考，不用参考图替代编辑对象。"
                "用户自己的自拍、其他人物和普通场景不等于机器人自画像，不自动选角色参考。Life无人生活插画用neko_life_draw_scene，不附人物图。"
                "参考图不可用时如实说明，不退回纯文生图或声称已参考。自然语言、标签或混合描述均可。"
            )
        return (
            "仅当当前用户确实要求生成图片时，以下生图说明才适用；历史提及、闲聊和取消请求不构成生图任务。"
            "有原图的修改或重绘使用 neko_edit_image，以原图为依据。"
            "无原图的自画像先调用 query_memory 查询长期记忆中的外貌，再将查到的相关特征用于 neko_draw；未知特征不当作已有记忆，用户要求的新造型作为本次创作设定。"
            "其他文生图按用户要求调用 neko_draw。自然语言、标签或混合描述均可，不要求固定句式或列全画面要素。"
        )

    @Tool(
        "neko_draw",
        brief_description="生成新图；画机器人自己可用预设角色参考图。",
        detailed_description=(
            "普通文生图；修改聊天中指定图片使用neko_edit_image。"
            "已配置参考时，画机器人自己/机器人自拍设use_self_reference=true，插件附角色原图，无需查记忆重建外貌；其他人物或用户自拍不设。"
            "参考不可用明确失败，不改纯文生图冒充；没有配置参考时按当前规划说明处理。"
            "描述所需主体、效果或风格即可，推荐清楚的自然语言，也接受标签、逗号短语及混合表达，无需列全外貌/场景要素。"
        ),
        parameters=[
            ToolParameterInfo(
                name="prompt",
                param_type=ToolParamType.STRING,
                description="本次主体、场景、动作、服装或画风；参考自画像不必重复编造外貌，自然语言/标签均可",
                required=True,
            ),
            ToolParameterInfo(name="use_self_reference", param_type=ToolParamType.BOOLEAN,
                              description="仅画机器人自己时设true；其他人物、用户自拍、无人场景不填。角色图由管理员配置，不接受路径或消息ID。", required=False),
        ],
    )
    async def draw(self, prompt: str = "", use_self_reference: bool = False, **kwargs: Any) -> Dict[str, Any]:
        if not isinstance(prompt, str) or type(use_self_reference) is not bool:
            return {"success": False, "content": "提示词或自画像参考参数无效"}
        text = prompt.strip()
        stream_id = self._stream_id(kwargs)
        if not self.config.plugin.enabled:
            return {"success": False, "content": "生图插件未启用"}
        if not text:
            return {"success": False, "content": "提示词是空的"}
        if not stream_id:
            return {"success": False, "content": "找不到当前聊天"}
        active = self._active_task(stream_id)
        if active is not None:
            return {"success": False, "content": "上一张还没画完。\n" + active.summary()}
        images = None
        if use_self_reference:
            reference = self.config.self_portrait
            settings = (reference.enabled, reference.reference_path)
            if not reference.enabled or not reference.reference_path:
                return {"success": False, "content": "未启用或配置自画像参考图，未提交生图；请管理员配置参考图。"}
            try:
                images = [await asyncio.to_thread(read_reference, reference.reference_path)]
            except Exception:
                return {"success": False, "content": "自画像参考图无法读取、已变化或格式/大小无效，未生成，不退回纯文生图。请管理员检查参考文件。"}
            if not self.config.plugin.enabled or settings != (self.config.self_portrait.enabled, self.config.self_portrait.reference_path):
                return {"success": False, "content": "参考图配置在读取期间已更改，本次未提交。"}
            active = self._active_task(stream_id)
            if active is not None:
                return {"success": False, "content": "上一张还没画完。\n" + active.summary()}
            text = portrait_prompt(text)
        task = self._start_task(stream_id, text, str(kwargs.get("user_id") or ""), images) if images else self._start_task(stream_id, text, str(kwargs.get("user_id") or ""))
        return {
            "success": True,
            "content": ("已附角色参考图，按图生图提交。" if images else "") + "提交成功，不代表已经生成成功。完成后直接发图；以下仅为提交时快照，后续以插件实时状态或 neko_draw_status 为准。\n" + task.summary(),
        }

    @Tool(
        "neko_edit_image",
        brief_description="修改聊天原图；可额外附机器人角色外貌参考。",
        detailed_description=(
            "用户要求修改这张图、换背景、换装、重绘、参考原图时调用。"
            "source_message_id 填当前聊天中图片/表情包或引用消息的真实 ID，也支持上下文短消息编号及工具媒体索引，不能编造。"
            "普通消息不填source_image_index时保留多图/引用；只想修改某张普通图片时填从0开始的source_image_index。上下文短编号/工具媒体索引默认取第0张，索引不用于emoji表情包。动图只取首帧，不修改整段动画。"
            "prompt说明修改与保留内容。需要机器人角色外貌时设use_self_reference=true：聊天原图在前，角色参考追加在最后；参考不替换原图，不把所有人物改成该角色。"
            "默认false不读角色图。最多3张聊天原图加1张参考，超限请用source_image_index明确选原图，不静默丢图。参考或聊天原图不可用均失败，不降级。无需查记忆重建已有视觉依据。"
            "找不到原图就请用户补发，不要改用文生图冒充图生图。"
        ),
        parameters=[
            ToolParameterInfo(name="prompt", param_type=ToolParamType.STRING, description="自然语言的修改要求和保留内容", required=True),
            ToolParameterInfo(name="source_message_id", param_type=ToolParamType.STRING, description="当前聊天中的真实消息ID、短消息编号或工具媒体索引", required=True),
            ToolParameterInfo(name="source_image_index", param_type=ToolParamType.INTEGER, description="可选，指定普通图片在源消息中的索引，从0开始；不用于emoji表情包", required=False),
            ToolParameterInfo(name="use_self_reference", param_type=ToolParamType.BOOLEAN, description="修改涉及机器人角色并需核对外貌时设true；保留聊天原图并在末尾附角色参考。用户自拍/其他人物默认false，不填本地路径。", required=False),
        ],
    )
    async def edit_image(self, prompt: str = "", source_message_id: str = "", source_image_index: int | None = None, use_self_reference: bool = False, **kwargs: Any) -> Dict[str, Any]:
        if not isinstance(prompt, str) or not isinstance(source_message_id, str) or type(use_self_reference) is not bool:
            return {"success": False, "content": "修改要求、原图编号或自参考参数无效"}
        stream_id = self._stream_id(kwargs)
        if not self.config.plugin.enabled or not stream_id or not prompt.strip():
            return {"success": False, "content": "插件未启用、聊天缺失或修改要求为空"}
        if self._active_task(stream_id):
            return {"success": False, "content": "上一张还没画完。\n" + self._status_text(stream_id)}
        try:
            images = await resolve_edit_images(self.ctx, stream_id, source_message_id.strip(), kwargs.get("message"), self._image_cache, index=source_image_index)
        except Exception as exc:
            return {"success": False, "content": "无法取得原图：" + self._redact(str(exc))[:300]}
        source_count = len(images)
        if not images:
            return {"success": False, "content": "没有取得聊天原图，未提交；角色参考不能代替待修改原图。"}
        text = prompt.strip()
        if use_self_reference:
            if source_count > 3:
                return {"success": False, "content": "自参考编辑最多3张聊天原图加1张角色参考；请用source_image_index明确选择原图，未自动丢弃图片。"}
            settings = (self.config.self_portrait.enabled, self.config.self_portrait.reference_path)
            if not settings[0] or not settings[1]:
                return {"success": False, "content": "未启用或配置角色参考图，本次未提交；不会忽略自参考选项。"}
            try:
                reference = await asyncio.to_thread(read_reference, settings[1])
            except Exception:
                return {"success": False, "content": "角色参考图无法读取或无效，本次未提交，不降级为普通编辑；请管理员检查参考图。"}
            if settings != (self.config.self_portrait.enabled, self.config.self_portrait.reference_path):
                return {"success": False, "content": "角色参考配置已更改，本次未提交。"}
            images = [*images, reference]
            text = portrait_edit_prompt(text, source_count)
        if not self.config.plugin.enabled:
            return {"success": False, "content": "插件已禁用，本次未提交。"}
        if self._active_task(stream_id):
            return {"success": False, "content": "上一张还没画完。\n" + self._status_text(stream_id)}
        task = self._start_task(stream_id, text, str(kwargs.get("user_id") or ""), images)
        notice = f"已读取 {source_count} 张聊天原图" + ("，并在最后附1张角色外貌参考" if use_self_reference else "")
        return {"success": True, "content": notice + "并提交图生图，不代表已经生成成功。完成后直接发图；以下仅为提交时快照，后续以插件实时状态或 neko_draw_status 为准。\n" + task.summary()}

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
        parts = payload.split(maxsplit=1)
        if parts[0].lower() in {"edit", "图生图", "改图"}:
            if len(parts) < 2:
                return False, "请引用原图，发送 /draw edit <修改要求>", True
            result = await self.edit_image(prompt=parts[1], stream_id=stream_id, user_id=sender, **kwargs)
            return bool(result["success"]), str(result["content"]), True
        active = self._active_task(stream_id)
        if active is not None:
            return False, "上一张还没画完。\n" + active.summary(), True
        task = self._start_task(stream_id, payload, sender)
        return True, "已经开始画了，画好会直接发图。\n" + task.summary(), True


def create_plugin() -> NekoDraw:
    return NekoDraw()
