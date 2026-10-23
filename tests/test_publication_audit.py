"""Publication scans detect leaks without echoing their contents."""
import unittest
from test_agent_reports import ROOT, load


class PublicationAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.audit = load("publication_audit", ROOT / "scripts/audit-publication.py")

    def test_credentials_private_files_and_explicit_identifiers_are_detected(self):
        self.assertTrue(self.audit.findings("infra/example.tfstate", "{}"))
        self.assertTrue(self.audit.findings("sample.py", "AKIA" + "X" * 16))
        self.assertEqual(self.audit.findings("README.md", "private.example", ("private.example",)),
                         [(1, "forbidden-identifier")])

    def test_documentation_addresses_are_allowed_but_private_prose_is_reported(self):
        self.assertFalse(self.audit.findings("README.md", "192.0.2.10 and 127.0.0.1"))
        self.assertTrue(self.audit.findings("docs/setup.md", "10.0.0.7"))
        self.assertFalse(self.audit.findings("tests/example.py", "10.0.0.7"))
        self.assertFalse(self.audit.findings(".env.example", "API_KEY=replace-me"))
