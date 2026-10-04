import asyncio
import tempfile
import unittest
from pathlib import Path

from monitor.detector import ChangeDetector
from monitor.models import EventKind, ProductInfo, Status
from monitor.state import StateStore
from monitor.stats import Stats


def info(status, price=10.0, sku="VC4A-1", name="Producto"):
    return ProductInfo(store="carrefour", url=f"https://www.carrefour.es/x/{sku}/p", status=status,
                       sku=sku, name=name, price=price)


class TransitionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = StateStore(":memory:")
        self.det = ChangeDetector(self.store, Stats(), stock_renotify_cooldown=0)

    async def asyncTearDown(self):
        self.store.close()

    async def kinds(self, status, **kw):
        evs = await self.det.process(info(status, **kw), source="url", target_name="t")
        return [e.kind for e in evs]

    async def test_transitions(self):
        self.assertEqual(await self.kinds(Status.OUT_OF_STOCK), [])                    # UNKNOWN -> OOS
        self.assertEqual(await self.kinds(Status.OUT_OF_STOCK), [])                    # OOS -> OOS
        self.assertEqual(await self.kinds(Status.IN_STOCK), [EventKind.STOCK_AVAILABLE])  # OOS -> IN
        self.assertEqual(await self.kinds(Status.IN_STOCK), [])                        # IN -> IN
        self.assertEqual(await self.kinds(Status.OUT_OF_STOCK), [EventKind.STOCK_LOST])   # IN -> OOS

    async def test_unknown_to_in_stock_notifies(self):
        self.assertEqual(await self.kinds(Status.IN_STOCK), [EventKind.STOCK_AVAILABLE])

    async def test_errors_do_not_reset_stock_state(self):
        await self.kinds(Status.IN_STOCK)
        for st in (Status.ERROR, Status.BLOCKED, Status.UNKNOWN):
            self.assertEqual(await self.kinds(st), [])
        self.assertEqual(await self.kinds(Status.IN_STOCK), [])      # sigue en stock: sin re-aviso

    async def test_price_change(self):
        await self.kinds(Status.OUT_OF_STOCK, price=20.0)
        evs = await self.det.process(info(Status.OUT_OF_STOCK, price=15.0), source="url", target_name="t")
        self.assertEqual([e.kind for e in evs], [EventKind.PRICE_CHANGE])
        self.assertEqual((evs[0].previous_price, evs[0].new_price), (20.0, 15.0))
        rec = await self.store.get_product("carrefour:VC4A-1")
        self.assertEqual((rec.previous_price, rec.current_price), (20.0, 15.0))
        self.assertEqual(await self.kinds(Status.OUT_OF_STOCK, price=15.0), [])   # mismo precio: nada

    async def test_renotify_cooldown_suppresses_flapping(self):
        det = ChangeDetector(self.store, Stats(), stock_renotify_cooldown=3600)
        k = lambda st: det.process(info(st, sku="VC4A-2"), source="url", target_name="t")
        self.assertEqual(len(await k(Status.IN_STOCK)), 1)
        await k(Status.OUT_OF_STOCK)
        self.assertEqual(await k(Status.IN_STOCK), [])      # dentro del cooldown

    async def test_concurrent_processing_does_not_duplicate_alert(self):
        results = await asyncio.gather(*(self.det.process(info(Status.IN_STOCK, sku="VC4A-3"), source="url",
                                                          target_name="t") for _ in range(8)))
        self.assertEqual(sum(len(r) for r in results), 1)


class SearchDedupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = StateStore(":memory:")
        self.det = ChangeDetector(self.store, Stats(), 0)

    async def asyncTearDown(self):
        self.store.close()

    async def test_new_product_only_once_and_baseline_is_silent(self):
        p = info(Status.IN_STOCK, sku="VC4A-10")
        base = await self.det.process(p, source="search", target_name="s", query="q", search_id="s1", baseline=True)
        self.assertEqual(base, [])
        again = await self.det.process(p, source="search", target_name="s", query="q", search_id="s1")
        self.assertEqual(again, [])                              # ya estaba (baseline) y sigue en stock
        new = await self.det.process(info(Status.IN_STOCK, sku="VC4A-11"), source="search", target_name="s",
                                     query="q", search_id="s1")
        self.assertEqual([e.kind for e in new], [EventKind.NEW_PRODUCT])   # y no además STOCK_AVAILABLE
        dup = await self.det.process(info(Status.IN_STOCK, sku="VC4A-11"), source="search", target_name="s",
                                     query="q", search_id="s1")
        self.assertEqual(dup, [])

    async def test_known_search_result_restock_notifies_stock(self):
        await self.det.process(info(Status.OUT_OF_STOCK, sku="VC4A-12"), source="search", target_name="s",
                               query="q", search_id="s1", baseline=True)
        evs = await self.det.process(info(Status.IN_STOCK, sku="VC4A-12"), source="search", target_name="s",
                                     query="q", search_id="s1")
        self.assertEqual([e.kind for e in evs], [EventKind.STOCK_AVAILABLE])

    async def test_same_product_in_two_searches_is_new_for_each(self):
        a = await self.det.process(info(Status.OUT_OF_STOCK, sku="VC4A-13"), source="search", target_name="a",
                                   query="a", search_id="sa")
        b = await self.det.process(info(Status.OUT_OF_STOCK, sku="VC4A-13"), source="search", target_name="b",
                                   query="b", search_id="sb")
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 1)


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_state_survives_restart_without_realerting(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.db"
            s1 = StateStore(path)
            det1 = ChangeDetector(s1, Stats(), 0)
            self.assertEqual(len(await det1.process(info(Status.IN_STOCK, sku="VC4A-20"), source="url", target_name="t")), 1)
            await s1.mark_search_initialized("s9", "q")
            s1.close()
            s2 = StateStore(path)
            det2 = ChangeDetector(s2, Stats(), 0)
            self.assertEqual(await det2.process(info(Status.IN_STOCK, sku="VC4A-20"), source="url", target_name="t"), [])
            self.assertTrue(await s2.search_initialized("s9"))
            rec = await s2.get_product("carrefour:VC4A-20")
            self.assertEqual(rec.stock_status, Status.IN_STOCK)
            self.assertGreater(rec.last_stock_notified, 0)
            s2.close()


if __name__ == "__main__":
    unittest.main()
