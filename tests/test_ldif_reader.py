import tempfile
import unittest

from archive_crawl.ldif import (
    LdifError,
    parse_ldif,
)


class WrappingTests(unittest.TestCase):
    def test_folding_is_undone_before_value_is_read(self):
        records = parse_ldif(
            b"dn: cn=x,dc=y\ndescription: a long\n  folded value\n"
        )
        value = records[0].attributes[0].values[0]
        self.assertEqual(value.text, "a long folded value")
        self.assertEqual(value.encoding, "plain")

    def test_folded_base64_is_decoded_as_one_value(self):
        # base64("你好") split across a physical fold.
        body = b"dn: cn=x,dc=y\ndescription:: 5L2g\n 5aW9\n"
        value = parse_ldif(body)[0].attributes[0].values[0]
        self.assertEqual(value.encoding, "base64")
        self.assertEqual(value.data, "你好".encode())
        self.assertEqual(value.text, "你好")

    def test_folded_external_reference_stays_one_reference(self):
        body = (
            b"dn: cn=x,dc=y\n"
            b"member:< https://example.invalid/\n"
            b" very/long/path\n"
        )
        value = parse_ldif(body)[0].attributes[0].values[0]
        self.assertEqual(value.encoding, "external")
        self.assertEqual(value.reference, "https://example.invalid/very/long/path")
        self.assertIsNone(value.data)


class ValueTests(unittest.TestCase):
    def test_base64_and_plain_remain_distinguishable(self):
        body = (
            b"dn: cn=x,dc=y\n"
            b"description: caf\xc3\xa9\n"
            b"description::Y2Fmw6k=\n"
        )
        values = parse_ldif(body)[0].attributes[0].values
        self.assertEqual([v.encoding for v in values], ["plain", "base64"])
        self.assertEqual(values[0].data, values[1].data)

    def test_external_value_is_not_a_string_value(self):
        body = b"dn: cn=x,dc=y\nmember:< https://example.invalid/x\n"
        value = parse_ldif(body)[0].attributes[0].values[0]
        self.assertTrue(value.is_external)
        self.assertIsNone(value.text)
        self.assertIsNone(value.data)

    def test_invalid_base64_is_rejected_with_line_number(self):
        with self.assertRaisesRegex(LdifError, "line 2"):
            parse_ldif(b"dn: cn=x,dc=y\ndescription:: not!base64\n")

    def test_empty_external_reference_is_rejected(self):
        with self.assertRaisesRegex(LdifError, "external"):
            parse_ldif(b"dn: cn=x,dc=y\nmember:< \n")


class AttributeTests(unittest.TestCase):
    def test_multi_valued_attribute_keeps_every_value_in_order(self):
        body = b"dn: cn=x,dc=y\nmember: a\nmember: b\nmember: c\n"
        values = parse_ldif(body)[0].attributes[0].values
        self.assertEqual([v.text for v in values], ["a", "b", "c"])

    def test_options_are_a_separate_form_of_the_attribute(self):
        body = (
            b"dn: cn=x,dc=y\n"
            b"description: plain\n"
            b"description;lang-zh: \xe4\xbd\xa0\xe5\xa5\xbd\n"
            b"description;lang-en: hello\n"
        )
        attributes = parse_ldif(body)[0].attributes
        self.assertEqual(
            [(a.name, a.options) for a in attributes],
            [("description", ()), ("description", ("lang-zh",)),
             ("description", ("lang-en",))],
        )

    def test_entry_uuid_reads_only_the_unoptioned_attribute(self):
        body = b"dn: cn=x,dc=y\nentryUUID: id-1\nentryUUID;binary:: aWQtMg==\n"
        self.assertEqual(parse_ldif(body)[0].entry_uuid, "id-1")

    def test_missing_entry_uuid_is_none(self):
        self.assertIsNone(parse_ldif(b"dn: cn=x,dc=y\ncn: x\n")[0].entry_uuid)

    def test_duplicate_entry_uuid_is_unusable(self):
        body = b"dn: cn=x,dc=y\nentryUUID: a\nentryUUID: b\n"
        self.assertIsNone(parse_ldif(body)[0].entry_uuid)


class ChangeRecordTests(unittest.TestCase):
    def test_modify_operations_keep_file_order_and_type(self):
        body = (
            b"dn: cn=x,dc=y\n"
            b"changetype: modify\n"
            b"add: description\n"
            b"description: one\n"
            b"-\n"
            b"delete: description\n"
            b"description: one\n"
            b"-\n"
            b"replace: description\n"
            b"description: two\n"
            b"-\n"
        )
        record = parse_ldif(body)[0]
        self.assertEqual(record.kind, "change")
        self.assertEqual(
            [m.operation for m in record.modifications],
            ["add", "delete", "replace"],
        )
        self.assertEqual(
            [m.attribute.values[0].text for m in record.modifications],
            ["one", "one", "two"],
        )

    def test_modify_value_attribute_must_match_operation(self):
        body = (
            b"dn: cn=x,dc=y\nchangetype: modify\nadd: description\n"
            b"cn: x\n-\n"
        )
        with self.assertRaisesRegex(LdifError, "does not match"):
            parse_ldif(body)

    def test_modrdn_retains_all_naming_fields(self):
        body = (
            b"dn: ou=Old,dc=y\nchangetype: modrdn\n"
            b"newrdn: ou=New\ndeleteoldrdn: 1\n"
            b"newsuperior: ou=Parent,dc=y\n"
        )
        record = parse_ldif(body)[0]
        self.assertEqual(record.changetype, "modrdn")
        self.assertEqual(record.new_rdn, "ou=New")
        self.assertIs(record.delete_old_rdn, True)
        self.assertEqual(record.new_superior, "ou=Parent,dc=y")

    def test_modrdn_without_deleteoldrdn_is_rejected(self):
        with self.assertRaisesRegex(LdifError, "deleteoldrdn"):
            parse_ldif(b"dn: ou=Old,dc=y\nchangetype: modrdn\nnewrdn: ou=New\n")

    def test_delete_record_must_not_carry_attributes(self):
        with self.assertRaisesRegex(LdifError, "delete"):
            parse_ldif(b"dn: cn=x,dc=y\nchangetype: delete\ncn: x\n")

    def test_raw_text_is_retained_with_line_span(self):
        body = (
            b"version: 1\n\n"
            b"dn: cn=x,dc=y\ncn: x\ndescription: a\n  folded\n\n"
            b"dn: cn=x,dc=y\nchangetype: delete\n"
        )
        records = parse_ldif(body)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].line_number, 3)
        self.assertIn("  folded", records[0].raw_text)
        self.assertEqual(records[1].line_number, 8)

    def test_version_header_is_not_an_entry(self):
        records = parse_ldif(b"version: 1\n")
        self.assertEqual(records, [])

    def test_unknown_changetype_is_rejected(self):
        with self.assertRaisesRegex(LdifError, "changetype"):
            parse_ldif(b"dn: cn=x,dc=y\nchangetype: rename\n")


if __name__ == "__main__":
    unittest.main()
