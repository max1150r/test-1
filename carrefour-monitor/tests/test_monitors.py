import unittest

from monitor.detector import ChangeDetector
from monitor.models import EventKind, FetchResult, Status
from monitor.product_monitor import ProductMonitor
from monitor.search_monitor import SearchMonitor
from monitor.state import StateStore
from monitor.stats import Stats
from stores.carrefour.adapter import CarrefourAdapter
from tests.helpers import LISTING_URL, URL_CAJA, FakeClient, fixture
from utils.config import DiscordCfg, MonitorCfg, TargetCfg


class FakeNotifier:
    def __init__(self):
        self.events, self.errors = [], []

    def notify_event(self, ev):
        self.events.append(ev)

    def notify_error(self, key, title, details, target=""):
        self.errors.append((key, title))
        return True


class Base(unittest.IsolatedAsyncioTestCase):
    def build(self, cls, target, client, **mon):
        self.store = StateStore(":memory:")
        self.stats = Stats()
        self.notifier = FakeNotifier()
        cfg = MonitorCfg(max_enrich_per_cycle=mon.pop("max_enrich", 0), stock_renotify_cooldown=0, **mon)
        det = ChangeDetector(self.store, self.stats, 0)
        return cls(target, 1.0, CarrefourAdapter(), client, det, self.notifier, self.stats, cfg,
                   DiscordCfg(error_threshold=3))

    async def asyncTearDown(self):
        self.store.close()


class ProductMonitorTests(Base):
    def target(self):
        return TargetCfg(name="Caja Elite", kind="url", value=URL_CAJA)

    async def test_restock_notifies_once_and_survives_further_checks(self):
        oos = fixture("product_oos.html").replace("VC4A-34535245", "VC4A-34535246")
        client = FakeClient({URL_CAJA: [oos, fixture("product_in_stock.html"), fixture("product_in_stock.html")]})
        m = self.build(ProductMonitor, self.target(), client)
        r1 = await m.check()
        self.assertEqual((r1.ok, r1.status), (True, Status.OUT_OF_STOCK))
        self.assertEqual(self.notifier.events, [])
        await m.check()                                              # IN_STOCK + confirmación
        stock = lambda: [e for e in self.notifier.events if e.kind == EventKind.STOCK_AVAILABLE]
        self.assertEqual(len(stock()), 1)
        self.assertEqual(stock()[0].info.price, 54.99)
        # el fixture OOS tenía otro precio (15,99): también se detecta el cambio de precio
        self.assertEqual(sorted(e.kind.value for e in self.notifier.events), ["PRICE_CHANGE", "STOCK_AVAILABLE"])
        await m.check()
        self.assertEqual(len(stock()), 1)                            # sin repetición

    async def test_unconfirmed_stock_is_discarded_as_false_positive(self):
        client = FakeClient({URL_CAJA: [fixture("product_in_stock.html"), fixture("product_disabled_button.html")]})
        m = self.build(ProductMonitor, self.target(), client)
        await m.check()
        self.assertEqual(self.notifier.events, [])
        rec = await self.store.get_product("carrefour:VC4A-34535246")
        self.assertEqual(rec.stock_status, Status.OUT_OF_STOCK)

    async def test_confirmation_can_be_disabled(self):
        client = FakeClient({URL_CAJA: [fixture("product_in_stock.html")]})
        m = self.build(ProductMonitor, self.target(), client, confirm_stock=False)
        await m.check()
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(self.notifier.events), 1)

    async def test_repeated_blocks_raise_one_error_alert_per_threshold_and_keep_state(self):
        blocked = FetchResult(False, URL_CAJA, status_code=429, error="HTTP 429", blocked=True)
        client = FakeClient({URL_CAJA: [fixture("product_in_stock.html"), blocked]})
        m = self.build(ProductMonitor, self.target(), client, confirm_stock=False)
        await m.check()                                              # IN_STOCK conocido
        for _ in range(3):
            r = await m.check()
            self.assertEqual((r.ok, r.blocked), (False, True))
        self.assertEqual(len(self.notifier.errors), 1)
        self.assertEqual(self.notifier.errors[0][1], "Bloqueo")
        self.assertEqual(self.stats.blocked, 3)
        rec = await self.store.get_product("carrefour:VC4A-34535246")
        self.assertEqual(rec.stock_status, Status.IN_STOCK)          # el bloqueo no borra el estado

    async def test_unrecognized_page_never_alerts_stock(self):
        client = FakeClient({URL_CAJA: ["<html><body><div id='app'></div>" + "x" * 300 + "</body></html>"]})
        m = self.build(ProductMonitor, self.target(), client)
        r = await m.check()
        self.assertEqual(r.status, Status.UNKNOWN)
        self.assertEqual(self.notifier.events, [])


