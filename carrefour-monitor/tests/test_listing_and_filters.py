import unittest

from monitor.filters import SearchFilter
from monitor.models import Status
from stores.carrefour.adapter import CarrefourAdapter
from tests.helpers import LISTING_URL, fixture


class ListingParserTests(unittest.TestCase):
    def setUp(self):
        self.items = CarrefourAdapter().parse_listing(fixture("listing.html"), LISTING_URL)
        self.by_sku = {i.sku: i for i in self.items}

    def test_all_products_found_once(self):
        self.assertEqual(len(self.items), 5)
        self.assertEqual(len(self.by_sku), 5)

    def test_in_stock_card_ignores_secondary_prices(self):
        i = self.by_sku["VC4A-34535256"]
        self.assertEqual(i.status, Status.IN_STOCK)
        self.assertEqual(i.price, 29.99)           # no 81,40 ("desde") ni 49 ("superiores a")
        self.assertEqual(i.seller, "Carrefour")
        self.assertIn("783612", i.image)
        self.assertTrue(i.url.endswith("/VC4A-34535256/p"))
        self.assertTrue(i.name.startswith("Pokémon 30Th Aniversario Caja Colección"))

    def test_out_of_stock_card(self):
        self.assertEqual(self.by_sku["VC4A-34535250"].status, Status.OUT_OF_STOCK)

    def test_discounted_price(self):
        i = self.by_sku["VC4A-34428370"]
        self.assertEqual((i.price, i.list_price), (19.99, 49.99))
        self.assertEqual(i.seller, "DRIM")

    def test_lazy_cards_are_unknown_but_have_identity(self):
        i = self.by_sku["VC4A-34670758"]
        self.assertEqual(i.status, Status.UNKNOWN)
        self.assertIsNone(i.price)
        self.assertEqual(i.name, "Lata Coleccion Megaevolucion Ex Pokemon")
        self.assertTrue(i.url.startswith("https://www.carrefour.es/"))

    def test_page_and_search_urls(self):
        a = CarrefourAdapter()
        self.assertEqual(a.build_search_url("Pokemon 30 aniversario"), "https://www.carrefour.es/?q=Pokemon%2030%20aniversario")
        self.assertIn("offset=24", a.build_search_url("pokemon", 1))
        self.assertTrue(a.is_product_url("https://www.carrefour.es/x/VC4A-1/p"))
        self.assertFalse(a.is_product_url("https://www.example.com/x/VC4A-1/p"))
        self.assertFalse(a.is_product_url("https://carrefour.es.evil.com/x"))


class FilterTests(unittest.TestCase):
    def test_include_exclude_accents_case(self):
        f = SearchFilter(query="pokemon", include_keywords=["30 ANIVERSARIO", "elite trainer"],
                         exclude_keywords=["álbum", "carpeta"], max_price=100)
        self.assertTrue(f.evaluate("Pokémon 30Th Aniversario Caja Ex", 34.99)[0] is False)   # "30th aniversario" != "30 aniversario"
        self.assertTrue(f.evaluate("Pokemon Elite Trainer Box 30 Aniversario", 54.99)[0])
        self.assertFalse(f.evaluate("Pokémon ÁLBUM Elite Trainer", 10)[0])                 # acento/mayúsculas
        self.assertFalse(f.evaluate("Pokemon Elite Trainer Box", 150)[0])                  # max_price
        self.assertTrue(f.evaluate("Pokemon Elite Trainer Box", None)[0])                  # sin precio => no se descarta

    def test_min_price(self):
        f = SearchFilter(query="", min_price=20)
        self.assertFalse(f.evaluate("Algo", 10)[0])
        self.assertTrue(f.evaluate("Algo", 20)[0])

    def test_query_match_modes(self):
        name = "Pokémon 30Th Aniversario Caja"
        self.assertTrue(SearchFilter(query="pokemon 30 aniversario", query_match="all").evaluate(name, 1)[0])
        self.assertFalse(SearchFilter(query="pokemon elite", query_match="all").evaluate(name, 1)[0])
        self.assertTrue(SearchFilter(query="pokemon elite", query_match="any").evaluate(name, 1)[0])
        self.assertTrue(SearchFilter(query="zzz", query_match="none").evaluate(name, 1)[0])

    def test_include_mode_all(self):
        f = SearchFilter(include_keywords=["caja", "aniversario"], include_mode="all", query_match="none")
        self.assertTrue(f.evaluate("Caja 30 Aniversario", 1)[0])
        self.assertFalse(f.evaluate("Caja Elite", 1)[0])


if __name__ == "__main__":
    unittest.main()
