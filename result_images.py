"""Bound API responses and validate generated image bytes before delivery."""
import base64
import io
import warnings

from PIL import Image

MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_PIXELS = 40_000_000


async def read_response(response) -> bytes:
    result = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        if len(result) + len(chunk) > MAX_RESPONSE_BYTES:
            raise ValueError("生图接口响应超过 32 MiB")
        result.extend(chunk)
    return bytes(result)


def validate_image(data: bytes) -> bytes:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("生成图片为空或超过 16 MiB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"}:
                    raise ValueError("生成图片必须是 PNG、JPEG 或 WebP")
                if image.width * image.height > MAX_PIXELS:
                    raise ValueError("生成图片像素数量过大")
                image.verify()
    except (OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("接口返回的图片无效") from exc
    return data


def decode_image(raw: str) -> bytes:
    if len(raw) > (MAX_IMAGE_BYTES + 2) // 3 * 4 + 1024:
        raise ValueError("生成图片 Base64 超过大小限制")
    return validate_image(base64.b64decode("".join(raw.split()), validate=True))
