"""Network-free regressions for the downloader shared with neko-web."""

import unittest
from unittest.mock import patch

import httpx

from public_http import PublicClient, PublicFetchError, download_public, validate_url


class PublicDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_pinned_ip_preserves_host_and_tls_name(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, content=b"image")

        with patch("public_http.default_resolver", return_value=["1.1.1.1"]) as resolve:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                data, _, final = await download_public(client, "https://cdn.test/a")
        resolve.assert_called_once_with("cdn.test")
        self.assertEqual(data, b"image")
        self.assertEqual(final, "https://cdn.test/a")
        self.assertEqual(requests[0].url.host, "1.1.1.1")
        self.assertEqual(requests[0].headers["Host"], "cdn.test")
        self.assertEqual(requests[0].extensions["sni_hostname"], "cdn.test")

    async def test_empty_mixed_private_mapped_and_local_dns_never_send(self):
        def handler(request):
            self.fail("request sent before DNS validation")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            for addresses in ([], ["1.1.1.1", "127.0.0.1"], ["::ffff:10.0.0.1"], ["1.1.1.1"]):
                with self.subTest(addresses=addresses), self.assertRaises(PublicFetchError):
                    await download_public(client, "https://cdn.test/a", resolver=lambda host: addresses,
                                          blocked_ips={"1.1.1.1"})

    async def test_relative_redirect_revalidates_and_keeps_original_hostname(self):
        seen = []
        resolves = []

        def resolve(host):
            resolves.append(host)
            return ["1.1.1.1"]

        def handler(request):
            seen.append(request)
            return (httpx.Response(302, headers={"location": "/b"}) if len(seen) == 1
                    else httpx.Response(200, content=b"image"))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            _, _, final = await download_public(client, "https://cdn.test/a", resolver=resolve)
        self.assertEqual(final, "https://cdn.test/b")
        self.assertEqual(resolves, ["cdn.test", "cdn.test"])
        self.assertTrue(all(r.url.host == "1.1.1.1" and r.headers["Host"] == "cdn.test" for r in seen))

    async def test_redirect_to_private_or_mixed_dns_never_follows(self):
        for target in ("http://169.254.169.254/latest", "https://evil.test/a", "http://user:pass@cdn.test/a"):
            seen = []

            def handler(request):
                seen.append(request)
                return httpx.Response(302, headers={"location": target})

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(PublicFetchError):
                    await download_public(client, "https://cdn.test/a", resolver=lambda host:
                                          ["1.1.1.1"] if host == "cdn.test" else ["1.1.1.1", "10.0.0.1"])
            self.assertEqual(len(seen), 1)

    async def test_rebinding_dns_on_redirect_is_rejected(self):
        with patch("public_http.default_resolver", side_effect=[["1.1.1.1"], ["127.0.0.1"]]) as resolve:
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda request: httpx.Response(302, headers={"location": "/next"}))) as client:
                with self.assertRaises(PublicFetchError):
                    await download_public(client, "https://cdn.test/a")
        self.assertEqual(resolve.call_count, 2)

    async def test_redirect_limit_and_missing_location(self):
        for headers, calls in (({"location": "/loop"}, 4), ({}, 1)):
            seen = []

            def handler(request):
                seen.append(request)
                return httpx.Response(302, headers=headers)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(PublicFetchError):
                    await download_public(client, "https://cdn.test/a", resolver=lambda host: ["1.1.1.1"])
            self.assertEqual(len(seen), calls)

    async def test_streamed_response_limits_ignore_small_content_length(self):
        class Chunks(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"123"
                yield b"456"

        for content_type, limits in (("image/png", {"max_image_bytes": 5}),
                                     ("text/html", {"page_bytes": 5})):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
                    200, headers={"content-type": content_type, "content-length": "1"}, stream=Chunks()))) as client:
                with self.assertRaisesRegex(PublicFetchError, "大小限制"):
                    await download_public(client, "https://cdn.test/a", resolver=lambda host: ["1.1.1.1"], **limits)

    async def test_proxy_transport_uses_ip_and_original_tls_name(self):
        class Content:
            async def iter_chunked(self, size):
                yield b"image"

        class Response:
            status = 200
            headers = {}
            content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        captured = {}

        class Session:
            def request(self, method, url, **kwargs):
                captured.update(url=url, **kwargs)
                return Response()

        client = PublicClient(proxy="http://127.0.0.1:7890")
        client.session = Session()
        await download_public(client, "https://cdn.test/a", resolver=lambda host: ["1.1.1.1"])
        self.assertEqual(captured["url"], "https://1.1.1.1/a")
        self.assertEqual(captured["headers"]["Host"], "cdn.test")
        self.assertEqual(captured["server_hostname"], "cdn.test")
        self.assertIs(captured["ssl"], True)
        self.assertIs(captured["allow_redirects"], False)
        self.assertEqual(captured["proxy"], "http://127.0.0.1:7890")

    def test_unsafe_and_malformed_urls(self):
        for url in ("http://[broken", "https://cdn.test:bad/a", "http://224.0.0.1/",
                    "http://[ff02::1]/", "http://[::ffff:127.0.0.1]/", "http://localhost/",
                    "http://metadata.google.internal/", "https://cdn.test:8443/", "file:///etc/passwd"):
            with self.subTest(url=url):
                self.assertIsNotNone(validate_url(url))
