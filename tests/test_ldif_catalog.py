import tempfile
import unittest
from pathlib import Path

from archive_crawl.archive import Archive
from archive_crawl.catalog import (
    Catalog,
    CatalogError,
    ContinuityError,
)


SNAPSHOT = b"""version: 1

dn: ou=Old,dc=synthetic,dc=invalid
objectClass: organizationalUnit
entryUUID: uuid-1
ou: Old
description: a long
  folded value
member: cn=Person,ou=Sub,ou=Old,dc=synthetic,dc=invalid
member:< https://policy.invalid/collective

dn: ou=Sub,ou=Old,dc=synthetic,dc=invalid
objectClass: organizationalUnit
entryUUID: uuid-2
ou: Sub
"""

RENAME = b"""dn: ou=Old,dc=synthetic,dc=invalid
changetype: modrdn
newrdn: ou=New
deleteoldrdn: 1
"""


class CatalogFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "catalog.sqlite3"
        self.archive = Archive(self.path)
        self.catalog = Catalog(self.archive)

    def tearDown(self):
        self.archive.close()
        self.temporary.cleanup()

    def deliver(self, body, *, export, sequence, kind="changes", directory="union-a",
                last_change_sequence=None):
        capture_id = self.archive.append(
            "export", "/exports/" + export, 200, {}, body
        )
        return self.catalog.ingest(
            capture_id,
            directory=directory,
            export=export,
            sequence=sequence,
            kind=kind,
            last_change_sequence=last_change_sequence,
        )

    def baseline(self, body=SNAPSHOT, export="full-1", through=10, directory="union-a"):
        result = self.deliver(
            body, export=export, sequence=0, kind="snapshot",
            directory=directory, last_change_sequence=through,
        )
        self.assertEqual(result.status, "applied")
        return result

    def entities(self, directory="union-a"):
        """Current entities even when the stream is incomplete.

        The strict publication check is exercised separately; these helpers
        inspect the last consistent state.
        """

        return {
            entity["uuid"]: entity
            for entity in self.catalog.current_catalog(
                directory, require_complete=False
            )["entities"]
        }

    def values(self, entity, attribute):
        for item in entity["attributes"]:
            if item["name"].lower() == attribute.lower():
                return [value.get("text") for value in item["values"]]
        return []


class FileIntakeTests(CatalogFixture):
    def test_snapshot_preserves_values_options_and_external_reference(self):
        self.baseline()
        entity = self.entities()["uuid-1"]
        self.assertEqual(self.values(entity, "description"), ["a long folded value"])
        self.assertEqual(entity["dn"], "ou=Old,dc=synthetic,dc=invalid")
        member = [
            item for item in entity["attributes"] if item["name"] == "member"
        ][0]
        self.assertEqual([value["encoding"] for value in member["values"]],
                         ["plain", "external"])
        self.assertIsNone(member["values"][1].get("text"))

    def test_external_reference_is_registered_but_not_fetched(self):
        self.baseline()
        pending = self.catalog.pending("union-a")
        references = [item for item in pending if item["kind"] == "external_reference"]
        self.assertEqual(len(references), 1)
        self.assertEqual(references[0]["detail"], "https://policy.invalid/collective")
        self.assertEqual(references[0]["status"], "pending")

    def test_file_and_record_are_traceable_to_the_capture(self):
        self.baseline()
        entity = self.entities()["uuid-1"]
        source = self.catalog.entity("union-a", "uuid-1")["versions"][0]["source"]
        self.assertEqual(source["capture_id"], 1)
        self.assertEqual(source["export"], "full-1")
        self.assertIn(b"folded", source["raw_text"].encode())
        self.assertEqual(source["status"], "applied")

    def test_repeated_export_is_recorded_without_applying_twice(self):
        self.baseline()
        second = self.deliver(
            SNAPSHOT, export="full-1", sequence=0, kind="snapshot",
            last_change_sequence=10,
        )
        self.assertEqual(second.status, "duplicate")
        self.assertEqual(len(self.entities()), 2)
        self.assertEqual(
            self.catalog.connection.execute("SELECT count(*) FROM intake_files").fetchone()[0],
            2,
        )

    def test_same_export_id_with_different_content_is_a_conflict(self):
        self.baseline()
        other = SNAPSHOT.replace(b"uuid-2", b"uuid-3")
        result = self.deliver(
            other, export="full-1", sequence=0, kind="snapshot",
            last_change_sequence=10,
        )
        self.assertEqual(result.status, "conflict")
        self.assertNotIn("uuid-3", self.catalog.current_catalog(
            "union-a", require_complete=False)["entities"][0]["uuid"])

    def test_identical_older_baseline_is_not_reapplied(self):
        self.baseline(through=10)
        result = self.deliver(
            SNAPSHOT, export="full-0", sequence=0, kind="snapshot",
            last_change_sequence=5,
        )
        self.assertEqual(result.status, "duplicate")
        self.assertEqual(self.catalog.status("union-a").baseline_through, 10)
        self.assertEqual(len(self.entities()), 2)

    def test_different_older_baseline_is_superseded(self):
        self.baseline(through=10)
        older = SNAPSHOT.replace(b"uuid-2", b"uuid-9")
        result = self.deliver(
            older, export="full-0", sequence=0, kind="snapshot",
            last_change_sequence=5,
        )
        self.assertEqual(result.status, "superseded")
        self.assertNotIn("uuid-9", self.entities())

    def test_invalid_ldif_is_kept_as_a_file_but_not_applied(self):
        result = self.deliver(
            b"description: orphan\n", export="bad", sequence=1
        )
        self.assertEqual(result.status, "invalid")
        self.assertIn("dn", result.detail)
        row = self.catalog.connection.execute(
            "SELECT * FROM intake_files WHERE id=?", (result.file_id,)
        ).fetchone()
        self.assertEqual(row["status"], "invalid")


