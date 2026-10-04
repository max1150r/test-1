import unittest

from network.proxy_manager import ProxyManager, ProxyParseError, parse_proxy_line


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


class ParseTests(unittest.TestCase):
    def test_all_supported_formats(self):
        self.assertEqual(parse_proxy_line("1.2.3.4:80"), "http://1.2.3.4:80")
        self.assertEqual(parse_proxy_line("u:p@1.2.3.4:80"), "http://u:p@1.2.3.4:80")
        self.assertEqual(parse_proxy_line("http://1.2.3.4:80"), "http://1.2.3.4:80")
        self.assertEqual(parse_proxy_line("http://u:p@1.2.3.4:80"), "http://u:p@1.2.3.4:80")
        self.assertEqual(parse_proxy_line("https://u:p@1.2.3.4:443"), "https://u:p@1.2.3.4:443")
        self.assertEqual(parse_proxy_line("socks5://1.2.3.4:1080"), "socks5://1.2.3.4:1080")
        self.assertEqual(parse_proxy_line("1.2.3.4:80:u:p"), "http://u:p@1.2.3.4:80")

    def test_special_chars_in_password_are_encoded(self):
        self.assertEqual(parse_proxy_line("u:p@ss/word@1.2.3.4:80"), "http://u:p%40ss%2Fword@1.2.3.4:80")

    def test_invalid(self):
        for bad in ("abc", "1.2.3.4", "999.1.1.1:80", "1.2.3.4:0", "1.2.3.4:70000", "ftp://1.2.3.4:21", "http://:80"):
            with self.assertRaises(ProxyParseError, msg=bad):
                parse_proxy_line(bad)


class ManagerTests(unittest.TestCase):
    def make(self, n=3, **kw):
        clock = Clock()
        m = ProxyManager(clock=clock, **kw)
        m.load_lines([f"10.0.0.{i}:808{i}" for i in range(1, n + 1)])
        return m, clock

    def test_load_skips_comments_invalid_and_duplicates(self):
        m = ProxyManager()
        loaded, bad = m.load_lines(["# c", "", "1.1.1.1:80", "1.1.1.1:80", "u:secret@2.2.2.2:80", "basura"])
        self.assertEqual((loaded, bad, len(m)), (2, 1, 2))
        self.assertTrue(all("secret" not in str(x) for x in m.invalid))

    def test_round_robin(self):
        m, _ = self.make()
        self.assertEqual([m.acquire().index for _ in range(6)], [1, 2, 3, 1, 2, 3])

    def test_random_strategy_only_returns_available(self):
        m, _ = self.make(strategy="random")
        m.report_failure(m._proxies[0], "x", severe=True)
        self.assertTrue(all(m.acquire().index != 1 for _ in range(50)))

    def test_cooldown_and_recovery(self):
        m, clock = self.make(n=2, cooldown=100, max_failures=2)
        p = m._proxies[0]
        self.assertFalse(m.report_failure(p, "timeout"))
        self.assertTrue(m.report_failure(p, "timeout"))          # 2º fallo seguido => cooldown
        self.assertEqual(m.stats()["disabled"], 1)
        self.assertTrue(all(m.acquire().index == 2 for _ in range(5)))
        clock.t += 101
        self.assertEqual(m.stats()["available"], 2)

    def test_severe_failure_disables_immediately_and_backs_off_exponentially(self):
        m, clock = self.make(n=1, cooldown=10)
        p = m._proxies[0]
        m.report_failure(p, "HTTP 429", severe=True)
        self.assertIsNone(m.acquire())
        self.assertAlmostEqual(m.seconds_until_available(), 10, delta=0.1)
        clock.t += 11
        m.report_failure(p, "HTTP 429", severe=True)               # 2ª vez seguida: 20 s
        self.assertAlmostEqual(m.seconds_until_available(), 20, delta=0.1)
        clock.t += 21
        m.report_success(p, 0.2)
        self.assertEqual(p.strikes, 0)

    def test_stats_and_label_anonymized(self):
        m, _ = self.make(n=1)
        m.load_lines(["user:pass@203.0.113.9:3128"])
        p = m._proxies[1]
        self.assertNotIn("pass", p.label)
        self.assertNotIn("113.9", p.label)
        m.report_success(p, 0.5)
        s = m.stats()
        self.assertEqual((s["total"], s["available"]), (2, 2))
        self.assertEqual(s["per_proxy"][1]["ok"], 1)

    def test_no_proxies_is_not_enabled(self):
        m = ProxyManager()
        self.assertFalse(m.enabled)
        self.assertIsNone(m.acquire())


if __name__ == "__main__":
    unittest.main()
