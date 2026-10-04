"""Gestión de proxies: carga, validación, rotación, cooldown y estadísticas."""
from __future__ import annotations

import ipaddress
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from utils.logger import log

SUPPORTED_SCHEMES = {"http", "https", "socks4", "socks5", "socks5h"}


class ProxyParseError(ValueError):
    pass


@dataclass
class Proxy:
    url: str                      # URL completa (con credenciales): NO se registra nunca
    scheme: str
    host: str
    port: int
    index: int = 0
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    strikes: int = 0              # veces que ha entrado en cooldown seguidas
    disabled_until: float = 0.0
    last_error: str = ""
    total_time: float = 0.0
    last_used: float = 0.0

    @property
    def label(self) -> str:
        """Identificador anonimizado para logs: #3 203.0.x.x:8080 (sin credenciales)."""
        host = self.host
        if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host):
            a, b, *_ = host.split(".")
            host = f"{a}.{b}.x.x"
        else:
            host = host[:3] + "***"
        return f"#{self.index} {host}:{self.port}"

    @property
    def avg_time(self) -> float:
        return self.total_time / self.successes if self.successes else 0.0


def parse_proxy_line(line: str, default_scheme: str = "http") -> str:
    """Normaliza una línea de proxies.txt a una URL válida. Lanza ProxyParseError si no es válida.

    Formatos: IP:PORT, USER:PASS@IP:PORT, scheme://IP:PORT, scheme://USER:PASS@IP:PORT
    (extra: IP:PORT:USER:PASS).
    """
    raw = line.strip()
    if "://" not in raw:
        if "@" not in raw and raw.count(":") == 3:       # host:port:user:pass
            host, port, user, pw = raw.split(":")
            raw = f"{user}:{pw}@{host}:{port}"
        raw = f"{default_scheme}://{raw}"
    # Análisis manual: todo lo que hay antes de la ÚLTIMA '@' son credenciales (pueden llevar '/' o ':').
    scheme, _, rest = raw.partition("://")
    scheme = scheme.lower()
    if scheme not in SUPPORTED_SCHEMES:
        raise ProxyParseError(f"esquema '{scheme}' no soportado ({', '.join(sorted(SUPPORTED_SCHEMES))})")
    creds, at, hostport = rest.rpartition("@")
    hostport = hostport.rstrip("/")
    if hostport.startswith("["):                           # IPv6: [::1]:8080
        host, _, tail = hostport[1:].partition("]")
        port_s = tail[1:] if tail.startswith(":") else ""
    else:
        host, _, port_s = hostport.rpartition(":")
        if not _:
            host, port_s = hostport, ""
    if not host:
        raise ProxyParseError("falta el host")
    if not port_s.isdigit() or not (1 <= int(port_s) <= 65535):
        raise ProxyParseError("puerto ausente o fuera de rango")
    port = int(port_s)
    if re.fullmatch(r"[\d.]+", host):
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            raise ProxyParseError(f"IP no válida '{host}'") from None
    elif not re.fullmatch(r"[A-Za-z0-9.\-]+|[0-9a-fA-F:]+", host):
        raise ProxyParseError("host con caracteres no válidos")
    auth = ""
    if at:
        user, colon, pw = creds.partition(":")
        if not user:
            raise ProxyParseError("usuario vacío")
        auth = quote(unquote(user), safe="")
        if colon:
            auth += ":" + quote(unquote(pw), safe="")
        auth += "@"
    host_part = f"[{host}]" if ":" in host else host
    return f"{scheme}://{auth}{host_part}:{port}"