class ContinuityTests(CatalogFixture):
    def test_change_before_baseline_is_held(self):
        result = self.deliver(RENAME, export="change-11", sequence=11)
        self.assertEqual(result.status, "baseline_required")
        status = self.catalog.status("union-a")
        self.assertFalse(status.contiguous)
        self.assertEqual(status.buffered[0]["export"], "change-11")

    def test_gap_holds_later_change_and_refuses_current_catalog(self):
        self.baseline()
        self.assertEqual(
            self.deliver(RENAME, export="change-11", sequence=11).status, "applied"
        )
        later = b"""dn: ou=New,dc=synthetic,dc=invalid
changetype: modify
add: description
description: after the gap
-
"""
        held = self.deliver(later, export="change-13", sequence=13)
        self.assertEqual(held.status, "blocked")
        status = self.catalog.status("union-a")
        self.assertEqual(status.missing, (12,))
        self.assertFalse(status.contiguous)
        with self.assertRaisesRegex(ContinuityError, "12"):
            self.catalog.current_catalog("union-a")
        # The held file is stored, and the last good state is still queryable.
        self.assertEqual(self.entities()["uuid-1"]["dn"],
                         "ou=New,dc=synthetic,dc=invalid")
        self.assertNotIn("after the gap", self.values(self.entities()["uuid-1"], "description"))

    def test_arriving_missing_segment_drains_held_changes(self):
        self.baseline()
        self.deliver(RENAME, export="change-11", sequence=11)
        later = b"""dn: ou=New,dc=synthetic,dc=invalid
changetype: modify
add: description
description: after the gap
-
"""
        self.deliver(later, export="change-13", sequence=13)
        filler = b"""dn: ou=New,dc=synthetic,dc=invalid
changetype: modify
add: description
description: twelve
-
"""
        applied = self.deliver(filler, export="change-12", sequence=12)
        self.assertEqual(applied.status, "applied")
        status = self.catalog.status("union-a")
        self.assertTrue(status.contiguous)
        self.assertEqual(status.applied_through, 13)
        self.assertIn(
            "after the gap",
            self.values(self.entities()["uuid-1"], "description"),
        )
        self.assertEqual(
            self.catalog.connection.execute(
                "SELECT status FROM intake_files WHERE export='change-13'"
            ).fetchone()[0],
            "applied",
        )

    def test_sequence_already_applied_is_superseded(self):
        self.baseline()
        self.deliver(RENAME, export="change-11", sequence=11)
        duplicate = self.deliver(RENAME, export="change-11-b", sequence=11)
        self.assertEqual(duplicate.status, "duplicate")

    def test_newer_baseline_obsoletes_buffered_changes(self):
        self.deliver(RENAME, export="change-11", sequence=11)
        self.baseline(export="full-2", through=20)
        status = self.catalog.status("union-a")
        self.assertTrue(status.contiguous)
        self.assertEqual(status.baseline_through, 20)
        self.assertEqual(status.buffered, ())
        self.assertEqual(
            self.catalog.connection.execute(
                "SELECT status FROM intake_files WHERE export='change-11'"
            ).fetchone()[0],
            "superseded",
        )

    def test_baseline_older_than_applied_changes_does_not_rewind(self):
        self.baseline(through=10)
        self.deliver(RENAME, export="change-11", sequence=11)
        # A full export that predates work already applied must not become
        # "current" by replacing the newer state.
        older = SNAPSHOT.replace(b"uuid-2", b"uuid-9")
        result = self.deliver(
            older, export="full-old", sequence=0, kind="snapshot",
            last_change_sequence=5,
        )
        self.assertEqual(result.status, "superseded")
        self.assertEqual(self.catalog.status("union-a").applied_through, 11)
        self.assertEqual(self.entities()["uuid-1"]["dn"],
                         "ou=New,dc=synthetic,dc=invalid")

    def test_new_baseline_keeps_held_change_at_the_next_sequence(self):
        self.deliver(RENAME, export="change-11", sequence=11)
        self.baseline(export="full-2", through=10)
        status = self.catalog.status("union-a")
        self.assertTrue(status.contiguous)
        self.assertEqual(status.applied_through, 11)


