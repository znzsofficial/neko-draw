"""Pinned public HTTP downloads. Vendored identically in neko-web and neko-draw.

Resolve and validate before sending; the HTTP request and proxy CONNECT both use
the validated IP, while Host, TLS SNI and certificate checks use the original name.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from contextlib import asynccontextmanager
from urllib.parse import urljoin, urlparse
from urllib.request import getproxies, proxy_bypass

import httpx


class PublicFetchError(ValueError):
    pass


def canonical_ip(raw: str) -> str:
    ip = ipaddress.ip_address(raw)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return str(ip)


def is_public_ip(raw: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified)
    except ValueError:
        return False


def validate_url(raw: str) -> str | None:
    try:
        if not isinstance(raw, str) or not raw.strip() or len(raw) > 2000:
            return "地址为空或过长"
        if any(ord(c) < 32 for c in raw):
            return "地址包含控制字符"
        parsed = urlparse(raw.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return "只接受 http 或 https 地址"
        if parsed.username is not None or parsed.password is not None:
            return "地址里不能带账号或密码"
        if parsed.port not in {None, 80, 443}:
            return "只接受 80 或 443 端口"
        host = parsed.hostname.lower().rstrip(".")
        if host in {"localhost", "localhost.localdomain", "metadata.google.internal", "metadata.internal"} or host.endswith((".localhost", ".local", ".internal")):
            return "不能获取本机或内网域名"
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None and not is_public_ip(str(ip)):
            return "不能获取内网或本机地址"
        httpx.URL(raw.strip())  # Also reject invalid IDNA/authority syntax.
    except (ValueError, UnicodeError, httpx.InvalidURL):
        return "地址格式无效"
    return None


def default_resolver(host: str) -> list[str]:
    try:
        return list(dict.fromkeys(str(info[4][0]) for info in socket.getaddrinfo(host, None)))
    except OSError as exc:
        raise PublicFetchError("无法解析域名") from exc


def local_addresses() -> set[str]:
    found = {"127.0.0.1", "::1"}
    try:
        found.update(default_resolver(socket.gethostname()))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            found.add(str(sock.getsockname()[0]))
    except (OSError, PublicFetchError):
        pass
    return found


async def download_public(client, url: str, *, resolver=None, blocked_ips=None,
                          max_image_bytes=8 * 1024 * 1024, page_bytes=None):
    current = url.strip()
    blocked = {canonical_ip(ip) for ip in (blocked_ips or ())}
    for _ in range(4):
        reason = validate_url(current)
        if reason:
            raise PublicFetchError(reason)
        original = httpx.URL(current)
        host = original.raw_host.decode("ascii").rstrip(".")
        try:
            addresses = [str(ipaddress.ip_address(host))]
        except ValueError:
            try:
                addresses = await asyncio.wait_for(asyncio.to_thread(resolver or default_resolver, host), 10)
            except (OSError, asyncio.TimeoutError) as exc:
                raise PublicFetchError("无法解析域名") from exc
        if not addresses:
            raise PublicFetchError("无法解析域名")
        for address in addresses:
            if not is_public_ip(address) or canonical_ip(address) in blocked:
                raise PublicFetchError("不能获取内网或本机地址")
        # Neither direct transport nor a proxy gets a domain to resolve again.
        pinned = original.copy_with(host=canonical_ip(addresses[0]))
        try:
            async with client.stream("GET", str(pinned), headers={"Host": original.netloc.decode("ascii")},
                                     extensions={"sni_hostname": host, "public_original_url": str(original)}) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise PublicFetchError("重定向没有目标")
                    try:
                        current = urljoin(str(original), location)
                    except ValueError as exc:
                        raise PublicFetchError("重定向地址无效") from exc
                    continue
                if response.status_code != 200:
                    raise PublicFetchError(f"HTTP {response.status_code}")
                content_type = response.headers.get("content-type", "")
                declared = content_type.split(";", 1)[0].strip().lower()
                limit = max_image_bytes
                if page_bytes is not None and not declared.startswith("image/") and declared not in {"", "application/octet-stream", "binary/octet-stream"}:
                    limit = min(limit, page_bytes)
                result = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(result) + len(chunk) > limit:
                        raise PublicFetchError("文件超过大小限制")
                    result.extend(chunk)
                return bytes(result), content_type, str(original)
        except httpx.HTTPError as exc:
            raise PublicFetchError("下载失败或超时") from exc
    raise PublicFetchError("重定向次数过多")


class PublicClient:
    """Small streaming adapter; no implicit redirects or environment routing."""
    def __init__(self, *, timeout=60, proxy=None, trust_env=False, headers=None):
        self.timeout, self.proxy, self.trust_env, self.headers = timeout, proxy, trust_env, headers
        self.session = None

    async def __aenter__(self):
        import aiohttp
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout), headers=self.headers,
            connector=aiohttp.TCPConnector(force_close=True), trust_env=False,
        )
        return self

    async def __aexit__(self, *args):
        await self.session.close()

    @asynccontextmanager
    async def stream(self, method, url, *, headers, extensions):
        import aiohttp
        original = urlparse(extensions["public_original_url"])
        proxy = self.proxy
        if proxy is None and self.trust_env and not proxy_bypass(original.hostname):
            proxies = getproxies()
            proxy = proxies.get(original.scheme) or proxies.get("all")
        if proxy and urlparse(proxy).scheme not in {"http", "https"}:
            raise PublicFetchError("公网下载仅支持 HTTP/HTTPS 代理，请改用对应的代理端口")
        try:
            async with self.session.request(method, url, headers=headers, proxy=proxy,
                                            allow_redirects=False, server_hostname=extensions["sni_hostname"], ssl=True) as response:
                class StreamResponse:
                    status_code = response.status
                    headers = response.headers

                    def aiter_bytes(self):
                        return response.content.iter_chunked(64 * 1024)
                yield StreamResponse()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise PublicFetchError("下载失败或超时") from exc
