"""Monitor de una URL de producto."""
from __future__ import annotations

import asyncio

from monitor.base import BaseMonitor
from monitor.models import CheckResult, ProductInfo, Status
from utils.logger import log
from utils.text import format_eur


def describe(info: ProductInfo) -> str:
    bits = []
    if info.price is not None:
        bits.append(format_eur(info.price))
    if info.response_time is not None:
        bits.append(f"{info.response_time * 1000:.0f} ms")
    if info.proxy:
        bits.append(f"proxy {info.proxy}")
    return f"{info.display_name}" + (f" - {' | '.join(bits)}" if bits else "")


class ProductMonitor(BaseMonitor):
    kind = "url"

    async def _fetch(self) -> ProductInfo:
        info = await self.adapter.get_product(self.client, self.target.value)
        self.stats.record_check(info.status, info.response_time)
        info.name = info.name or self.target.name
        return info

    async def check(self) -> CheckResult:
        log.check(self.name)
        info = await self._fetch()

        if info.status == Status.IN_STOCK and self.cfg.confirm_stock and await self.detector.would_alert_stock(info):
            await asyncio.sleep(0.3)
            second = await self._fetch()
            if second.status == Status.IN_STOCK:
                log.info("Stock confirmado en segunda lectura: %s", self.name)
            elif second.status == Status.OUT_OF_STOCK:
                log.warning("Stock NO confirmado (falso positivo evitado): %s", self.name)
                info = second
            else:
                log.warning("No se pudo confirmar el stock (%s); se notifica con la primera lectura: %s",
                            second.status.value, self.name)

        if info.status == Status.IN_STOCK:
            log.stock(describe(info))
        elif info.status == Status.OUT_OF_STOCK:
            log.oos(describe(info))
        elif info.status == Status.BLOCKED:
            log.blocked("%s - %s%s", self.name, info.reason, f" (proxy {info.proxy})" if info.proxy else "")
        elif info.status == Status.ERROR:
            log.error("%s - %s%s", self.name, info.reason, f" (proxy {info.proxy})" if info.proxy else "")
        else:
            log.warning("%s - estado desconocido: %s", self.name, info.reason)

        events = await self.detector.process(info, source="url", target_name=self.name)
        self.dispatch(events)
        self.track(info.status, info.reason)
        ok = info.status not in (Status.ERROR, Status.BLOCKED)
        return CheckResult(ok=ok, status=info.status, error=None if ok else info.reason,
                           blocked=info.status == Status.BLOCKED)