class RenameTests(CatalogFixture):
    def test_rename_keeps_identity_and_old_dn_in_history(self):
        self.baseline()
        self.deliver(RENAME, export="change-11", sequence=11)
        entity = self.entities()["uuid-1"]
        self.assertEqual(entity["dn"], "ou=New,dc=synthetic,dc=invalid")
        self.assertEqual(entity["reason"], "modrdn")
        history = self.catalog.dn_history("union-a", "ou=Old,dc=synthetic,dc=invalid")
        self.assertEqual(history[0]["uuid"], "uuid-1")
        self.assertFalse(history[0]["active"])

    def test_rename_moves_descendants(self):
        self.baseline()
        self.deliver(RENAME, export="change-11", sequence=11)
        child = self.entities()["uuid-2"]
        self.assertEqual(child["dn"], "ou=Sub,ou=New,dc=synthetic,dc=invalid")
        self.assertEqual(child["reason"], "ancestor_rename")
        old = self.catalog.dn_history("union-a", "ou=Sub,ou=Old,dc=synthetic,dc=invalid")
        self.assertFalse(old[0]["active"])

    def test_rename_rewrites_dn_valued_member_below_renamed_entry(self):
        self.baseline()
        self.deliver(RENAME, export="change-11", sequence=11)
        entity = self.entities()["uuid-1"]
        self.assertIn(
            "cn=Person,ou=Sub,ou=New,dc=synthetic,dc=invalid",
            self.values(entity, "member"),
        )

    def test_deleteoldrdn_controls_the_naming_attribute(self):
        self.baseline()
        kept = b"""dn: ou=Old,dc=synthetic,dc=invalid
changetype: modrdn
newrdn: ou=New
deleteoldrdn: 0
"""
        self.deliver(kept, export="change-11", sequence=11)
        self.assertEqual(
            sorted(self.values(self.entities()["uuid-1"], "ou")), ["New", "Old"]
        )

    def test_old_address_of_the_renamed_entry_is_not_left_active(self):
        self.baseline()
        self.deliver(RENAME, export="change-11", sequence=11)
        active = self.catalog.connection.execute(
            "SELECT dn_key FROM dn_bindings WHERE directory='union-a' AND active=1 "
            "ORDER BY dn_key"
        ).fetchall()
        self.assertEqual(
            [row["dn_key"] for row in active],
            [
                "ou=new,dc=synthetic,dc=invalid",
                "ou=sub,ou=new,dc=synthetic,dc=invalid",
            ],
        )

    def test_rename_into_an_address_of_a_live_entity_is_refused(self):
        self.baseline(
            SNAPSHOT
            + b"\ndn: ou=Other,dc=synthetic,dc=invalid\n"
            b"objectClass: organizationalUnit\nentryUUID: uuid-3\nou: Other\n"
        )
        clash = b"""dn: ou=Old,dc=synthetic,dc=invalid
changetype: modrdn
newrdn: ou=Other
deleteoldrdn: 1
"""
        result = self.deliver(clash, export="change-11", sequence=11)
        self.assertEqual(result.unresolved_records, 1)
        with self.assertRaisesRegex(ContinuityError, "unresolved"):
            self.catalog.current_catalog("union-a")

    def test_newsuperior_moves_the_entry_and_its_descendants(self):
        self.baseline(
            SNAPSHOT
            + b"\ndn: ou=Home,dc=synthetic,dc=invalid\n"
            b"objectClass: organizationalUnit\nentryUUID: uuid-5\nou: Home\n"
        )
        move = b"""dn: ou=Old,dc=synthetic,dc=invalid
changetype: modrdn
newrdn: ou=Old
deleteoldrdn: 1
newsuperior: ou=Home,dc=synthetic,dc=invalid
"""
        self.deliver(move, export="change-11", sequence=11)
        entities = self.entities()
        self.assertEqual(
            entities["uuid-1"]["dn"],
            "ou=Old,ou=Home,dc=synthetic,dc=invalid",
        )
        self.assertEqual(
            entities["uuid-2"]["dn"],
            "ou=Sub,ou=Old,ou=Home,dc=synthetic,dc=invalid",
        )
        self.assertEqual(
            self.catalog.dn_history("union-a", "ou=Old,dc=synthetic,dc=invalid")[0]["active"],
            False,
        )

    def test_moving_an_entry_beneath_itself_is_refused(self):
        self.baseline()
        cycle = b"""dn: ou=Old,dc=synthetic,dc=invalid
changetype: modrdn
newrdn: ou=Old
deleteoldrdn: 1
newsuperior: ou=Sub,ou=Old,dc=synthetic,dc=invalid
"""
        result = self.deliver(cycle, export="change-11", sequence=11)
        self.assertEqual(result.unresolved_records, 1)
        self.assertEqual(self.entities()["uuid-1"]["dn"],
                         "ou=Old,dc=synthetic,dc=invalid")

    def test_rename_without_active_predecessor_is_left_for_evidence(self):
        self.baseline()
        orphan = b"""dn: ou=Missing,dc=synthetic,dc=invalid
changetype: modrdn
newrdn: ou=Elsewhere
deleteoldrdn: 1
"""
        result = self.deliver(orphan, export="change-11", sequence=11)
        self.assertEqual(result.unresolved_records, 1)
        pending = [item for item in self.catalog.pending("union-a")
                   if item["kind"] == "unresolved_record"]
        self.assertIn("predecessor", pending[0]["detail"])


