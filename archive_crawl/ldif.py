"""RFC 2849 LDIF reader for the institutional directory intake.

The reader keeps the distinctions that matter to the archive:

* physical line folding is undone before a value is interpreted;
* base64 values are decoded but remain distinguishable from plain values;
* ``attr:< URL`` values are external references, never ordinary strings;
* attribute options (``description;lang-zh``) and repeated values are kept;
* change records keep operation order and all ``modrdn`` fields.

The module does not fetch external values and does not talk to an LDAP
server.  It only turns a received export into an ordered list of records.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from typing import Iterable

_HEX = set("0123456789abcdefABCDEF")
_ATTRIBUTE_TYPE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9-]*|[0-9]+(?:\.[0-9]+)*)$")
_OPTION = re.compile(r"^[A-Za-z0-9-]+$")

PLAIN = "plain"
BASE64 = "base64"
EXTERNAL = "external"


class LdifError(ValueError):
    """Raised when an export cannot be read as RFC 2849 LDIF."""

    def __init__(self, message, line_number=None):
        self.line_number = line_number
        if line_number is not None:
            message = "line %s: %s" % (line_number, message)
        super().__init__(message)


@dataclass(frozen=True)
class LdifValue:
    """One LDIF value, preserving how the supplier encoded it.

    ``encoding`` is ``plain``, ``base64`` or ``external``.  An external value
    has no data by design: the supplier declared an address, not a value.
    """

    encoding: str
    text: str | None = None
    data: bytes | None = None
    reference: str | None = None

    @classmethod
    def plain(cls, text):
        return cls(PLAIN, text=text, data=text.encode("utf-8"))

    @classmethod
    def from_base64(cls, encoded, line_number=None):
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise LdifError("invalid base64 value: %s" % error, line_number) from error
        text = _decode_text(data)
        return cls(BASE64, text=text, data=data)

    @classmethod
    def external(cls, reference):
        return cls(EXTERNAL, reference=reference)

    @property
    def is_external(self):
        return self.encoding == EXTERNAL

    def identity(self):
        """Value identity for LDAP set semantics (encoding independent)."""
        if self.encoding == EXTERNAL:
            return ("external", self.reference)
        return ("data", self.data)

    def as_dict(self):
        result = {"encoding": self.encoding}
        if self.encoding == EXTERNAL:
            result["reference"] = self.reference
        else:
            result["text"] = self.text
            if self.encoding == BASE64:
                result["data"] = base64.b64encode(self.data).decode("ascii")
        return result


def _decode_text(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


@dataclass(frozen=True)
class Attribute:
    """An attribute description and its ordered values."""

    name: str
    options: tuple[str, ...]
    values: tuple[LdifValue, ...] = ()

    @property
    def name_key(self):
        return self.name.lower()

    @property
    def options_key(self):
        return tuple(sorted(option.lower() for option in self.options))

    @property
    def description(self):
        if self.options:
            return self.name + ";" + ";".join(self.options)
        return self.name

    @property
    def key(self):
        return (self.name_key, self.options_key)

    def with_values(self, values):
        return Attribute(self.name, self.options, tuple(values))


@dataclass(frozen=True)
class Modification:
    """One add/delete/replace operation, in file order."""

    operation: str
    attribute: Attribute

    @property
    def name(self):
        return self.attribute.name

    @property
    def options(self):
        return self.attribute.options


@dataclass(frozen=True)
class LdifRecord:
    kind: str  # "entry" or "change"
    dn: str
    line_number: int
    attributes: tuple[Attribute, ...] = ()
    changetype: str | None = None
    modifications: tuple[Modification, ...] = ()
    new_rdn: str | None = None
    delete_old_rdn: bool | None = None
    new_superior: str | None = None
    raw_text: str = ""
    line_end: int | None = None

    @property
    def entry_uuid(self):
        """Return the plain entryUUID value, if exactly one is present.

        ``None`` means absent, malformed or externally declared.  The
        directory layer treats all three as "identity not established".
        """
        matches = [
            attribute
            for attribute in self.attributes
            if attribute.name_key == "entryuuid" and not attribute.options
        ]
        if len(matches) != 1 or len(matches[0].values) != 1:
            return None
        value = matches[0].values[0]
        if value.is_external or value.text is None or value.text == "":
            return None
        return value.text


def parse_attribute_description(description, line_number=None):
    parts = description.split(";")
    name = parts[0].strip()
    if not name or not _ATTRIBUTE_TYPE.match(name):
        raise LdifError("invalid attribute type %r" % description, line_number)
    options = []
    for option in parts[1:]:
        option = option.strip()
        if not option or not _OPTION.match(option):
            raise LdifError("invalid attribute option %r" % description, line_number)
        options.append(option)
    return name, tuple(options)


def _split_value_line(line, line_number):
    """Split ``attr: value`` / ``attr:: base64`` / ``attr:< URL``."""

    colon = line.find(":")
    if colon <= 0:
        raise LdifError("missing ':' in line", line_number)
    description = line[:colon]
    rest = line[colon + 1 :]
    if rest.startswith("<"):
        return description, EXTERNAL, rest[1:].lstrip(" ")
    if rest.startswith(":"):
        return description, BASE64, rest[1:].lstrip(" ")
    return description, PLAIN, rest.lstrip(" ")


def _value(encoding, raw, line_number):
    if encoding == EXTERNAL:
        if not raw:
            raise LdifError("empty external value reference", line_number)
        return LdifValue.external(raw)
    if encoding == BASE64:
        return LdifValue.from_base64(raw, line_number)
    return LdifValue.plain(raw)


def _add_attribute(attributes, name, options, value):
    """Append a value to the matching attribute, preserving first spelling."""

    key = (name.lower(), tuple(sorted(option.lower() for option in options)))
    for index, attribute in enumerate(attributes):
        if attribute.key == key:
            attributes[index] = attribute.with_values(
                attribute.values + (value,)
            )
            return
    attributes.append(Attribute(name, tuple(options), (value,)))


def _text_field(encoding, raw, line_number, field):
    value = _value(encoding, raw, line_number)
    if value.is_external:
        raise LdifError("%s cannot be an external value" % field, line_number)
    if value.text is None:
        raise LdifError("%s is not valid UTF-8 text" % field, line_number)
    return value.text


def _parse_record(lines):
    if not lines:
        return None
    start = lines[0][0]
    attributes = []
    dn = None
    changetype = None
    new_rdn = None
    delete_old_rdn = None
    new_superior = None
    operations = []  # dicts, in file order
    current_operation = None
    saw_version = False
    saw_control = False

    for line_number, line in lines:
        if line == "-":
            if current_operation is None:
                raise LdifError("separator outside a modify operation", line_number)
            current_operation = None
            continue

        description, encoding, raw = _split_value_line(line, line_number)
        name, options = parse_attribute_description(description, line_number)
        name_key = name.lower()

        if dn is None and changetype is None:
            if name_key == "version":
                if saw_version:
                    raise LdifError("duplicate version line", line_number)
                saw_version = True
                if raw.strip() != "1":
                    raise LdifError("unsupported LDIF version %r" % raw, line_number)
                continue
            if name_key == "control":
                saw_control = True
                continue
            if name_key == "dn":
                dn = _text_field(encoding, raw, line_number, "dn")
                continue
            raise LdifError("record does not start with dn", line_number)

        if name_key == "changetype":
            if changetype is not None:
                raise LdifError("duplicate changetype", line_number)
            changetype = _text_field(encoding, raw, line_number, "changetype").lower()
            if changetype not in ("add", "delete", "modify", "modrdn"):
                raise LdifError(
                    "unsupported changetype %r" % changetype, line_number
                )
            continue

        if changetype == "modrdn":
            text = _text_field(encoding, raw, line_number, name)
            if name_key == "newrdn":
                new_rdn = text
            elif name_key == "deleteoldrdn":
                if text not in ("0", "1"):
                    raise LdifError("deleteoldrdn must be 0 or 1", line_number)
                delete_old_rdn = text == "1"
            elif name_key == "newsuperior":
                new_superior = text
            elif name_key == "control":
                saw_control = True
            else:
                raise LdifError(
                    "unexpected field %r in modrdn record" % name, line_number
                )
            continue

        if changetype == "modify":
            if name_key in ("add", "delete", "replace"):
                operation_name, operation_options = parse_attribute_description(
                    raw, line_number
                )
                current_operation = {
                    "operation": name_key,
                    "name": operation_name,
                    "options": operation_options,
                    "values": [],
                    "line_number": line_number,
                }
                operations.append(current_operation)
                continue
            if current_operation is None:
                raise LdifError("value outside a modify operation", line_number)
            operation_key = current_operation["name"].lower()
            operation_options = tuple(
                option.lower() for option in current_operation["options"]
            )
            if name_key != operation_key:
                raise LdifError(
                    "value attribute %r does not match operation %r"
                    % (name, current_operation["name"]),
                    line_number,
                )
            if options and tuple(option.lower() for option in options) != operation_options:
                raise LdifError(
                    "value attribute options do not match operation %r"
                    % current_operation["name"],
                    line_number,
                )
            current_operation["values"].append(
                _value(encoding, raw, line_number)
            )
            continue

        if changetype == "delete":
            if name_key == "control":
                saw_control = True
                continue
            raise LdifError("delete record must not carry attributes", line_number)

        # A normal entry, or the attribute block of changetype: add.
        _add_attribute(
            attributes, name, options, _value(encoding, raw, line_number)
        )

    if dn is None:
        if saw_version or saw_control:
            return None
        raise LdifError("record has no dn", start)
    if changetype == "modrdn" and not new_rdn:
        raise LdifError("modrdn record has no newrdn", start)
    if changetype == "modrdn" and delete_old_rdn is None:
        raise LdifError("modrdn record has no deleteoldrdn", start)
    if changetype == "modify" and not operations:
        raise LdifError("modify record has no modifications", start)
    if changetype == "delete" and attributes:
        raise LdifError("delete record must not carry attributes", start)

    modifications = []
    for operation in operations:
        modifications.append(
            Modification(
                operation["operation"],
                Attribute(
                    operation["name"],
                    tuple(operation["options"]),
                    tuple(operation["values"]),
                ),
            )
        )

    return LdifRecord(
        kind="entry" if changetype is None else "change",
        dn=dn,
        line_number=start,
        attributes=tuple(attributes),
        changetype=changetype,
        modifications=tuple(modifications),
        new_rdn=new_rdn,
        delete_old_rdn=delete_old_rdn,
        new_superior=new_superior,
    )


def _physical_records(text):
    """Split into physical records at blank lines.

    The raw block is kept verbatim (including comments and folded lines) so an
    archivist can later be shown exactly what the supplier sent.
    """

    lines = text.splitlines()
    current = []
    start = None
    for number, line in enumerate(lines, 1):
        if line.strip() == "":
            if current:
                yield start, number - 1, "\n".join(current), current
            current = []
            start = None
            continue
        if not current:
            start = number
        current.append(line)
    if current:
        yield start, len(lines), "\n".join(current), current


def parse_ldif(source):
    """Parse LDIF text/bytes into records.

    ``version: 1`` is a file header, not an entry.  Every other record needs a
    ``dn``; change records keep ``changetype`` and operation order.
    """

    if isinstance(source, bytes):
        try:
            text = source.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise LdifError("LDIF is not valid UTF-8: %s" % error) from error
    elif isinstance(source, str):
        text = source
        if text.startswith("﻿"):
            text = text[1:]
    else:
        raise TypeError("LDIF source must be bytes or str")

    records = []
    for start, end, raw, block in _physical_records(text):
        numbered = [
            (start + offset, line) for offset, line in enumerate(block)
        ]
        record = _parse_record(list(_logical_lines_with_numbers(numbered)))
        if record is None:
            continue
        records.append(
            LdifRecord(
                kind=record.kind,
                dn=record.dn,
                line_number=start,
                attributes=record.attributes,
                changetype=record.changetype,
                modifications=record.modifications,
                new_rdn=record.new_rdn,
                delete_old_rdn=record.delete_old_rdn,
                new_superior=record.new_superior,
                raw_text=raw,
                line_end=end,
            )
        )
    return records


def _logical_lines_with_numbers(numbered):
    """Undo folding while keeping the original physical line numbers."""

    buffer = None
    start = None
    for number, line in numbered:
        if line.startswith(" "):
            if buffer is None:
                raise LdifError("continuation line without a predecessor", number)
            buffer += line[1:]
            continue
        if buffer is not None:
            yield start, buffer
            buffer = None
            start = None
        if line.startswith("#"):
            continue
        buffer = line
        start = number
    if buffer is not None:
        yield start, buffer



# ---------------------------------------------------------------------------
# RFC 4514 distinguished-name handling
# ---------------------------------------------------------------------------


def _split_escaped(text, separator):
    """Split on an unescaped separator, respecting quotes."""

    parts = []
    current = []
    escaped = False
    quoted = False
    for character in text:
        if escaped:
            current.append(character)
            escaped = False
            continue
        if character == "\\":
            current.append(character)
            escaped = True
            continue
        if character == '"':
            quoted = not quoted
            current.append(character)
            continue
        if character == separator and not quoted:
            parts.append("".join(current))
            current = []
            continue
        current.append(character)
    if escaped:
        raise LdifError("trailing backslash in distinguished name")
    if quoted:
        raise LdifError("unbalanced quotes in distinguished name")
    parts.append("".join(current))
    return parts


def _unescape_value(value):
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1]
    raw = bytearray()
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\\":
            if index + 1 >= len(value):
                raise LdifError("trailing backslash in distinguished name")
            first = value[index + 1]
            if (
                index + 2 < len(value)
                and first in _HEX
                and value[index + 2] in _HEX
            ):
                raw.append(int(value[index + 1 : index + 3], 16))
                index += 3
                continue
            raw.extend(first.encode("utf-8"))
            index += 2
            continue
        raw.extend(character.encode("utf-8"))
        index += 1
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise LdifError("distinguished name is not valid UTF-8") from error


def _escape_value(value):
    result = []
    for index, character in enumerate(value):
        if character in ',+"\\<>;=':
            result.append("\\" + character)
        elif character == "\x00":
            result.append("\\00")
        elif character == " " and (index == 0 or index == len(value) - 1):
            result.append("\\ ")
        elif character == "#" and index == 0:
            result.append("\\#")
        else:
            result.append(character)
    return "".join(result)


def parse_dn(dn):
    """Return a DN as a tuple of RDNs of ``(type, value)`` pairs."""

    if not isinstance(dn, str):
        raise TypeError("distinguished name must be text")
    if dn == "":
        return ()
    rdns = []
    for raw_rdn in _split_escaped(dn, ","):
        if raw_rdn.strip() == "":
            raise LdifError("empty RDN in distinguished name")
        avas = []
        for raw_ava in _split_escaped(raw_rdn, "+"):
            equals = raw_ava.find("=")
            if equals <= 0:
                raise LdifError("invalid RDN component %r" % raw_ava)
            attribute_type = raw_ava[:equals].strip()
            if not _ATTRIBUTE_TYPE.match(attribute_type):
                raise LdifError("invalid RDN attribute type %r" % attribute_type)
            value = _unescape_value(raw_ava[equals + 1 :].strip())
            avas.append((attribute_type.lower(), value))
        rdns.append(tuple(avas))
    return tuple(rdns)


def normalize_rdn(rdn):
    avas = sorted(
        ((attribute_type, value.casefold()) for attribute_type, value in rdn),
    )
    return "+".join(
        "%s=%s" % (attribute_type, _escape_value(value))
        for attribute_type, value in avas
    )


def normalize_dn(dn):
    """Canonical, case-insensitive address key used for matching only.

    The original DN is always stored alongside this key so that evidence is
    never replaced by the normalised spelling.
    """

    return ",".join(normalize_rdn(rdn) for rdn in parse_dn(dn))


def dn_parent(dn):
    """Return the raw parent portion of a DN, or ``None`` for a top entry."""

    parts = _split_escaped(dn, ",")
    if len(parts) <= 1:
        return None
    return ",".join(parts[1:])


def replace_dn_suffix(dn, old_suffix, new_suffix):
    """Replace the ancestor ``old_suffix`` of ``dn`` with ``new_suffix``.

    Raw RDN spelling of the child portion is preserved.
    """

    parts = _split_escaped(dn, ",")
    old_parts = _split_escaped(old_suffix, ",")
    if len(parts) <= len(old_parts):
        raise LdifError("DN is not a descendant of the replacement suffix")
    child = parts[: len(parts) - len(old_parts)]
    candidate = ",".join(child + old_parts)
    if normalize_dn(candidate) != normalize_dn(dn):
        raise LdifError("DN is not a descendant of the replacement suffix")
    return ",".join(child + [new_suffix])


def join_dn(rdn, parent):
    if parent:
        return rdn + "," + parent
    return rdn
