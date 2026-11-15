import json
import tempfile
import unittest
from pathlib import Path

from archive_crawl import __main__ as cli
from archive_crawl.archive import Archive
from archive_crawl.catalog import Catalog
from archive_crawl.client import Client
from archive_crawl.decoding import records
from archive_crawl.origin import load_scenario, serve


class FixtureIntakeTests(unittest.TestCase):
    def test_declared_exports_are_archived_before_interpretation(self):
        scenario = load_scenario()
        with serve(scenario) as origin, tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "archive.sqlite3"
            summary = cli.intake(origin.base_url, path, "union-a")
            archive = Archive(path)
            try:
                exports = {
                    row["export"]: row
                    for row in archive.connection.execute(
                        "SELECT export, capture_id FROM intake_files"
                    ).fetchall()
                }
                self.assertEqual(set(exports), {"full-1", "change-11", "change-13"})
                # Every interpreted file points at its own archived capture.
                self.assertEqual(len({row["capture_id"] for row in exports.values()}), 3)
                for row in exports.values():
                    self.assertTrue(archive.body(row["capture_id"]))
            finally:
                archive.close()

            self.assertEqual(
                [item["status"] for item in summary["delivered"]],
                ["applied", "applied", "blocked"],
            )
            self.assertIn("refused", summary["catalog"])
            self.assertIn("12", summary["catalog"]["refused"])
            kinds = {item["kind"] for item in summary["pending"]}
            self.assertEqual(kinds, {"blocked_export", "external_reference"})

    def test_closing_the_gap_publishes_without_relabelling_old_rows(self):
        scenario = load_scenario()
        with serve(scenario) as origin, tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "archive.sqlite3"
            archive = Archive(path)
            catalog = Catalog(archive)
            try:
                for item in records(
                    Client(origin.base_url).fetch("/feed").body, "application/json"
                ):
                    response = Client(origin.base_url).fetch(item["path"])
                    capture_id = archive.append(
                        "export", response.url, response.status,
                        response.headers, response.body,
                    )
                    catalog.ingest(
                        capture_id,
                        directory=item["directory"],
                        export=item["export"],
                        sequence=item["sequence"],
                        kind=item["kind"],
                        last_change_sequence=item.get("last_change_sequence"),
                    )
                with self.assertRaises(Exception) as caught:
                    catalog.current_catalog("union-a")
                self.assertIn("12", str(caught.exception))

                filler = b"""dn: ou=New,dc=synthetic,dc=invalid
changetype: modify
add: description
description: sequence twelve
-
"""
                capture_id = archive.append(
                    "export", "/exports/change-12.ldif", 200, {}, filler
                )
                result = catalog.ingest(
                    capture_id,
                    directory="union-a",
                    export="change-12",
                    sequence=12,
                    kind="changes",
                )
                self.assertEqual(result.status, "applied")
                published = catalog.current_catalog("union-a")
                self.assertTrue(published["status"]["contiguous"])
                entities = {item["uuid"]: item for item in published["entities"]}
                self.assertEqual(
                    entities["uuid-1"]["dn"], "ou=New,dc=synthetic,dc=invalid"
                )
                descriptions = [
                    value["text"]
                    for attribute in entities["uuid-1"]["attributes"]
                    if attribute["name"] == "description"
                    for value in attribute["values"]
                ]
                self.assertIn("sequence twelve", descriptions)
                self.assertIn("合成有构", descriptions)
            finally:
                archive.close()

    def test_demo_output_is_json_serialisable(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            cli._demo()
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["operation"], "ldif_intake")
        self.assertEqual(
            payload["raw"]["source_records"][0]["export"], "full-1"
        )
        self.assertEqual(
            payload["interpretation"]["delivered"][2]["status"], "blocked"
        )


if __name__ == "__main__":
    unittest.main()
