"""Capa HTTP única: sesiones persistentes, proxies, reintentos con backoff y rate limiting.

No intenta resolver CAPTCHAs ni retos de seguridad: si la web los muestra, el resultado se
marca como ``blocked`` y se aplica backoff / cambio de proxy.
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Callable

from monitor.models import FetchResult
from network.proxy_manager import Proxy, ProxyManager
from utils.config import MonitorCfg, NetworkCfg
from utils.logger import log

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

BlockDetector = Callable[[int, str], bool]


class RateLimiter:
    """Limita el número global de peticiones por minuto (espaciado uniforme)."""

    def __init__(self, max_per_minute: int):
        self.spacing = 60.0 / max_per_minute if max_per_minute and max_per_minute > 0 else 0.0
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if not self.spacing:
            return
        async with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.spacing
        if delay > 0:
            await asyncio.sleep(delay)


def parse_retry_after(value: str | None, cap: float = 60.0) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, min(float(value), cap))
    except ValueError:
        return None


class HttpClient:
    def __init__(self, monitor: MonitorCfg, network: NetworkCfg, proxies: ProxyManager | None = None,
                 block_detector: BlockDetector | None = None):
        import httpx  # import diferido: permite probar el resto sin httpx instalado

        self._httpx = httpx
        self.monitor = monitor
        self.network = network
        self.proxies = proxies if proxies and proxies.enabled else None
        self.block_detector = block_detector or (lambda status, text: status in (403, 429))
        self.limiter = RateLimiter(monitor.max_requests_per_minute)
        self._clients: dict[str | None, "httpx.AsyncClient"] = {}
        self._closed = False
        self._ua = random.choice(USER_AGENTS)
        self.requests_made = 0

    # ---- sesiones (una por proxy => cookies y keep-alive separados) ----
    def _client_for(self, proxy: Proxy | None):
        key = proxy.url if proxy else None
        client = self._clients.get(key)
        if client is None:
            httpx = self._httpx
            headers = {
                "User-Agent": self._ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": self.network.accept_language,
                "Upgrade-Insecure-Requests": "1",
            }
            kwargs = dict(
                headers=headers,
                timeout=httpx.Timeout(self.monitor.timeout, connect=min(self.monitor.timeout, 10.0)),
                follow_redirects=True,
                limits=httpx.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=30),
                http2=self.network.http2,
            )
            if proxy:
                kwargs["proxy"] = proxy.url
            client = httpx.AsyncClient(**kwargs)  # verificación TLS activa (valor por defecto)
            self._clients[key] = client
        return client

    def _backoff(self, attempt: int) -> float:
        base = self.network.retry_backoff_base * (2 ** attempt)
        return min(self.network.retry_backoff_max, base) * random.uniform(0.8, 1.2)

    async def fetch(self, url: str) -> FetchResult:
        httpx = self._httpx
        attempts = self.monitor.retries + 1
        last = FetchResult(ok=False, url=url, error="sin intentos")
        proxy: Proxy | None = None
        for attempt in range(attempts):
            if self._closed:
                return FetchResult(ok=False, url=url, error="cliente cerrado")
            previous = proxy
            proxy = None
            if self.proxies:
                proxy = self.proxies.acquire(exclude=previous)
                if proxy is None and not self.network.proxy_fallback_direct:
                    wait = self.proxies.seconds_until_available()
                    last = FetchResult(ok=False, url=url, error=f"todos los proxies en cooldown (~{wait:.0f}s)",
                                       attempts=attempt + 1)
                    return last
                if previous is not None and proxy is not None and previous is not proxy:
                    log.proxy("Proxy cambiado %s -> %s", previous.label, proxy.label)
            await self.limiter.wait()
            label = proxy.label if proxy else None
            started = time.perf_counter()
            self.requests_made += 1
            retry_delay = 0.0
            try:
                resp = await self._client_for(proxy).get(url)
                elapsed = time.perf_counter() - started
                text = resp.text
                code = resp.status_code
                blocked = self.block_detector(code, text)
                final_url = str(resp.url)
                if code == 200 and not blocked and len(text) < 200:
                    last = FetchResult(False, url, final_url, code, text, elapsed, label,
                                       "respuesta anómala (cuerpo casi vacío)", False, attempt + 1)
                    reason, severe = "respuesta vacía", False
                elif code in (404, 410):
                    if proxy:
                        self.proxies.report_success(proxy, elapsed)
                    return FetchResult(False, url, final_url, code, text, elapsed, label,
                                       f"HTTP {code} (producto no encontrado)", False, attempt + 1)
                elif blocked or code in (403, 429):
                    last = FetchResult(False, url, final_url, code, text, elapsed, label,
                                       f"HTTP {code} (bloqueo/limitación)" if code != 200 else "página de verificación/bloqueo",
                                       True, attempt + 1)
                    reason, severe = f"HTTP {code}" if code != 200 else "bloqueo", True
                    retry_delay = parse_retry_after(resp.headers.get("retry-after")) or 0.0
                elif code >= 500:
                    last = FetchResult(False, url, final_url, code, text, elapsed, label,
                                       f"HTTP {code}", False, attempt + 1)
                    reason, severe = f"HTTP {code}", False
                elif code >= 400:
                    return FetchResult(False, url, final_url, code, text, elapsed, label,
                                       f"HTTP {code}", False, attempt + 1)
                else:
                    if proxy:
                        self.proxies.report_success(proxy, elapsed)
                    return FetchResult(True, url, final_url, code, text, elapsed, label, None, False, attempt + 1)
            except httpx.TimeoutException:
                elapsed = time.perf_counter() - started
                last = FetchResult(False, url, None, None, "", elapsed, label, "timeout", False, attempt + 1)
                reason, severe = "timeout", False
            except httpx.HTTPError as exc:   # ConnectError, ProxyError, ReadError, ...
                elapsed = time.perf_counter() - started
                msg = f"{type(exc).__name__}"
                last = FetchResult(False, url, None, None, "", elapsed, label, f"error de conexión ({msg})", False, attempt + 1)
                reason, severe = msg, False

            if proxy:
                self.proxies.report_failure(proxy, reason, severe=severe)
            log.warning("Petición fallida (%s) intento %d/%d%s", last.error, attempt + 1, attempts,
                        f" vía {label}" if label else "")
            if attempt + 1 < attempts:
                can_switch = bool(self.proxies and self.proxies.available())
                delay = max(retry_delay, 0.2 if can_switch else self._backoff(attempt))
                await asyncio.sleep(delay)
        return last

    async def post_json(self, url: str, payload: dict, timeout: float = 10.0):
        """POST directo (sin proxy) usado por Discord."""
        return await self._client_for(None).post(url, json=payload, timeout=timeout)

    async def aclose(self) -> None:
        self._closed = True
        for c in list(self._clients.values()):
            try:
                await c.aclose()
            except Exception:  # pragma: no cover
                pass
        self._clients.clear()
