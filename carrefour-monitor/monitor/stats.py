"""Estadísticas internas (health check)."""
from __future__ import annotations

import time

from monitor.models import Status


class Stats:
    def __init__(self) -> None:
        self.started = time.time()
        self.total_checks = 0
        self.ok_checks = 0
        self.errors = 0
        self.blocked = 0
        self.stock_detected = 0
        self.new_products = 0
        self.price_changes = 0
        self.notifications_sent = 0
        self.notifications_failed = 0
        self._rt_sum = 0.0
        self._rt_n = 0

    def record_check(self, status: Status, elapsed: float | None = None) -> None:
        self.total_checks += 1
        if status == Status.BLOCKED:
            self.blocked += 1
        elif status == Status.ERROR:
            self.errors += 1
        else:
            self.ok_checks += 1
        if elapsed is not None:
            self._rt_sum += elapsed
            self._rt_n += 1

    @property
    def avg_response_time(self) -> float:
        return self._rt_sum / self._rt_n if self._rt_n else 0.0

    @property
    def uptime(self) -> float:
        return time.time() - self.started

    def snapshot(self, proxy_stats: dict | None = None) -> dict:
        d = {
            "uptime_s": round(self.uptime),
            "total_checks": self.total_checks,
            "ok_checks": self.ok_checks,
            "errors": self.errors,
            "blocked": self.blocked,
            "stock_detected": self.stock_detected,
            "new_products": self.new_products,
            "price_changes": self.price_changes,
            "avg_response_ms": round(self.avg_response_time * 1000),
            "notifications_sent": self.notifications_sent,
            "notifications_failed": self.notifications_failed,
        }
        if proxy_stats:
            d["proxies_available"] = proxy_stats["available"]
            d["proxies_disabled"] = proxy_stats["disabled"]
        return d

    @staticmethod
    def format_uptime(seconds: float) -> str:
        s = int(seconds)
        h, rem = divmod(s, 3600)
        m, s = divmod(rem, 60)
        return f"{h}h {m:02d}m {s:02d}s"

    def summary(self, proxy_stats: dict | None = None) -> str:
        d = self.snapshot(proxy_stats)
        parts = [
            f"uptime={self.format_uptime(d['uptime_s'])}",
            f"checks={d['total_checks']}", f"ok={d['ok_checks']}", f"err={d['errors']}",
            f"bloq={d['blocked']}", f"stock={d['stock_detected']}", f"nuevos={d['new_products']}",
            f"precio={d['price_changes']}", f"resp_media={d['avg_response_ms']}ms",
        ]
        if proxy_stats:
            parts.append(f"proxies={d['proxies_available']} activos/{d['proxies_disabled']} en cooldown")
        return " | ".join(parts)
