"""Detección de CAMBIOS: compara el estado nuevo con el guardado y decide qué eventos emitir.

(La detección de stock dentro de una página vive en stores/carrefour/detection.py.)
"""
from __future__ import annotations

import asyncio
import time

from monitor.models import Event, EventKind, ProductInfo, Status
from monitor.state import StateStore
from monitor.stats import Stats
from utils.logger import log


class ChangeDetector:
    def __init__(self, store: StateStore, stats: Stats, stock_renotify_cooldown: float = 60.0):
        self.store = store
        self.stats = stats
        self.cooldown = stock_renotify_cooldown
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, key: str) -> asyncio.Lock:
        # Sin awaits entre get y set: seguro en asyncio (un solo hilo).
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    async def would_alert_stock(self, info: ProductInfo) -> bool:
        """¿Esta observación IN_STOCK sería una transición a stock? (para confirmar antes)."""
        if info.status != Status.IN_STOCK:
            return False
        prev = await self.store.get_product(info.key)
        return prev is None or prev.stock_status != Status.IN_STOCK

    async def process(self, info: ProductInfo, *, source: str, target_name: str,
                      query: str | None = None, search_id: str | None = None,
                      baseline: bool = False) -> list[Event]:
        """Actualiza el estado persistido y devuelve los eventos a notificar.

        Reglas:
          * ERROR/BLOCKED nunca modifican el último estado de stock conocido.
          * UNKNOWN tampoco (evita falsos "vuelta a stock" tras un fallo de lectura).
          * OOS->IN, UNKNOWN->IN => STOCK_AVAILABLE; IN->IN no repite; IN->OOS => STOCK_LOST.
          * Resultado nuevo en una búsqueda => NEW_PRODUCT (una sola vez por búsqueda).
          * baseline=True (primera pasada de una búsqueda) guarda sin avisar.
        """
        async with self._lock(info.key):
            now = time.time()
            prev = await self.store.get_product(info.key)

            if info.status in (Status.ERROR, Status.BLOCKED):
                if prev:
                    await self.store.touch(info.key, now)
                return []

            events: list[Event] = []
            new_hit = False
            if search_id:
                new_hit = await self.store.mark_hit(search_id, info.key, query, now)

            prev_stock = prev.stock_status if prev else Status.UNKNOWN
            cur_stock = info.status if info.status.is_stock_status else prev_stock
            stock_changed = info.status.is_stock_status and info.status != prev_stock
            last_stock_notified = prev.last_stock_notified if prev else None
            last_notified = prev.last_notified if prev else None

            def mk(kind: EventKind, **kw) -> Event:
                return Event(kind=kind, info=info, target_name=target_name, source=source, query=query, **kw)

            suppress_stock_event = False
            if new_hit and not baseline:
                events.append(mk(EventKind.NEW_PRODUCT, previous_status=None, new_price=info.price))
                self.stats.new_products += 1
                suppress_stock_event = True  # el aviso de nuevo producto ya incluye la disponibilidad
                last_notified = now
                if info.status == Status.IN_STOCK:
                    last_stock_notified = now
                    self.stats.stock_detected += 1

            if info.status == Status.IN_STOCK and prev_stock != Status.IN_STOCK:
                if suppress_stock_event or (baseline and prev is None):
                    pass
                elif last_stock_notified and now - last_stock_notified < self.cooldown:
                    log.info("Aviso de stock suprimido por cooldown (%s)", info.display_name)
                else:
                    events.append(mk(EventKind.STOCK_AVAILABLE, previous_status=prev_stock, new_price=info.price))
                    self.stats.stock_detected += 1
                    last_stock_notified = now
                    last_notified = now
            elif info.status == Status.OUT_OF_STOCK and prev_stock == Status.IN_STOCK:
                events.append(mk(EventKind.STOCK_LOST, previous_status=prev_stock, new_price=info.price))
                last_notified = now

            prev_price = prev.current_price if prev else None
            previous_price = prev.previous_price if prev else None
            price_changed = (info.price is not None and prev_price is not None
                             and abs(info.price - prev_price) >= 0.005)
            if price_changed:
                previous_price = prev_price
                events.append(mk(EventKind.PRICE_CHANGE, previous_price=prev_price, new_price=info.price))
                self.stats.price_changes += 1
                log.info("Cambio de precio %s: %.2f -> %.2f", info.display_name, prev_price, info.price)

            current_price = info.price if info.price is not None else prev_price
            changed = stock_changed or price_changed
            await self.store.upsert_product({
                "key": info.key, "store": info.store, "sku": info.sku or (prev.sku if prev else None),
                "name": info.name or (prev.name if prev else None), "url": info.url,
                "image": info.image or (prev.image if prev else None),
                "seller": info.seller or (prev.seller if prev else None),
                "status": info.status, "stock_status": cur_stock,
                "current_price": current_price, "previous_price": previous_price,
                "first_seen": prev.first_seen if prev else now, "last_check": now,
                "last_change": now if changed else (prev.last_change if prev else now),
                "last_notified": last_notified, "last_stock_notified": last_stock_notified,
            })
            if info.status.is_stock_status and prev_stock != info.status and prev is not None:
                log.info("Cambio de estado %s: %s -> %s", info.display_name, prev_stock.value, info.status.value)
            return events
