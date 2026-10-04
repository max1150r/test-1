import unittest

from utils.text import canonical_url, find_prices, format_eur, normalize, parse_price, with_query_param


class TextTests(unittest.TestCase):
    def test_parse_price(self):
        self.assertEqual(parse_price("15,99 €", from_text=True), 15.99)
        self.assertEqual(parse_price("1.299,99 €", from_text=True), 1299.99)
        self.assertEqual(parse_price("1.299 €", from_text=True), 1299.0)
        self.assertEqual(parse_price("19.54"), 19.54)       # JSON-LD
        self.assertEqual(parse_price(54.99), 54.99)
        self.assertIsNone(parse_price("sin precio"))
        self.assertIsNone(parse_price(None))
        self.assertIsNone(parse_price(True))

    def test_find_prices(self):
        self.assertEqual(find_prices("49,99 € 19,99 € Más ofertas desde 81,40 €"), [49.99, 19.99, 81.40])

    def test_normalize_accents_case(self):
        self.assertEqual(normalize("  Megaevolución  ÁLBUM "), "megaevolucion album")

    def test_format_eur(self):
        self.assertEqual(format_eur(1299.5), "1.299,50 €")
        self.assertEqual(format_eur(None), "N/D")

    def test_urls(self):
        self.assertEqual(canonical_url("HTTPS://WWW.Carrefour.es/a/b/p?x=1#y"), "https://www.carrefour.es/a/b/p")
        self.assertIn("offset=24", with_query_param("https://x.es/c?filter=a", "offset", 24))
        self.assertEqual(with_query_param("https://x.es/c?offset=24", "offset", 48).count("offset"), 1)


if __name__ == "__main__":
    unittest.main()