class IdentityReuseTests(CatalogFixture):
    def test_old_dn_can_be_reused_by_another_entity(self):
        self.baseline()
        self.deliver(RENAME, export="change-11", sequence=11)
        reuse = b"""dn: ou=Old,dc=synthetic,dc=invalid
changetype: add
objectClass: organizationalUnit
entryUUID: uuid-3
ou: Old
"""
        result = self.deliver(reuse, export="change-12", sequence=12)
        self.assertEqual(result.status, "applied")
        entities = self.entities()
        self.assertEqual(entities["uuid-1"]["dn"], "ou=New,dc=synthetic,dc=invalid")
        self.assertEqual(entities["uuid-3"]["dn"], "ou=Old,dc=synthetic,dc=invalid")
        history = self.catalog.dn_history("union-a", "ou=Old,dc=synthetic,dc=invalid")
        self.assertEqual([item["uuid"] for item in history], ["uuid-1", "uuid-3"])
        self.assertEqual([item["active"] for item in history], [False, True])

    def test_add_for_an_active_entry_uuid_is_refused(self):
        self.baseline()
        duplicate = b"""dn: ou=Other,dc=synthetic,dc=invalid
changetype: add
objectClass: organizationalUnit
entryUUID: uuid-1
ou: Other
"""
        result = self.deliver(duplicate, export="change-11", sequence=11)
        self.assertEqual(result.unresolved_records, 1)
        self.assertNotIn(
            "ou=Other,dc=synthetic,dc=invalid",
            [entity["dn"] for entity in self.entities().values()],
        )

    def test_newer_baseline_that_reuses_a_dn_keeps_both_holders(self):
        self.baseline()
        replaced = SNAPSHOT.replace(b"ou=Old,dc=synthetic,dc=invalid\n", b"ou=Newer,dc=synthetic,dc=invalid\n", 1)
        replaced = replaced.replace(b"entryUUID: uuid-1", b"entryUUID: uuid-1")
        # uuid-1 now lives at a new address and uuid-4 takes the old one.
        added = (
            b"\ndn: ou=Old,dc=synthetic,dc=invalid\n"
            b"objectClass: organizationalUnit\nentryUUID: uuid-4\nou: Old\n"
        )
        result = self.deliver(
            replaced + added, export="full-2", sequence=0, kind="snapshot",
            last_change_sequence=20,
        )
        self.assertEqual(result.status, "applied")
        entities = self.entities()
        self.assertEqual(entities["uuid-1"]["dn"], "ou=Newer,dc=synthetic,dc=invalid")
        self.assertEqual(entities["uuid-4"]["dn"], "ou=Old,dc=synthetic,dc=invalid")
        history = self.catalog.dn_history("union-a", "ou=Old,dc=synthetic,dc=invalid")
        self.assertEqual([item["uuid"] for item in history], ["uuid-1", "uuid-4"])

    def test_entry_absent_from_a_newer_baseline_is_closed_not_forgotten(self):
        self.baseline()
        only_one = SNAPSHOT.split(b"\ndn: ou=Sub")[0] + b"\n"
        self.deliver(
            only_one, export="full-2", sequence=0, kind="snapshot",
            last_change_sequence=20,
        )
        self.assertNotIn("uuid-2", self.entities())
        history = self.catalog.entity("union-a", "uuid-2")
        self.assertTrue(history["versions"][-1]["deleted"])
        self.assertEqual(history["versions"][-1]["reason"], "absent_from_baseline")


