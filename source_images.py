"""Resolve real images inside one MaiBot chat; never turn descriptions into inputs."""
import base64
import time
from collections import OrderedDict

MAX_BYTES = 10 * 1024 * 1024
MAX_IMAGES = 4


class ImageCache:
    def __init__(self):
        self.entries = OrderedDict()

    def prune(self):
        now = time.monotonic()
        for key, (expires, _) in list(self.entries.items()):
            if expires <= now:
                del self.entries[key]
        total = sum(sum(map(len, images)) for _, images in self.entries.values())
        while self.entries and (total > 32 * 1024 * 1024 or len(self.entries) > 40):
            _, (_, images) = self.entries.popitem(last=False)
            total -= sum(map(len, images))

    def put(self, stream, message):
        mid = str(message.get('message_id') or '')
        images = extract(message)
        if mid and images:
            self.entries[(stream, mid)] = (time.monotonic() + 1800, images)
            self.entries.move_to_end((stream, mid))
        self.prune()

    def get(self, stream, mid):
        self.prune()
        return list(self.entries.get((stream, mid), (0, []))[1])


def mime_type(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'image/webp'
    raise ValueError('源图需要 PNG、JPEG 或 WebP 格式')


def unwrap(value):
    if isinstance(value, list):
        return [m for v in value for m in unwrap(v)]
    if not isinstance(value, dict):
        return []
    if 'message_segments' in value or 'raw_message' in value:
        return [value]
    for key in ('message', 'messages', 'result'):
        if key in value:
            return unwrap(value[key])
    return []


def segments(message):
    return [s for key in ('message_segments', 'raw_message')
            for s in (message.get(key) or []) if isinstance(s, dict)]


def extract(message):
    images = []
    for segment in segments(message):
        if segment.get('type') != 'image':
            continue
        for source in (segment, segment.get('data')):
            if not isinstance(source, dict):
                continue
            raw = next((source.get(k) for k in ('binary_data_base64', 'base64', 'image_base64', 'file_base64') if source.get(k)), '')
            if not isinstance(raw, str) or not raw:
                continue
            if len(raw) > MAX_BYTES * 4 // 3 + 1024:
                raise ValueError('单张源图不能超过 10 MiB')
            if raw.startswith('data:image/'):
                raw = raw.split(',', 1)[-1]
            data = base64.b64decode(''.join(raw.split()), validate=True)
            if len(data) > MAX_BYTES:
                raise ValueError('单张源图不能超过 10 MiB')
            mime_type(data)
            if data not in images:
                images.append(data)
            if len(images) > MAX_IMAGES:
                raise ValueError('一次最多使用 4 张源图')
    return images


async def resolve_images(ctx, stream_id, message_id='', current=None, cache=None):
    seen = set()

    async def lookup(mid, depth=0):
        mid = str(mid or '').strip()
        if not mid or mid in seen or depth > 2:
            return []
        seen.add(mid)
        cached = cache.get(stream_id, mid) if cache else []
        if cached:
            return cached
        result = await ctx.call_capability('message.get_by_id', chat_id=stream_id,
                                          message_id=mid, include_binary_data=True)
        messages = unwrap(result)
        return await collect(messages[0], depth) if messages else []

    async def collect(message, depth=0):
        images = extract(message)
        if cache:
            for image in cache.get(stream_id, str(message.get('message_id') or '')):
                if image not in images:
                    images.append(image)
        refs = [message.get(k) for k in ('reply_to', 'reply_message_id', 'quote_message_id', 'quoted_message_id')]
        for key in ('reply', 'quote', 'quoted_message'):
            ref = message.get(key)
            if isinstance(ref, dict):
                ref = ref.get('message_id') or ref.get('id') or ref.get('target_message_id')
            refs.append(ref)
        for segment in segments(message):
            if segment.get('type') in ('reply', 'quote'):
                data = segment.get('data')
                refs.append((data.get('message_id') or data.get('id') or data.get('target_message_id')) if isinstance(data, dict) else data)
        for ref in refs:
            if isinstance(ref, (str, int)):
                for image in await lookup(ref, depth + 1):
                    if image not in images:
                        images.append(image)
        if len(images) > MAX_IMAGES:
            raise ValueError('一次最多使用 4 张源图')
        return images

    # Explicit IDs must not silently fall back to unrelated recent pictures.
    if message_id:
        images = await lookup(message_id)
    elif isinstance(current, dict):
        images = await collect(current)
        if not images:
            images = await lookup(current.get('message_id'))
    else:
        images = []
    if not images:
        raise ValueError('指定消息或引用里没有可读取的原图。请重新发送图片，并引用它使用 /draw edit <修改要求>')
    return images
