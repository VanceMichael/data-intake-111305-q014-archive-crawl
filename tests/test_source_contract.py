import hashlib
import json
import unittest

from archive_crawl.client import Client
from archive_crawl.decoding import parse_page, records as decode_records
from archive_crawl.origin import load_scenario, serve


class SourceContractTests(unittest.TestCase):
    def test_domain_observations_preserve_source_distinctions(self):
        scenario = load_scenario()
        records = scenario["source_records"]
        with serve(scenario) as origin:
            client = Client(origin.base_url)
            self.assertEqual(records[0]["last_change_sequence"], 10)
            self.assertEqual(records[2]["sequence"] - records[1]["sequence"], 2)
            original = client.fetch(records[0]["path"]).body.decode()
            rename = client.fetch(records[1]["path"]).body.decode()
            self.assertIn("entryUUID: uuid-1", original)
            self.assertIn("\n  folded", original)
            self.assertIn("changetype: modrdn", rename)

    def test_every_record_has_the_declared_source_identity(self):
        scenario = load_scenario()
        for record in scenario["source_records"]:
            for field in scenario["identity_fields"]:
                self.assertIn(field, record)
                self.assertNotEqual(str(record[field]).strip(), "")


if __name__ == "__main__":
    unittest.main()