class VersionQueryTests(CatalogFixture):
    def test_optioned_attribute_is_not_touched_by_unoptioned_modify(self):
        self.baseline(
            SNAPSHOT.replace(
                b"ou: Old\n",
                b"ou: Old\ndescription;lang-zh:: 5L2g5aW9\n",
            )
        )
        body = b"""dn: ou=Old,dc=synthetic,dc=invalid
changetype: modify
replace: description
description: reset
-
"""
        self.deliver(body, export="change-11", sequence=11)
        entity = self.entities()["uuid-1"]
        options = {
            tuple(attribute["options"]): attribute["values"]
            for attribute in entity["attributes"]
            if attribute["name"] == "description"
        }
        self.assertEqual(options[()][0]["text"], "reset")
        self.assertEqual(options[("lang-zh",)][0]["text"], "\u4f60\u597d")

    def test_modify_appends_a_version_and_orders_operations(self):
        self.baseline()
        body = b"""dn: ou=Old,dc=synthetic,dc=invalid
changetype: modify
delete: description
description: a long folded value
-
add: description
description: replacement
-
"""
        self.deliver(body, export="change-11", sequence=11)
        history = self.catalog.entity("union-a", "uuid-1")["versions"]
        self.assertEqual([item["version"] for item in history], [1, 2])
        self.assertEqual(
            self.values(history[1], "description"), ["replacement"]
        )
        self.assertEqual(
            self.values(history[0], "description"), ["a long folded value"]
        )

    def test_reversed_operations_are_not_equivalent(self):
        # delete-then-add leaves "replacement"; add-then-delete would leave the
        # original.  The catalogue must follow the file order it received.
        self.baseline()
        body = b"""dn: ou=Old,dc=synthetic,dc=invalid
changetype: modify
add: description
description: replacement
-
delete: description
description: a long folded value
-
"""
        self.deliver(body, export="change-11", sequence=11)
        entity = self.entities()["uuid-1"]
        self.assertEqual(
            sorted(self.values(entity, "description")),
            ["replacement"],
        )

    def test_multi_valued_add_and_delete_touch_only_named_values(self):
        self.baseline()
        body = b"""dn: ou=Old,dc=synthetic,dc=invalid
changetype: modify
add: description
description: second
description: third
-
delete: description
description: second
-
"""
        self.deliver(body, export="change-11", sequence=11)
        entity = self.entities()["uuid-1"]
        self.assertEqual(
            sorted(self.values(entity, "description")),
            ["a long folded value", "third"],
        )

    def test_replace_with_no_values_removes_the_attribute(self):
        self.baseline()
        body = b"""dn: ou=Old,dc=synthetic,dc=invalid
changetype: modify
replace: description
-
"""
        self.deliver(body, export="change-11", sequence=11)
        self.assertEqual(self.values(self.entities()["uuid-1"], "description"), [])

    def test_as_of_sequence_shows_earlier_state(self):
        self.baseline()
        self.deliver(RENAME, export="change-11", sequence=11)
        before = {item["uuid"]: item for item in self.catalog.as_of("union-a", 10)}
        after = {item["uuid"]: item for item in self.catalog.as_of("union-a", 11)}
        self.assertEqual(before["uuid-1"]["dn"], "ou=Old,dc=synthetic,dc=invalid")
        self.assertEqual(after["uuid-1"]["dn"], "ou=New,dc=synthetic,dc=invalid")

    def test_delete_closes_the_entry_but_keeps_history(self):
        self.baseline()
        body = b"""dn: ou=Sub,ou=Old,dc=synthetic,dc=invalid
changetype: delete
"""
        self.deliver(body, export="change-11", sequence=11)
        self.assertNotIn("uuid-2", self.entities())
        history = self.catalog.entity("union-a", "uuid-2")
        self.assertEqual(history["versions"][-1]["reason"], "delete")
        self.assertTrue(history["versions"][-1]["deleted"])

    def test_parent_delete_while_descendant_exists_is_unresolved(self):
        self.baseline()
        body = b"""dn: ou=Old,dc=synthetic,dc=invalid
changetype: delete
"""
        result = self.deliver(body, export="change-11", sequence=11)
        self.assertEqual(result.unresolved_records, 1)
        unresolved = [
            item for item in self.catalog.pending("union-a")
            if item["kind"] == "unresolved_record"
        ]
        self.assertIn("descendants", unresolved[0]["detail"])

    def test_directories_do_not_share_identity(self):
        self.baseline(directory="union-a")
        result = self.deliver(
            SNAPSHOT, export="full-1", sequence=0, kind="snapshot",
            directory="union-b", last_change_sequence=10,
        )
        self.assertEqual(result.status, "applied")
        self.assertEqual(
            self.catalog.connection.execute(
                "SELECT count(*) FROM entities WHERE directory='union-a'"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.catalog.connection.execute(
                "SELECT count(*) FROM entities WHERE directory='union-b'"
            ).fetchone()[0],
            2,
        )


class ValidationTests(CatalogFixture):
    def test_changes_export_cannot_declare_a_baseline_boundary(self):
        with self.assertRaisesRegex(CatalogError, "last_change_sequence"):
            self.deliver(
                RENAME, export="change-11", sequence=11,
                last_change_sequence=20,
            )

    def test_snapshot_must_declare_its_boundary(self):
        with self.assertRaisesRegex(CatalogError, "last_change_sequence"):
            self.deliver(SNAPSHOT, export="full-1", sequence=0, kind="snapshot")

    def test_entry_uuid_cannot_be_modified(self):
        self.baseline()
        body = b"""dn: ou=Old,dc=synthetic,dc=invalid
changetype: modify
replace: entryUUID
entryUUID: uuid-9
-
"""
        result = self.deliver(body, export="change-11", sequence=11)
        self.assertEqual(result.unresolved_records, 1)
        self.assertEqual(self.entities()["uuid-1"]["dn"], "ou=Old,dc=synthetic,dc=invalid")

    def test_delete_of_a_value_the_entry_does_not_have_is_unresolved(self):
        self.baseline()
        body = b"""dn: ou=Old,dc=synthetic,dc=invalid
changetype: modify
delete: description
description: never present
-
"""
        result = self.deliver(body, export="change-11", sequence=11)
        self.assertEqual(result.unresolved_records, 1)

    def test_failed_record_does_not_leave_a_partial_version(self):
        self.baseline()
        body = b"""dn: ou=Old,dc=synthetic,dc=invalid
changetype: modify
add: description
description: first
-
delete: ou
ou: not-present
-
"""
        self.deliver(body, export="change-11", sequence=11)
        versions = self.catalog.entity("union-a", "uuid-1")["versions"]
        self.assertEqual(len(versions), 1)
        self.assertEqual(self.entities()["uuid-1"]["reason"], "snapshot")

    def test_unknown_capture_is_rejected(self):
        with self.assertRaisesRegex(CatalogError, "unknown capture"):
            self.catalog.ingest(
                999, directory="union-a", export="x", sequence=1, kind="changes"
            )


if __name__ == "__main__":
    unittest.main()
