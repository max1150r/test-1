"""Pruebas de HttpClient con un módulo ``httpx`` falso (sin red, sin depender de httpx instalado)."""
import sys
import types
import unittest
from unittest import mock

from network.proxy_manager import ProxyManager
from utils.config import MonitorCfg, NetworkCfg


class StubHTTPError(Exception): ...
class StubTimeout(StubHTTPError): ...
class StubConnectError(StubHTTPError): ...


def make_stub(script):
    """script: lista de acciones por petición: (status, text, headers) o Exception."""
    httpx = types.ModuleType("httpx")

    class Response:
        def __init__(self, status, text, headers, url):
            self.status_code, self.text, self.headers, self.url = status, text, headers or {}, url

    class AsyncClient:
        instances = []
        def __init__(self, **kw):
            self.kw = kw
            self.calls = []
            AsyncClient.instances.append(self)
        async def get(self, url):
            self.calls.append(url)
            action = script.pop(0)
            if isinstance(action, Exception):
                raise action
            status, text, headers = action
            return Response(status, text, headers, url)
        async def aclose(self): ...

    httpx.HTTPError, httpx.TimeoutException, httpx.ConnectError = StubHTTPError, StubTimeout, StubConnectError
    httpx.Timeout = lambda *a, **k: ("timeout", a, k)
    httpx.Limits = lambda **k: ("limits", k)
    httpx.AsyncClient = AsyncClient
    return httpx


BODY = "<html><h1>P</h1>" + "x" * 400 + "</html>"
URL = "https://www.carrefour.es/x/VC4A-1/p"


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def build(self, script, proxies=None, retries=3, **net):
        stub = make_stub(script)
        with mock.patch.dict(sys.modules, {"httpx": stub}):
            from network.client import HttpClient
            c = HttpClient(MonitorCfg(retries=retries, timeout=5, max_requests_per_minute=0),
                           NetworkCfg(retry_backoff_base=0.001, retry_backoff_max=0.002, **net), proxies,
                           lambda st, tx: st in (403, 429) or "captcha" in tx)
        return c, stub

    async def test_success_first_try(self):
        c, _ = self.build([(200, BODY, {})])
        r = await c.fetch(URL)
        self.assertTrue(r.ok)
        self.assertEqual(r.attempts, 1)

    async def test_retries_on_5xx_timeout_and_connect_error_then_succeeds(self):
        c, stub = self.build([(503, "err", {}), StubTimeout(), StubConnectError(), (200, BODY, {})])
        r = await c.fetch(URL)
        self.assertTrue(r.ok)
        self.assertEqual(r.attempts, 4)

    async def test_gives_up_after_retries_without_raising(self):
        c, _ = self.build([(500, "e", {})] * 4, retries=3)
        r = await c.fetch(URL)
        self.assertFalse(r.ok)
        self.assertEqual(r.attempts, 4)
        self.assertEqual(r.error, "HTTP 500")

    async def test_403_and_429_flag_blocked_and_honor_retry_after(self):
        c, _ = self.build([(429, "slow down", {"retry-after": "0"}), (403, "x", {})], retries=1)
        r = await c.fetch(URL)
        self.assertFalse(r.ok)
        self.assertTrue(r.blocked)

    async def test_captcha_page_is_blocked_even_with_200(self):
        c, _ = self.build([(200, "<html>captcha</html>" + "x" * 300, {})], retries=0)
        r = await c.fetch(URL)
        self.assertTrue(r.blocked and not r.ok)

    async def test_404_not_retried(self):
        c, stub = self.build([(404, "nf", {}), (200, BODY, {})])
        r = await c.fetch(URL)
        self.assertEqual((r.ok, r.status_code, r.attempts), (False, 404, 1))

    async def test_empty_body_is_anomalous(self):
        c, _ = self.build([(200, "", {})], retries=0)
        r = await c.fetch(URL)
        self.assertFalse(r.ok)
        self.assertIn("anómala", r.error)

    async def test_proxy_rotation_on_block_and_cooldown(self):
        pm = ProxyManager(cooldown=100)
        pm.load_lines(["10.0.0.1:80", "10.0.0.2:80"])
        c, stub = self.build([(429, "x", {}), (200, BODY, {})], proxies=pm)
        r = await c.fetch(URL)
        self.assertTrue(r.ok)
        self.assertEqual(pm.stats()["disabled"], 1)          # el que recibió 429 queda en cooldown
        self.assertEqual(len(stub.AsyncClient.instances), 2)  # una sesión por proxy
        proxies_used = [i.kw["proxy"] for i in stub.AsyncClient.instances]
        self.assertEqual(sorted(proxies_used), ["http://10.0.0.1:80", "http://10.0.0.2:80"])

    async def test_all_proxies_down_does_not_fall_back_to_direct(self):
        pm = ProxyManager(cooldown=100)
        pm.load_lines(["10.0.0.1:80"])
        c, stub = self.build([(403, "x", {})], proxies=pm, retries=2)
        r = await c.fetch(URL)
        self.assertFalse(r.ok)
        self.assertIn("proxies", r.error)
        self.assertEqual(len(stub.AsyncClient.instances), 1)  # nunca se creó una sesión directa

    async def test_tls_verification_not_disabled(self):
        c, stub = self.build([(200, BODY, {})])
        await c.fetch(URL)
        self.assertNotIn("verify", stub.AsyncClient.instances[0].kw)

    async def test_close_is_idempotent(self):
        c, _ = self.build([(200, BODY, {})])
        await c.fetch(URL)
        await c.aclose()
        await c.aclose()
        r = await c.fetch(URL)
        self.assertFalse(r.ok)


if __name__ == "__main__":
    unittest.main()