class ProxyManager:
    def __init__(self, strategy: str = "round_robin", cooldown: float = 120.0, max_failures: int = 3,
                 max_cooldown: float = 1800.0, rng: random.Random | None = None, clock=time.monotonic):
        if strategy not in ("round_robin", "random"):
            raise ValueError("strategy debe ser round_robin o random")
        self.strategy = strategy
        self.cooldown = cooldown
        self.max_failures = max(1, max_failures)
        self.max_cooldown = max_cooldown
        self._rng = rng or random.Random()
        self._clock = clock
        self._proxies: list[Proxy] = []
        self._cursor = 0
        self.invalid: list[tuple[str, str]] = []

    # ---- carga ----
    def load_lines(self, lines) -> tuple[int, int]:
        loaded = 0
        known = {p.url for p in self._proxies}
        for n, line in enumerate(lines, 1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            try:
                url = parse_proxy_line(text)
            except ProxyParseError as exc:
                self.invalid.append((f"línea {n}", str(exc)))   # no guardamos el texto (puede llevar contraseña)
                continue
            if url in known:
                continue
            known.add(url)
            p = urlsplit(url)
            self._proxies.append(Proxy(url=url, scheme=p.scheme, host=p.hostname or "", port=p.port or 0,
                                       index=len(self._proxies) + 1))
            loaded += 1
        return loaded, len(self.invalid)

    @classmethod
    def from_file(cls, path: str | Path, **kwargs) -> "ProxyManager":
        mgr = cls(**kwargs)
        path = Path(path)
        if path.exists():
            loaded, bad = mgr.load_lines(path.read_text(encoding="utf-8", errors="ignore").splitlines())
            for where, why in mgr.invalid:
                log.warning("Proxy ignorado (%s): %s", where, why)
            log.info("Proxies cargados: %d (inválidos: %d)", loaded, bad)
        return mgr

    # ---- selección ----
    def __len__(self) -> int:
        return len(self._proxies)

    @property
    def enabled(self) -> bool:
        return bool(self._proxies)

    def _is_available(self, p: Proxy) -> bool:
        return p.disabled_until <= self._clock()

    def available(self) -> list[Proxy]:
        return [p for p in self._proxies if self._is_available(p)]

    def acquire(self, exclude: Proxy | None = None) -> Proxy | None:
        """Devuelve un proxy disponible (o None si todos están en cooldown / no hay proxies)."""
        avail = self.available()
        if exclude is not None and len(avail) > 1:
            avail = [p for p in avail if p is not exclude]
        if not avail:
            return None
        if self.strategy == "random":
            choice = self._rng.choice(avail)
        else:
            n = len(self._proxies)
            choice = None
            for i in range(n):
                cand = self._proxies[(self._cursor + i) % n]
                if cand in avail:
                    choice = cand
                    self._cursor = (self._cursor + i + 1) % n
                    break
            choice = choice or avail[0]
        choice.last_used = self._clock()
        return choice

    def seconds_until_available(self) -> float:
        if not self._proxies:
            return 0.0
        return max(0.0, min(p.disabled_until for p in self._proxies) - self._clock())

    # ---- feedback ----
    def report_success(self, proxy: Proxy, elapsed: float = 0.0) -> None:
        proxy.successes += 1
        proxy.consecutive_failures = 0
        proxy.strikes = 0
        proxy.total_time += elapsed

    def report_failure(self, proxy: Proxy, reason: str, severe: bool = False) -> bool:
        """Registra un fallo. Devuelve True si el proxy pasa a cooldown."""
        proxy.failures += 1
        proxy.consecutive_failures += 1
        proxy.last_error = reason
        if severe or proxy.consecutive_failures >= self.max_failures:
            proxy.strikes += 1
            wait = min(self.max_cooldown, self.cooldown * (2 ** (proxy.strikes - 1)))
            proxy.disabled_until = self._clock() + wait
            proxy.consecutive_failures = 0
            log.proxy("Proxy %s en cooldown %.0fs (%s)", proxy.label, wait, reason)
            return True
        return False

    # ---- estadísticas ----
    def stats(self) -> dict:
        avail = len(self.available())
        return {
            "total": len(self._proxies),
            "available": avail,
            "disabled": len(self._proxies) - avail,
            "per_proxy": [
                {"proxy": p.label, "ok": p.successes, "fail": p.failures,
                 "avg_ms": round(p.avg_time * 1000), "available": self._is_available(p), "last_error": p.last_error}
                for p in self._proxies
            ],
        }