class SearchMonitorTests(Base):
    SEARCH_URL = "https://www.carrefour.es/?q=pokemon"

    def target(self, **kw):
        return TargetCfg(name="pokemon", kind="search", value="pokemon", **kw)

    async def test_first_pass_is_silent_then_new_products_alert_once(self):
        base_html = fixture("listing.html")
        extra = base_html.replace("</ul>", '<li><a href="/pokemon-nueva-lata/VC4A-77777/p"><img src="https://static.carrefour.es/n.jpg"></a>'
                                  '<span>44,95 €</span><h2><a href="/pokemon-nueva-lata/VC4A-77777/p">Pokémon Nueva Lata 30 Aniversario</a></h2>'
                                  '<p>Vendido por Carrefour</p><button>Añadir</button></li></ul>')
        client = FakeClient({self.SEARCH_URL: [base_html, extra, extra]})
        m = self.build(SearchMonitor, self.target(exclude_keywords=["peluche"]), client)
        await m.check()
        self.assertEqual(self.notifier.events, [])                   # baseline
        self.assertTrue(await self.store.search_initialized(m.target.id))
        await m.check()
        kinds = [(e.kind, e.info.sku) for e in self.notifier.events]
        self.assertEqual(kinds, [(EventKind.NEW_PRODUCT, "VC4A-77777")])
        ev = self.notifier.events[0]
        self.assertEqual((ev.info.status, ev.info.price, ev.query), (Status.IN_STOCK, 44.95, "pokemon"))
        await m.check()
        self.assertEqual(len(self.notifier.events), 1)               # deduplicado
        self.assertIsNone(await self.store.get_product("carrefour:VC4A-34428370"))   # peluche filtrado

    async def test_restock_inside_search_notifies_stock(self):
        before = fixture("listing.html")
        after = before.replace("<div>Agotado temporalmente</div>", "<button>Añadir</button>")
        client = FakeClient({self.SEARCH_URL: [before, after]})
        m = self.build(SearchMonitor, self.target(), client)
        await m.check()
        await m.check()
        self.assertEqual([(e.kind, e.info.sku) for e in self.notifier.events],
                         [(EventKind.STOCK_AVAILABLE, "VC4A-34535250")])

    async def test_notify_existing_alerts_on_first_pass(self):
        client = FakeClient({self.SEARCH_URL: [fixture("listing.html")]})
        m = self.build(SearchMonitor, self.target(notify_existing=True), client)
        await m.check()
        self.assertEqual(len(self.notifier.events), 5)

    async def test_lazy_items_are_enriched_with_product_page(self):
        lazy_url = "https://www.carrefour.es/pokemon-caja-elite-30th-aniversario-ingles-juego-de-mesa-6-anos-unboxing/VC4A-34535246/p"
        client = FakeClient({self.SEARCH_URL: [fixture("listing.html")], lazy_url: [fixture("product_in_stock.html")]})
        m = self.build(SearchMonitor, self.target(notify_existing=True), client, max_enrich=2)
        await m.check()
        ev = next(e for e in self.notifier.events if e.info.sku == "VC4A-34535246")
        self.assertEqual((ev.info.status, ev.info.price), (Status.IN_STOCK, 54.99))
        self.assertLessEqual(len([u for u in client.calls if u != self.SEARCH_URL]), 2)

    async def test_blocked_search_is_reported_and_does_not_initialize(self):
        blocked = FetchResult(False, self.SEARCH_URL, status_code=403, error="HTTP 403", blocked=True)
        client = FakeClient({self.SEARCH_URL: blocked})
        m = self.build(SearchMonitor, self.target(), client)
        r = await m.check()
        self.assertEqual((r.ok, r.blocked), (False, True))
        self.assertFalse(await self.store.search_initialized(m.target.id))

    async def test_listing_target_uses_url_and_pagination(self):
        client = FakeClient({LISTING_URL: [fixture("listing.html")],
                             LISTING_URL + "?offset=24": ["<html><body><div>vacío</div>" + "x" * 300 + "</body></html>"]})
        t = TargetCfg(name="Listado", kind="listing", value=LISTING_URL, max_pages=3)
        m = self.build(SearchMonitor, t, client)
        r = await m.check()
        self.assertTrue(r.ok)
        self.assertEqual(client.calls, [LISTING_URL, LISTING_URL + "?offset=24"])   # corta en la 1ª página vacía


if __name__ == "__main__":
    unittest.main()
