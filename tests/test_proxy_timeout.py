"""The cold-loading timeout change must affect only the PeerFoundry route."""
import unittest
from test_agent_reports import ROOT, load


class ProxyTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.module = load("proxy_timeout_test", ROOT / "scripts/configure-peerfoundry-proxy.py")
        self.block = "location ^~ /peerfoundry/ {\n    proxy_read_timeout 180s;\n    proxy_buffering off;\n}\n"

    def test_changes_only_peerfoundry_timeout_and_is_idempotent(self):
        source = "location /another-app/ { proxy_read_timeout 180s; }\n" + self.block
        changed = self.module.update_timeout(source)
        self.assertIn("location /another-app/ { proxy_read_timeout 180s; }", changed)
        self.assertIn("proxy_read_timeout 3660s;", changed)
        self.assertEqual(changed, self.module.update_timeout(changed))

    def test_refuses_missing_duplicate_or_unrecognized_target(self):
        for source in ("location /other/ { proxy_read_timeout 180s; }", self.block * 2,
                       self.block.replace("180s", "90s")):
            with self.assertRaises(ValueError):
                self.module.update_timeout(source)


if __name__ == "__main__":
    unittest.main()
