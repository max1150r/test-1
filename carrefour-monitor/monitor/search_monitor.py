"""Monitor de búsquedas por palabras clave (o de una URL de listado/categoría)."""
from __future__ import annotations

from monitor.base import BaseMonitor
from monitor.filters import SearchFilter
from monitor.models import CheckResult, EventKind, ProductInfo, Status
from utils.logger import log


class SearchMonitor(BaseMonitor):
    kind = "search"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        t = self.target
        self.is_listing = t.kind == "listing"
        self.query = t.name if self.is_listing else t.value
        self.filter = SearchFilter(
            query="" if self.is_listing else t.value, include_keywords=t.include_keywords,
            exclude_keywords=t.exclude_keywords, include_mode=t.include_mode, query_match=t.query_match,
            min_price=t.min_price, max_price=t.max_price,
        )
        self._warned_empty = False

    async def _fetch_pages(self) -> tuple[list[ProductInfo], bool, Status | None, str]:
        """Devuelve (items, completo, estado_error_si_falla_la_primera_pagina, motivo)."""
        items: dict[str, ProductInfo] = {}
        complete = True
        for page in range(self.target.max_pages):
            if self.is_listing:
                sp = await self.adapter.get_listing(self.client, self.target.value, page)
            else:
                sp = await self.adapter.search(self.client, self.target.value, page)
            st = Status.UNKNOWN if sp.ok else (Status.BLOCKED if sp.blocked else Status.ERROR)
            self.stats.record_check(st, sp.elapsed)
            if not sp.ok:
                if page == 0:
                    return [], False, st, sp.error or "error"
                complete = False
                log.warning("%s: fallo en la página %d (%s); se continúa con lo obtenido", self.name, page + 1, sp.error)
                break
            for it in sp.items:
                items.setdefault(it.key, it)
            if not sp.items:
                break
        return list(items.values()), complete, None, ""

    async def _enrich(self, items: list[ProductInfo], baseline: bool) -> None:
        """Completa (stock/precio) los resultados que el listado trae sin datos, con tope por ciclo."""
        if not self.cfg.enrich_new_products or self.cfg.max_enrich_per_cycle <= 0:
            return
        cands = []
        for it in items:
            if it.status != Status.UNKNOWN:
                continue
            prev = await self.detector.store.get_product(it.key)
            if prev is not None and prev.stock_status.is_stock_status and baseline:
                continue
            cands.append((prev is not None, (prev.last_check or 0) if prev else 0, it))
        cands.sort(key=lambda c: (c[0], c[1]))     # primero los nunca vistos, luego los más antiguos
        for _, _, it in cands[: self.cfg.max_enrich_per_cycle]:
            full = await self.adapter.get_product(self.client, it.url)
            self.stats.record_check(full.status, full.response_time)
            if full.status in (Status.ERROR, Status.BLOCKED):
                continue
            it.status = full.status
            it.price = full.price if full.price is not None else it.price
            it.name = it.name or full.name
            it.image = it.image or full.image
            it.seller = it.seller or full.seller
            it.reason = full.reason

    async def check(self) -> CheckResult:
        log.check(f"{self.name} ({'listado' if self.is_listing else 'búsqueda'})")
        sid = self.target.id
        initialized = await self.detector.store.search_initialized(sid)
        baseline = (not initialized) and (not self.target.notify_existing)

        items, complete, err_status, reason = await self._fetch_pages()
        if err_status is not None:
            if err_status == Status.BLOCKED:
                log.blocked("%s - %s", self.name, reason)
            else:
                log.error("%s - %s", self.name, reason)
            self.track(err_status, reason)
            return CheckResult(ok=False, status=err_status, error=reason, blocked=err_status == Status.BLOCKED)

        if not items:
            if not self._warned_empty:
                log.warning("%s: la página respondió pero no se reconoció ningún producto. "
                            "¿URL de búsqueda correcta? Prueba `python main.py --inspect <URL>`.", self.name)
                self._warned_empty = True
            self.track(Status.UNKNOWN, "0 resultados reconocidos")
            return CheckResult(ok=True, status=Status.UNKNOWN)
        self._warned_empty = False

        accepted: list[ProductInfo] = []
        for it in items:
            ok, why = self.filter.evaluate(it.name, it.price)
            if ok:
                accepted.append(it)
            else:
                log.debug("Descartado (%s): %s", why, it.display_name)

        await self._enrich(accepted, baseline)

        new_count = 0
        for it in accepted:
            events = await self.detector.process(it, source="listing" if self.is_listing else "search",
                                                 target_name=self.name, query=self.query,
                                                 search_id=sid, baseline=baseline)
            for ev in events:
                if ev.kind == EventKind.NEW_PRODUCT:
                    new_count += 1
                    await self.detector.store.set_hit_notified(sid, it.key)
            self.dispatch(events)

        if complete and not initialized:
            await self.detector.store.mark_search_initialized(sid, self.query)
            if baseline:
                log.info("%s: primera pasada, %d resultados guardados como referencia (sin avisos)",
                         self.name, len(accepted))
        in_stock = sum(1 for it in accepted if it.status == Status.IN_STOCK)
        log.info("%s: %d resultados (%d tras filtros, %d en stock, %d nuevos)",
                 self.name, len(items), len(accepted), in_stock, new_count)
        self.track(Status.OUT_OF_STOCK, "")   # lectura correcta: resetea rachas
        return CheckResult(ok=True, status=Status.IN_STOCK if in_stock else Status.OUT_OF_STOCK)
