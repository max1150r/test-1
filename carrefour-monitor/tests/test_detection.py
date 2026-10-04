import unittest

from monitor.models import Status
from stores.carrefour import detection as det
from stores.carrefour.adapter import CarrefourAdapter
from tests.helpers import URL_CAJA, URL_SOBRES, fixture


class ProductDetectionTests(unittest.TestCase):
    def setUp(self):
        self.adapter = CarrefourAdapter()

    def test_out_of_stock_text_beats_stale_jsonld(self):
        a = det.analyze_product_page(fixture("product_oos.html"), URL_SOBRES)
        self.assertEqual(a.status, Status.OUT_OF_STOCK)
        self.assertEqual(a.price, 15.99)
        self.assertEqual(a.seller, "Carrefour")
        self.assertEqual(a.ean, "0196214152281")
        self.assertIn("og:image" if False else "783645", a.image)

    def test_carousel_buttons_do_not_make_oos_page_in_stock(self):
        # el carrusel de otros productos tiene botones "Añadir": no deben contar
        a = det.analyze_product_page(fixture("product_oos.html"), URL_SOBRES)
        self.assertNotIn(Status.IN_STOCK, [s.status for s in a.signals if s.source == "html"])

    def test_in_stock_jsonld_and_button(self):
        a = det.analyze_product_page(fixture("product_in_stock.html"), URL_CAJA)
        self.assertEqual(a.status, Status.IN_STOCK)
        self.assertEqual(a.price, 54.99)
        self.assertEqual({s.source for s in a.signals}, {"jsonld", "html"})

    def test_carousel_oos_text_does_not_make_in_stock_page_oos(self):
        a = det.analyze_product_page(fixture("product_in_stock.html"), URL_CAJA)
        self.assertEqual(a.status, Status.IN_STOCK)

    def test_html_only_in_stock_and_strikethrough_price(self):
        url = "https://www.carrefour.es/mini-lata/VC4A-1/p"
        a = det.analyze_product_page(fixture("product_html_only_in_stock.html"), url)
        self.assertEqual(a.status, Status.IN_STOCK)
        self.assertEqual((a.price, a.list_price), (19.99, 49.99))

    def test_min_positive_signals_two_requires_two_sources(self):
        url = "https://www.carrefour.es/mini-lata/VC4A-1/p"
        a = det.analyze_product_page(fixture("product_html_only_in_stock.html"), url, min_positive_signals=2)
        self.assertEqual(a.status, Status.UNKNOWN)
        b = det.analyze_product_page(fixture("product_in_stock.html"), URL_CAJA, min_positive_signals=2)
        self.assertEqual(b.status, Status.IN_STOCK)

    def test_contradictory_signals_are_unknown(self):
        a = det.analyze_product_page(fixture("product_conflict.html"), "https://www.carrefour.es/p/VC4A-5/p")
        self.assertEqual(a.status, Status.UNKNOWN)
        self.assertEqual(a.reason, "senales_contradictorias")

    def test_disabled_button_is_out_of_stock(self):
        a = det.analyze_product_page(fixture("product_disabled_button.html"), "https://www.carrefour.es/p/VC4A-6/p")
        self.assertEqual(a.status, Status.OUT_OF_STOCK)

    def test_embedded_json_only_uses_matching_product(self):
        a = det.analyze_product_page(fixture("product_embedded_json.html"), URL_SOBRES)
        self.assertEqual(a.status, Status.IN_STOCK)      # id 34535245 -> InStock; id 99999 (OutOfStock) se ignora
        self.assertEqual(a.price, 15.99)

    def test_unrecognized_page_is_unknown(self):
        a = det.analyze_product_page("<html><body><div id='app'></div></body></html>", URL_SOBRES)
        self.assertEqual(a.status, Status.UNKNOWN)
        self.assertIn("no_reconocida", a.reason)

    def test_adapter_interface_methods(self):
        html = fixture("product_in_stock.html")
        st, signals = self.adapter.detect_stock(html, URL_CAJA)
        self.assertEqual(st, Status.IN_STOCK)
        self.assertTrue(signals)
        self.assertEqual(self.adapter.extract_price(html, URL_CAJA), 54.99)
        meta = self.adapter.extract_metadata(html, URL_CAJA)
        self.assertEqual(meta["sku"], "VC4A-34535246")
        self.assertEqual(meta["ean"], "0196214152298")

    def test_interpret_availability(self):
        self.assertEqual(det.interpret_availability("https://schema.org/InStock"), Status.IN_STOCK)
        self.assertEqual(det.interpret_availability("https://schema.org/OutOfStock"), Status.OUT_OF_STOCK)
        self.assertEqual(det.interpret_availability("SoldOut"), Status.OUT_OF_STOCK)
        self.assertIsNone(det.interpret_availability("https://schema.org/PreOrder"))
        self.assertIsNone(det.interpret_availability("https://schema.org/InStoreOnly"))
        self.assertEqual(det.interpret_availability(False), Status.OUT_OF_STOCK)


class BlockDetectionTests(unittest.TestCase):
    def test_status_codes(self):
        self.assertTrue(det.looks_blocked(403, ""))
        self.assertTrue(det.looks_blocked(429, ""))

    def test_block_pages(self):
        self.assertTrue(det.looks_blocked(200, fixture("blocked.html")))
        self.assertTrue(det.looks_blocked(200, fixture("captcha.html")))

    def test_normal_pages_not_blocked(self):
        self.assertFalse(det.looks_blocked(200, fixture("product_oos.html")))
        self.assertFalse(det.looks_blocked(200, fixture("listing.html")))


class AdapterAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_product_blocked_and_error(self):
        from monitor.models import FetchResult
        from tests.helpers import FakeClient

        adapter = CarrefourAdapter()
        c = FakeClient({URL_SOBRES: FetchResult(False, URL_SOBRES, status_code=429, error="HTTP 429", blocked=True)})
        self.assertEqual((await adapter.get_product(c, URL_SOBRES)).status, Status.BLOCKED)
        c = FakeClient({URL_SOBRES: FetchResult(False, URL_SOBRES, error="timeout")})
        self.assertEqual((await adapter.get_product(c, URL_SOBRES)).status, Status.ERROR)

    async def test_redirect_away_from_product_is_unknown(self):
        from monitor.models import FetchResult
        from tests.helpers import FakeClient

        adapter = CarrefourAdapter()
        res = FetchResult(True, URL_SOBRES, final_url="https://www.carrefour.es/juguetes", status_code=200,
                          text="<html><h1>Juguetes</h1>" + "x" * 300 + "</html>")
        info = await adapter.get_product(FakeClient({URL_SOBRES: res}), URL_SOBRES)
        self.assertEqual(info.status, Status.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
