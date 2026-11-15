"""File-level LDIF intake, entity evolution and queryable directory versions.

The catalog sits beside :class:`archive_crawl.archive.Archive`.  Raw capture
stays the evidence; this module adds the business interpretation:

``intake_files``
    One row per received export, tied to the archived capture.  Repeated
    exports are recorded as duplicates and never applied twice.

``intake_records``
    Every LDIF record in file order, with its verbatim text and line span so
    an archivist can return from a version to the received file.

``entities`` / ``entity_versions``
    Stable identity is the supplier's ``entryUUID``.  Every applied change
    appends a version; nothing overwrites an older DN or attribute state.

``dn_bindings``
    Time-scoped address history.  A DN is never rewritten in place, so an
    address later reused by another entity keeps both holders.

``external_references``
    ``attr:< URL`` values are registered as pending evidence and never
    fetched.

Continuity is tracked per directory.  A full export declares
``last_change_sequence``; change files must continue at the next sequence.  A
change received early is stored but held, and :meth:`Catalog.current_catalog`
refuses to publish a directory while a segment is missing or a record could
not be interpreted.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from . import ldif


class CatalogError(ValueError):
    """Malformed intake metadata or a record that cannot be applied."""


class ContinuityError(CatalogError):
    """The directory cannot be published without inventing a baseline."""


class ConflictError(CatalogError):
    """An applied record would reuse an address or identity ambiguously."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS directories (
    directory TEXT PRIMARY KEY,
    baseline_file_id INTEGER,
    baseline_export TEXT,
    baseline_through INTEGER,
    applied_through INTEGER,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS intake_files (
    id INTEGER PRIMARY KEY,
    directory TEXT NOT NULL,
    export TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('snapshot','changes')),
    last_change_sequence INTEGER,
    capture_id INTEGER NOT NULL REFERENCES captures(id),
    digest TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS intake_records (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES intake_files(id),
    position INTEGER NOT NULL,
    line_number INTEGER NOT NULL,
    line_end INTEGER,
    kind TEXT NOT NULL,
    changetype TEXT,
    dn_raw TEXT NOT NULL,
    dn_key TEXT NOT NULL,
    entry_uuid TEXT,
    raw_text TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    UNIQUE (file_id, position)
);

CREATE TABLE IF NOT EXISTS entities (
    directory TEXT NOT NULL,
    uuid TEXT NOT NULL,
    first_file_id INTEGER REFERENCES intake_files(id),
    first_record_id INTEGER REFERENCES intake_records(id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (directory, uuid)
);

CREATE TABLE IF NOT EXISTS entity_versions (
    id INTEGER PRIMARY KEY,
    directory TEXT NOT NULL,
    uuid TEXT NOT NULL,
    version INTEGER NOT NULL,
    dn_raw TEXT NOT NULL,
    dn_key TEXT NOT NULL,
    attributes TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL,
    file_id INTEGER REFERENCES intake_files(id),
    record_id INTEGER REFERENCES intake_records(id),
    applied_sequence INTEGER NOT NULL,
    position INTEGER,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (directory, uuid, version)
);

CREATE TABLE IF NOT EXISTS dn_bindings (
    id INTEGER PRIMARY KEY,
    directory TEXT NOT NULL,
    dn_raw TEXT NOT NULL,
    dn_key TEXT NOT NULL,
    uuid TEXT NOT NULL,
    from_version INTEGER NOT NULL,
    until_version INTEGER,
    from_record_id INTEGER REFERENCES intake_records(id),
    until_record_id INTEGER REFERENCES intake_records(id),
    reason TEXT NOT NULL,
    closed_reason TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS external_references (
    id INTEGER PRIMARY KEY,
    directory TEXT NOT NULL,
    uuid TEXT NOT NULL DEFAULT '',
    attribute TEXT NOT NULL,
    options TEXT NOT NULL,
    reference TEXT NOT NULL,
    first_record_id INTEGER REFERENCES intake_records(id),
    last_record_id INTEGER REFERENCES intake_records(id),
    status TEXT NOT NULL DEFAULT 'pending',
    UNIQUE (directory, uuid, attribute, options, reference)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_binding_per_dn
    ON dn_bindings (directory, dn_key) WHERE active = 1;
CREATE INDEX IF NOT EXISTS bindings_by_uuid
    ON dn_bindings (directory, uuid, active);
CREATE INDEX IF NOT EXISTS versions_by_uuid
    ON entity_versions (directory, uuid, version);
CREATE INDEX IF NOT EXISTS files_by_directory
    ON intake_files (directory, sequence);

CREATE VIEW IF NOT EXISTS pending_relations AS
    SELECT 'external_reference' AS kind, directory, NULL AS file_id,
           NULL AS record_id, uuid AS entity_uuid, attribute AS detail_key,
           reference AS detail, 'pending' AS status
      FROM external_references WHERE status = 'pending'
    UNION ALL
    SELECT 'unresolved_record', f.directory, r.file_id, r.id, r.entry_uuid,
           r.dn_raw,
           coalesce(r.changetype, r.kind) || ': ' || coalesce(r.detail, ''),
           'unresolved'
      FROM intake_records r JOIN intake_files f ON f.id = r.file_id
     WHERE r.status = 'unresolved'
    UNION ALL
    SELECT 'blocked_export', f.directory, f.id, NULL, NULL, f.export,
           f.detail, f.status
      FROM intake_files f WHERE f.status IN ('blocked','baseline_required');
"""


@dataclass(frozen=True)
class IngestResult:
    file_id: int
    status: str
    applied_records: int = 0
    unresolved_records: int = 0
    detail: str | None = None

    def as_dict(self):
        return {
            "file_id": self.file_id,
            "status": self.status,
            "applied_records": self.applied_records,
            "unresolved_records": self.unresolved_records,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class DirectoryStatus:
    directory: str
    baseline_export: str | None
    baseline_through: int | None
    applied_through: int | None
    contiguous: bool
    complete: bool
    missing: tuple[int, ...]
    unresolved_records: int
    buffered: tuple[dict, ...]

    def as_dict(self):
        return {
            "directory": self.directory,
            "baseline_export": self.baseline_export,
            "baseline_through": self.baseline_through,
            "applied_through": self.applied_through,
            "contiguous": self.contiguous,
            "complete": self.complete,
            "missing": list(self.missing),
            "unresolved_records": self.unresolved_records,
            "buffered": list(self.buffered),
        }


# ---------------------------------------------------------------------------
# attribute values
# ---------------------------------------------------------------------------


def _value_bytes(value):
    if value.get("encoding") == "external":
        return None
    data = value.get("data")
    if data is not None:
        return base64.b64decode(data)
    return (value.get("text") or "").encode("utf-8")


def _value_key(value):
    if value.get("encoding") == "external":
        return ("external", value.get("reference"))
    return ("data", base64.b64encode(_value_bytes(value)).decode("ascii"))


def _value_text(value):
    if value.get("encoding") == "external":
        return None
    return value.get("text")


def _attribute_key(attribute):
    return (
        attribute["name"].lower(),
        tuple(sorted(option.lower() for option in attribute["options"])),
    )


def _find_attribute(attributes, name, options):
    key = (name.lower(), tuple(sorted(option.lower() for option in options)))
    for index, attribute in enumerate(attributes):
        if _attribute_key(attribute) == key:
            return index
    return None


def _add_values(attributes, name, options, values):
    index = _find_attribute(attributes, name, options)
    if index is None:
        attributes.append(
            {"name": name, "options": list(options), "values": []}
        )
        index = len(attributes) - 1
    present = {_value_key(value) for value in attributes[index]["values"]}
    for value in values:
        key = _value_key(value)
        if key not in present:
            attributes[index]["values"].append(value)
            present.add(key)


def _delete_values(attributes, name, options, values):
    index = _find_attribute(attributes, name, options)
    if index is None:
        return
    if not values:
        del attributes[index]
        return
    keys = {_value_key(value) for value in values}
    attributes[index]["values"] = [
        value
        for value in attributes[index]["values"]
        if _value_key(value) not in keys
    ]
    if not attributes[index]["values"]:
        del attributes[index]


def _replace_values(attributes, name, options, values):
    index = _find_attribute(attributes, name, options)
    if not values:
        if index is not None:
            del attributes[index]
        return
    attribute = {"name": name, "options": list(options), "values": list(values)}
    if index is None:
        attributes.append(attribute)
    else:
        attributes[index] = attribute


def _apply_modifications(attributes, modifications):
    """Apply modify operations strictly in file order."""

    for modification in modifications:
        name = modification.attribute.name
        options = modification.attribute.options
        values = [value.as_dict() for value in modification.attribute.values]
        if name.lower() == "entryuuid":
            raise CatalogError("entryUUID cannot be modified")
        if modification.operation == "add":
            _add_values(attributes, name, options, values)
        elif modification.operation == "delete":
            index = _find_attribute(attributes, name, options)
            if index is None:
                raise CatalogError(
                    "delete refers to an attribute the entry does not have"
                )
            present = {_value_key(value) for value in attributes[index]["values"]}
            for value in values:
                if _value_key(value) not in present:
                    raise CatalogError(
                        "delete refers to a value the entry does not have"
                    )
            _delete_values(attributes, name, options, values)
        elif modification.operation == "replace":
            _replace_values(attributes, name, options, values)
        else:  # pragma: no cover - the parser rejects other operations
            raise CatalogError("unknown modification %r" % modification.operation)


def _external_references(attributes):
    references = []
    for attribute in attributes:
        for value in attribute["values"]:
            if value.get("encoding") == "external":
                references.append(
                    (
                        attribute["name"],
                        ";".join(attribute["options"]),
                        value["reference"],
                    )
                )
    return references


def _replace_value_text(value, text):
    """Return ``value`` carrying ``text`` while keeping its LDIF encoding."""

    if value.get("encoding") == "external":
        return value
    if value.get("encoding") == "base64":
        return {
            "encoding": "base64",
            "text": text,
            "data": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        }
    return {"encoding": "plain", "text": text}


def _rewrite_dn_references(attributes, renames, eligible=None):
    """Rewrite attribute values that name a renamed entry or its descendants.

    ``renames`` is a sequence of ``(old_dn, new_dn)`` pairs.  Only attributes
    known to hold DNs (``eligible``) are considered, so an unrelated string
    that happens to parse as a DN is never silently rewritten.  The longest
    old suffix wins, keeping the child RDN spelling.  External values are left
    alone: their address is evidence, not a DN.
    """

    changed = False
    ordered = sorted(
        ((ldif.normalize_dn(old), old, new) for old, new in renames),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    for attribute in attributes:
        if eligible is not None and attribute["name"].lower() not in eligible:
            continue
        for index, value in enumerate(attribute["values"]):
            text = _value_text(value)
            if not text:
                continue
            try:
                key = ldif.normalize_dn(text)
            except (ldif.LdifError, TypeError, ValueError):
                continue
            for old_key, old_dn, new_dn in ordered:
                if key != old_key and not key.endswith("," + old_key):
                    continue
                replacement = (
                    new_dn
                    if key == old_key
                    else ldif.replace_dn_suffix(text, old_dn, new_dn)
                )
                attribute["values"][index] = _replace_value_text(value, replacement)
                changed = True
                break
    return changed


DEFAULT_DN_REFERENCE_ATTRIBUTES = (
    "member",
    "uniquemember",
    "owner",
    "seealso",
    "manager",
    "secretary",
    "roleoccupant",
    "aliasedobjectname",
    "distinguishedname",
    "entrydn",
)


def _like_escape(text):
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---------------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------------


class Catalog:
    def __init__(self, archive, dn_reference_attributes=None):
        self.archive = archive
        self.connection = archive.connection
        self.dn_reference_attributes = frozenset(
            name.lower()
            for name in (
                dn_reference_attributes
                if dn_reference_attributes is not None
                else DEFAULT_DN_REFERENCE_ATTRIBUTES
            )
        )
        self.connection.executescript(SCHEMA)

    # -- intake ------------------------------------------------------------

    def ingest(self, capture_id, *, directory, export, sequence, kind,
               last_change_sequence=None):
        """Interpret one archived capture as an LDIF export.

        The file is always registered, even when it cannot be applied yet.
        """

        directory = _require_text(directory, "directory")
        export = _require_text(export, "export")
        sequence = _require_sequence(sequence, "sequence")
        if kind not in ("snapshot", "changes"):
            raise CatalogError("kind must be 'snapshot' or 'changes'")
        if kind == "snapshot":
            last_change_sequence = _require_sequence(
                last_change_sequence, "last_change_sequence"
            )
        elif last_change_sequence is not None:
            raise CatalogError(
                "changes export must not declare last_change_sequence"
            )

        try:
            body = self.archive.body(capture_id)
        except KeyError as error:
            raise CatalogError("unknown capture %s" % capture_id) from error
        capture = self.connection.execute(
            "SELECT digest FROM captures WHERE id=?", (capture_id,)
        ).fetchone()
        digest = capture["digest"]

        try:
            parsed = ldif.parse_ldif(body)
        except ldif.LdifError as error:
            with self.archive.transaction():
                file_id = self._insert_file(
                    directory, export, sequence, kind, last_change_sequence,
                    capture_id, digest, "invalid", str(error),
                )
            return IngestResult(file_id, "invalid", detail=str(error))

        with self.archive.transaction():
            duplicate = self._duplicate_of(
                directory, export, sequence, kind, digest
            )
            if duplicate is not None:
                file_id = self._insert_file(
                    directory, export, sequence, kind, last_change_sequence,
                    capture_id, digest, duplicate[0], duplicate[1],
                )
                return IngestResult(file_id, duplicate[0], detail=duplicate[1])

            file_id = self._insert_file(
                directory, export, sequence, kind, last_change_sequence,
                capture_id, digest, "pending", None,
            )
            for position, record in enumerate(parsed):
                self._insert_record(file_id, position, record)

            if kind == "snapshot":
                status, detail = self._ingest_snapshot(
                    file_id, directory, through=last_change_sequence
                )
            else:
                status, detail = self._ingest_changes(file_id, directory, sequence)
            self._update_file(file_id, status, detail)
            self._drain(directory)
            applied, unresolved = self._count_records(file_id)
            return IngestResult(
                file_id, self._file(file_id)["status"], applied, unresolved,
                self._file(file_id)["detail"],
            )

    def _insert_file(self, directory, export, sequence, kind,
                     last_change_sequence, capture_id, digest, status, detail):
        cursor = self.connection.execute(
            """INSERT INTO intake_files
                   (directory,export,sequence,kind,last_change_sequence,
                    capture_id,digest,status,detail)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (directory, export, sequence, kind, last_change_sequence,
             capture_id, digest, status, detail),
        )
        return cursor.lastrowid

    def _insert_record(self, file_id, position, record):
        self.connection.execute(
            """INSERT INTO intake_records
                   (file_id,position,line_number,line_end,kind,changetype,
                    dn_raw,dn_key,entry_uuid,raw_text,status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                file_id, position, record.line_number, record.line_end,
                record.kind, record.changetype, record.dn,
                ldif.normalize_dn(record.dn), record.entry_uuid,
                record.raw_text, "pending",
            ),
        )

    def _duplicate_of(self, directory, export, sequence, kind, digest):
        same_export = self.connection.execute(
            "SELECT * FROM intake_files WHERE directory=? AND export=?",
            (directory, export),
        ).fetchone()
        if same_export is not None:
            if same_export["digest"] == digest:
                return ("duplicate", "same export and content received again")
            return ("conflict", "export id re-used with different content")

        same_content = self.connection.execute(
            "SELECT * FROM intake_files WHERE directory=? AND digest=? AND kind=? "
            "ORDER BY id LIMIT 1",
            (directory, digest, kind),
        ).fetchone()
        if same_content is not None:
            if same_content["sequence"] == sequence:
                return ("duplicate", "identical content for the same sequence")
            return ("conflict", "identical content under a different sequence")

        if kind == "changes":
            # Snapshot sequence is the delivery batch, not the change cursor;
            # several full exports legitimately share it.  Change sequences,
            # in contrast, identify one segment of the stream.
            same_sequence = self.connection.execute(
                "SELECT 1 FROM intake_files WHERE directory=? AND sequence=? "
                "AND kind='changes' "
                "AND status NOT IN ('duplicate','conflict','invalid','superseded')",
                (directory, sequence),
            ).fetchone()
            if same_sequence is not None:
                return ("conflict", "sequence already supplied by another export")
        return None

    def _update_file(self, file_id, status, detail):
        self.connection.execute(
            "UPDATE intake_files SET status=?, detail=? WHERE id=?",
            (status, detail, file_id),
        )

    def _file(self, file_id):
        row = self.connection.execute(
            "SELECT * FROM intake_files WHERE id=?", (file_id,)
        ).fetchone()
        if row is None:
            raise CatalogError("unknown intake file %s" % file_id)
        return row

    def _count_records(self, file_id):
        row = self.connection.execute(
            "SELECT sum(status='applied') AS applied, "
            "sum(status='unresolved') AS unresolved "
            "FROM intake_records WHERE file_id=?",
            (file_id,),
        ).fetchone()
        return int(row["applied"] or 0), int(row["unresolved"] or 0)

    # -- directory continuity ---------------------------------------------

    def _directory(self, directory):
        return self.connection.execute(
            "SELECT * FROM directories WHERE directory=?", (directory,)
        ).fetchone()

    def _ensure_directory(self, directory):
        self.connection.execute(
            "INSERT OR IGNORE INTO directories(directory) VALUES (?)", (directory,)
        )

    def _ingest_snapshot(self, file_id, directory, through):
        self._ensure_directory(directory)
        current = self._directory(directory)
        if (
            current["applied_through"] is not None
            and through < current["applied_through"]
        ):
            # Applying this baseline would erase changes already in force and
            # publish an older state as current.
            self._set_records_status(file_id, "superseded")
            return "superseded", "older than changes already applied"
        if (
            current["baseline_through"] is not None
            and through < current["baseline_through"]
        ):
            self._set_records_status(file_id, "superseded")
            return "superseded", "older than the current baseline"
        if (
            current["baseline_through"] is not None
            and through == current["baseline_through"]
            and current["baseline_file_id"] != file_id
        ):
            self._set_records_status(file_id, "conflict")
            return "conflict", "a different baseline already covers this boundary"

        self._apply_snapshot(file_id, directory, through)
        self.connection.execute(
            """UPDATE directories
                  SET baseline_file_id=?, baseline_export=?, baseline_through=?,
                      applied_through=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE directory=?""",
            (file_id, self._file(file_id)["export"], through, through, directory),
        )
        # Buffered changes the new baseline already covers are obsolete.
        for row in self.connection.execute(
            "SELECT id FROM intake_files WHERE directory=? AND kind='changes' "
            "AND status IN ('blocked','baseline_required') AND sequence<=?",
            (directory, through),
        ).fetchall():
            self._update_file(row["id"], "superseded", "covered by a newer full export")
            self._set_records_status(row["id"], "superseded")
        self._drain(directory)
        return "applied", None

    def _ingest_changes(self, file_id, directory, sequence):
        current = self._directory(directory)
        if current is None or current["baseline_through"] is None:
            self._set_records_status(file_id, "blocked")
            return "baseline_required", "no full export has been applied yet"

        applied_through = current["applied_through"]
        if sequence <= applied_through:
            self._set_records_status(file_id, "superseded")
            return "superseded", "sequence already covered"

        if sequence != applied_through + 1:
            self._set_records_status(file_id, "blocked")
            return "blocked", "waiting for sequence %s" % (applied_through + 1)

        self._apply_change_file(file_id, directory, sequence)
        self.connection.execute(
            "UPDATE directories SET applied_through=?, "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE directory=?",
            (sequence, directory),
        )
        return "applied", None

    def _drain(self, directory):
        """Apply buffered change files that now continue the stream."""

        while True:
            current = self._directory(directory)
            if current is None or current["applied_through"] is None:
                return
            next_sequence = current["applied_through"] + 1
            row = self.connection.execute(
                "SELECT * FROM intake_files WHERE directory=? AND kind='changes' "
                "AND status IN ('blocked','baseline_required') AND sequence=? "
                "ORDER BY id LIMIT 1",
                (directory, next_sequence),
            ).fetchone()
            if row is None:
                return
            self._apply_change_file(row["id"], directory, next_sequence)
            self._update_file(row["id"], "applied", None)
            self.connection.execute(
                "UPDATE directories SET applied_through=?, "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE directory=?",
                (next_sequence, directory),
            )

    def _set_records_status(self, file_id, status):
        self.connection.execute(
            "UPDATE intake_records SET status=? WHERE file_id=?", (status, file_id)
        )

    # -- snapshot ----------------------------------------------------------

    def _apply_snapshot(self, file_id, directory, through):
        rows = self.connection.execute(
            "SELECT * FROM intake_records WHERE file_id=? ORDER BY position",
            (file_id,),
        ).fetchall()
        snapshot_uuids = {row["entry_uuid"] for row in rows if row["entry_uuid"]}
        previous_bindings = self.connection.execute(
            "SELECT * FROM dn_bindings WHERE directory=? AND active=1 ORDER BY id",
            (directory,),
        ).fetchall()

        seen_uuids = set()
        seen_dns = set()
        for row in rows:
            try:
                record = self._record(row)
            except (ldif.LdifError, CatalogError) as error:
                self._resolve_record(row["id"], "unresolved", str(error))
                continue
            uuid = record.entry_uuid
            if row["kind"] != "entry":
                self._resolve_record(
                    row["id"], "unresolved",
                    "change record inside a full export",
                )
                self._register_orphan_references(directory, row["id"], record)
                continue
            if uuid is None:
                self._resolve_record(
                    row["id"], "unresolved", "entryUUID missing or unusable"
                )
                self._register_orphan_references(directory, row["id"], record)
                continue
            if uuid in seen_uuids:
                self._resolve_record(
                    row["id"], "unresolved",
                    "duplicate entryUUID within the export",
                )
                continue
            if row["dn_key"] in seen_dns:
                self._resolve_record(
                    row["id"], "unresolved",
                    "duplicate address within the export",
                )
                continue
            seen_uuids.add(uuid)
            seen_dns.add(row["dn_key"])
            self._attempt(
                row,
                lambda: self._snapshot_entry(
                    row, record, uuid, file_id, directory, through
                ),
                orphan_record=record,
                directory=directory,
            )

        # Entries active before this baseline but absent from it are closed;
        # their history stays queryable.
        for binding in previous_bindings:
            if binding["uuid"] in snapshot_uuids:
                continue
            latest = self._latest_version(directory, binding["uuid"])
            if latest is None or latest["deleted"]:
                self._release_binding(
                    binding, latest["version"] if latest else None,
                    None, "absent_from_baseline",
                )
                continue
            version = self._write_version(
                directory, binding["uuid"], latest["dn_raw"],
                json.loads(latest["attributes"]),
                file_id=file_id, record_id=None, sequence=through,
                position=None, reason="absent_from_baseline", deleted=True,
            )
            self._release_binding(binding, version, None,
                                  "absent_from_baseline")

    def _snapshot_entry(self, row, record, uuid, file_id, directory, through):
        holder = self.connection.execute(
            "SELECT * FROM dn_bindings WHERE directory=? AND dn_key=? AND active=1",
            (directory, row["dn_key"]),
        ).fetchone()
        if holder is not None and holder["uuid"] != uuid:
            latest = self._latest_version(directory, holder["uuid"])
            self._release_binding(
                holder, latest["version"] if latest else None, row["id"],
                "dn_reused_by_baseline",
            )

        self._ensure_entity(directory, uuid, file_id, row["id"])
        version = self._write_version(
            directory, uuid, record.dn, self._record_attributes(record),
            file_id=file_id, record_id=row["id"], sequence=through,
            position=row["position"], reason="snapshot",
        )
        self._set_binding(directory, uuid, record.dn, version, row["id"], "snapshot")

    # -- change files ------------------------------------------------------

    def _apply_change_file(self, file_id, directory, sequence):
        rows = self.connection.execute(
            "SELECT * FROM intake_records WHERE file_id=? ORDER BY position",
            (file_id,),
        ).fetchall()
        handlers = {
            "add": self._change_add,
            "delete": self._change_delete,
            "modify": self._change_modify,
            "modrdn": self._change_modrdn,
        }
        for row in rows:
            try:
                record = self._record(row)
            except (ldif.LdifError, CatalogError) as error:
                self._resolve_record(row["id"], "unresolved", str(error))
                continue
            if record.kind != "change":
                self._resolve_record(
                    row["id"], "unresolved",
                    "entry record inside a change export",
                )
                self._register_orphan_references(directory, row["id"], record)
                continue
            self._attempt(
                row,
                lambda: handlers[record.changetype](
                    row, record, file_id, directory, sequence
                ),
                orphan_record=record,
                directory=directory,
            )

    def _attempt(self, row, action, *, orphan_record=None, directory=None):
        """Run one record's interpretation atomically.

        A failure rolls back only that record's writes, so a corrupt or
        conflicting record cannot leave a half-applied version behind.
        """

        self.connection.execute("SAVEPOINT intake_record")
        try:
            action()
            self.connection.execute("RELEASE SAVEPOINT intake_record")
            self._resolve_record(row["id"], "applied", None)
        except (ConflictError, CatalogError, ldif.LdifError, ValueError) as error:
            self.connection.execute(
                "ROLLBACK TO SAVEPOINT intake_record"
            )
            self.connection.execute("RELEASE SAVEPOINT intake_record")
            self._resolve_record(row["id"], "unresolved", str(error))
            if orphan_record is not None:
                self._register_orphan_references(
                    directory, row["id"], orphan_record
                )

    def _change_add(self, row, record, file_id, directory, sequence):
        uuid = record.entry_uuid
        if uuid is None:
            raise CatalogError("entryUUID missing or unusable")
        latest = self._latest_version(directory, uuid)
        if latest is not None and not latest["deleted"]:
            raise ConflictError("entryUUID is already active")
        self._ensure_entity(directory, uuid, file_id, row["id"])
        version = self._write_version(
            directory, uuid, record.dn, self._record_attributes(record),
            file_id=file_id, record_id=row["id"], sequence=sequence,
            position=row["position"], reason="add",
        )
        self._set_binding(directory, uuid, record.dn, version, row["id"], "add")

    def _change_delete(self, row, record, file_id, directory, sequence):
        binding = self._active_binding(directory, record.dn)
        if binding is None:
            raise CatalogError("no active entry at this address")
        if self._active_descendants(directory, binding["dn_key"]):
            raise ConflictError("entry still has active descendants")
        uuid = binding["uuid"]
        latest = self._latest_version(directory, uuid)
        version = self._write_version(
            directory, uuid, latest["dn_raw"], json.loads(latest["attributes"]),
            file_id=file_id, record_id=row["id"], sequence=sequence,
            position=row["position"], reason="delete", deleted=True,
        )
        self._release_binding(binding, version, row["id"], "delete")
        self._withdraw_references(directory, uuid, row["id"])

    def _change_modify(self, row, record, file_id, directory, sequence):
        binding = self._active_binding(directory, record.dn)
        if binding is None:
            raise CatalogError("no active entry at this address")
        uuid = binding["uuid"]
        latest = self._latest_version(directory, uuid)
        attributes = json.loads(latest["attributes"])
        before = json.dumps(attributes, sort_keys=True)
        _apply_modifications(attributes, record.modifications)
        if json.dumps(attributes, sort_keys=True) == before:
            return
        self._write_version(
            directory, uuid, latest["dn_raw"], attributes,
            file_id=file_id, record_id=row["id"], sequence=sequence,
            position=row["position"], reason="modify",
        )

    def _change_modrdn(self, row, record, file_id, directory, sequence):
        binding = self._active_binding(directory, record.dn)
        if binding is None:
            raise CatalogError(
                "rename source address is not active; predecessor missing"
            )
        uuid = binding["uuid"]
        old_dn = binding["dn_raw"]
        old_key = binding["dn_key"]

        try:
            new_rdn = ldif.parse_dn(record.new_rdn)
        except ldif.LdifError as error:
            raise CatalogError("invalid newrdn: %s" % error) from error
        if len(new_rdn) != 1 or not new_rdn[0]:
            raise CatalogError("newrdn is not a single RDN")

        parent = record.new_superior
        if parent is None:
            parent = ldif.dn_parent(old_dn)
        if parent:
            try:
                ldif.parse_dn(parent)
            except ldif.LdifError as error:
                raise CatalogError("invalid newsuperior: %s" % error) from error
        new_dn = ldif.join_dn(record.new_rdn, parent) if parent else record.new_rdn
        new_key = ldif.normalize_dn(new_dn)
        if new_key == old_key or new_key.endswith("," + old_key):
            raise ConflictError("an entry cannot be moved beneath itself")

        holder = self.connection.execute(
            "SELECT * FROM dn_bindings WHERE directory=? AND dn_key=? AND active=1",
            (directory, new_key),
        ).fetchone()
        if holder is not None and holder["uuid"] != uuid:
            raise ConflictError("new address is already active for another entity")

        descendants = self._active_descendants(directory, old_key)
        mapping = {old_key: new_dn}
        moves = [(uuid, new_dn, "modrdn")]
        for descendant in descendants:
            child_dn = ldif.replace_dn_suffix(
                descendant["dn_raw"], old_dn, new_dn
            )
            mapping[descendant["dn_key"]] = child_dn
            moves.append((descendant["uuid"], child_dn, "ancestor_rename"))
        # A rename of an ancestor also rewrites references to entries below
        # the renamed address; longest-suffix matching handles both levels.
        renames = [(old_dn, new_dn)] + [
            (descendant["dn_raw"], mapping[descendant["dn_key"]])
            for descendant in descendants
        ]

        pending = []  # (uuid, dn_raw, attributes, reason, move_binding)
        for moved_uuid, moved_dn, reason in moves:
            latest = self._latest_version(directory, moved_uuid)
            attributes = json.loads(latest["attributes"])
            if moved_uuid == uuid:
                if record.delete_old_rdn:
                    for ava_type, ava_value in ldif.parse_dn(old_dn)[0]:
                        _delete_values(
                            attributes, ava_type, (),
                            [{"encoding": "plain", "text": ava_value}],
                        )
                for ava_type, ava_value in new_rdn[0]:
                    _add_values(
                        attributes, ava_type, (),
                        [{"encoding": "plain", "text": ava_value}],
                    )
            _rewrite_dn_references(attributes, renames, self.dn_reference_attributes)
            pending.append((moved_uuid, moved_dn, attributes, reason, True))

        # References held elsewhere follow the renamed addresses.  The value
        # string changes; the containing entry keeps its address.
        moved_uuids = {item[0] for item in pending}
        for other in self.connection.execute(
            "SELECT * FROM dn_bindings WHERE directory=? AND active=1 ORDER BY id",
            (directory,),
        ).fetchall():
            if other["uuid"] in moved_uuids:
                continue
            latest = self._latest_version(directory, other["uuid"])
            if latest is None or latest["deleted"]:
                continue
            attributes = json.loads(latest["attributes"])
            if _rewrite_dn_references(attributes, renames, self.dn_reference_attributes):
                pending.append(
                    (other["uuid"], latest["dn_raw"], attributes,
                     "reference_update", False)
                )

        # Free the old addresses first so a rename may also move into an
        # address that another moved entry is vacating in the same record.
        for moved_uuid, _, reason in moves:
            latest = self._latest_version(directory, moved_uuid)
            self._release_binding(
                self.connection.execute(
                    "SELECT * FROM dn_bindings WHERE directory=? AND uuid=? AND active=1",
                    (directory, moved_uuid),
                ).fetchone(),
                latest["version"], row["id"], reason,
            )

        for pending_uuid, pending_dn, attributes, reason, move_binding in pending:
            version = self._write_version(
                directory, pending_uuid, pending_dn, attributes,
                file_id=file_id, record_id=row["id"], sequence=sequence,
                position=row["position"], reason=reason,
            )
            if move_binding:
                self._set_binding(
                    directory, pending_uuid, pending_dn, version, row["id"], reason
                )

    # -- record / version primitives --------------------------------------

    def _record(self, row):
        """Re-parse the stored raw block into the logical record."""

        records = ldif.parse_ldif(row["raw_text"])
        if len(records) != 1:
            raise CatalogError("stored record does not re-parse to one record")
        return records[0]

    def _record_attributes(self, record):
        return [
            {
                "name": attribute.name,
                "options": list(attribute.options),
                "values": [value.as_dict() for value in attribute.values],
            }
            for attribute in record.attributes
        ]

    def _latest_version(self, directory, uuid):
        return self.connection.execute(
            "SELECT * FROM entity_versions WHERE directory=? AND uuid=? "
            "ORDER BY version DESC LIMIT 1",
            (directory, uuid),
        ).fetchone()

    def _ensure_entity(self, directory, uuid, file_id, record_id):
        self.connection.execute(
            "INSERT OR IGNORE INTO entities(directory,uuid,first_file_id,first_record_id) "
            "VALUES (?,?,?,?)",
            (directory, uuid, file_id, record_id),
        )

    def _write_version(self, directory, uuid, dn_raw, attributes, *, file_id,
                       record_id, sequence, position, reason, deleted=False):
        latest = self._latest_version(directory, uuid)
        version = 1 if latest is None else latest["version"] + 1
        self.connection.execute(
            """INSERT INTO entity_versions
                   (directory,uuid,version,dn_raw,dn_key,attributes,deleted,reason,
                    file_id,record_id,applied_sequence,position)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                directory, uuid, version, dn_raw, ldif.normalize_dn(dn_raw),
                json.dumps(attributes, ensure_ascii=False, sort_keys=True),
                1 if deleted else 0, reason, file_id, record_id,
                sequence, position,
            ),
        )
        if deleted:
            self._withdraw_references(directory, uuid, record_id)
        else:
            self._sync_external_references(directory, uuid, attributes, record_id)
        return version

    def _active_binding(self, directory, dn):
        return self.connection.execute(
            "SELECT * FROM dn_bindings WHERE directory=? AND dn_key=? AND active=1",
            (directory, ldif.normalize_dn(dn)),
        ).fetchone()

    def _active_descendants(self, directory, dn_key):
        pattern = "%," + _like_escape(dn_key)
        return self.connection.execute(
            "SELECT * FROM dn_bindings WHERE directory=? AND active=1 "
            "AND dn_key LIKE ? ESCAPE '\\' ORDER BY id",
            (directory, pattern),
        ).fetchall()

    def _set_binding(self, directory, uuid, dn_raw, version, record_id, reason):
        dn_key = ldif.normalize_dn(dn_raw)
        existing = self.connection.execute(
            "SELECT * FROM dn_bindings WHERE directory=? AND uuid=? AND active=1",
            (directory, uuid),
        ).fetchone()
        if existing is not None and existing["dn_key"] == dn_key:
            return
        holder = self.connection.execute(
            "SELECT * FROM dn_bindings WHERE directory=? AND dn_key=? AND active=1",
            (directory, dn_key),
        ).fetchone()
        if holder is not None and holder["uuid"] != uuid:
            raise ConflictError("address is already active for another entity")
        if existing is not None:
            self._release_binding(existing, version - 1, record_id, reason)
        self.connection.execute(
            """INSERT INTO dn_bindings
                   (directory,dn_raw,dn_key,uuid,from_version,from_record_id,reason,active)
               VALUES (?,?,?,?,?,?,?,1)""",
            (directory, dn_raw, dn_key, uuid, version, record_id, reason),
        )

    def _release_binding(self, binding, until_version, record_id, reason=None):
        if binding is None:
            return
        self.connection.execute(
            "UPDATE dn_bindings SET active=0, until_version=?, until_record_id=?, "
            "closed_reason=? WHERE id=? AND active=1",
            (until_version, record_id, reason, binding["id"]),
        )

    def _sync_external_references(self, directory, uuid, attributes, record_id):
        current = {
            (name, options, reference)
            for name, options, reference in _external_references(attributes)
        }
        for name, options, reference in current:
            self.connection.execute(
                """INSERT INTO external_references
                       (directory,uuid,attribute,options,reference,
                        first_record_id,last_record_id,status)
                   VALUES (?,?,?,?,?,?,?,'pending')
                   ON CONFLICT(directory,uuid,attribute,options,reference)
                   DO UPDATE SET last_record_id=excluded.last_record_id,
                                 status='pending'""",
                (directory, uuid, name, options, reference, record_id, record_id),
            )
        for row in self.connection.execute(
            "SELECT * FROM external_references WHERE directory=? AND uuid=? "
            "AND status='pending'",
            (directory, uuid),
        ).fetchall():
            if (row["attribute"], row["options"], row["reference"]) not in current:
                self.connection.execute(
                    "UPDATE external_references SET status='withdrawn', "
                    "last_record_id=? WHERE id=?",
                    (record_id, row["id"]),
                )

    def _withdraw_references(self, directory, uuid, record_id):
        self.connection.execute(
            "UPDATE external_references SET status='withdrawn', last_record_id=? "
            "WHERE directory=? AND uuid=? AND status='pending'",
            (record_id, directory, uuid),
        )

    def _register_orphan_references(self, directory, record_id, record):
        for attribute in record.attributes:
            for value in attribute.values:
                if not value.is_external:
                    continue
                self.connection.execute(
                    """INSERT OR IGNORE INTO external_references
                           (directory,uuid,attribute,options,reference,
                            first_record_id,last_record_id,status)
                       VALUES (?,'',?,?,?,?,?,'pending')""",
                    (
                        directory, attribute.name, ";".join(attribute.options),
                        value.reference, record_id, record_id,
                    ),
                )

    def _resolve_record(self, record_id, status, detail):
        self.connection.execute(
            "UPDATE intake_records SET status=?, detail=? WHERE id=?",
            (status, detail, record_id),
        )

    # -- queries -----------------------------------------------------------

    def status(self, directory):
        row = self._directory(directory)
        buffered = tuple(
            dict(item)
            for item in self.connection.execute(
                "SELECT id, export, sequence, kind, detail FROM intake_files "
                "WHERE directory=? AND status IN ('blocked','baseline_required') "
                "ORDER BY sequence, id",
                (directory,),
            ).fetchall()
        )
        if row is None:
            return DirectoryStatus(directory, None, None, None, False, False,
                                   (), 0, buffered)
        applied_through = row["applied_through"]
        missing = ()
        if row["baseline_through"] is not None and buffered:
            ahead = sorted(
                item["sequence"]
                for item in buffered
                if item["sequence"] > applied_through
            )
            if ahead:
                present = set(ahead)
                missing = tuple(
                    number
                    for number in range(applied_through + 1, ahead[-1] + 1)
                    if number not in present
                )
        unresolved = self.connection.execute(
            "SELECT count(*) FROM intake_records r "
            "JOIN intake_files f ON f.id=r.file_id "
            "WHERE f.directory=? AND r.status='unresolved'",
            (directory,),
        ).fetchone()[0]
        contiguous = (
            row["baseline_through"] is not None
            and not any(item["sequence"] > applied_through for item in buffered)
        )
        complete = contiguous and unresolved == 0
        return DirectoryStatus(
            directory, row["baseline_export"], row["baseline_through"],
            applied_through, contiguous, complete, missing, int(unresolved),
            buffered,
        )

    def current_catalog(self, directory, require_complete=True):
        """Return the current entity states for a directory.

        By default a missing change segment, or a record that could not be
        interpreted, raises :class:`ContinuityError` instead of publishing a
        directory silently built on a broken baseline.
        """

        state = self.status(directory)
        if require_complete:
            if state.baseline_through is None:
                raise ContinuityError(
                    "directory %r has no full export baseline" % directory
                )
            if not state.contiguous:
                raise ContinuityError(
                    "directory %r is missing change sequence(s) %s"
                    % (directory, ", ".join(str(number) for number in state.missing))
                )
            if state.unresolved_records:
                raise ContinuityError(
                    "directory %r has %s unresolved record(s)"
                    % (directory, state.unresolved_records)
                )
        entities = []
        for entity in self.connection.execute(
            "SELECT uuid FROM entities WHERE directory=? ORDER BY uuid",
            (directory,),
        ).fetchall():
            latest = self._latest_version(directory, entity["uuid"])
            if latest is None or latest["deleted"]:
                continue
            entities.append(self._version_dict(latest))
        return {
            "directory": directory,
            "status": state.as_dict(),
            "entities": entities,
        }

    def entity(self, directory, uuid):
        if self.connection.execute(
            "SELECT 1 FROM entities WHERE directory=? AND uuid=?", (directory, uuid)
        ).fetchone() is None:
            raise KeyError(uuid)
        versions = [
            self._version_dict(row, include_source=True)
            for row in self.connection.execute(
                "SELECT * FROM entity_versions WHERE directory=? AND uuid=? "
                "ORDER BY version",
                (directory, uuid),
            ).fetchall()
        ]
        bindings = [
            self._binding_dict(row)
            for row in self.connection.execute(
                "SELECT * FROM dn_bindings WHERE directory=? AND uuid=? ORDER BY id",
                (directory, uuid),
            ).fetchall()
        ]
        references = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM external_references WHERE directory=? AND uuid=? "
                "ORDER BY id",
                (directory, uuid),
            ).fetchall()
        ]
        return {
            "directory": directory,
            "uuid": uuid,
            "versions": versions,
            "dn_bindings": bindings,
            "external_references": references,
        }

    def dn_history(self, directory, dn):
        key = ldif.normalize_dn(dn)
        return [
            self._binding_dict(row)
            for row in self.connection.execute(
                "SELECT * FROM dn_bindings WHERE directory=? AND dn_key=? ORDER BY id",
                (directory, key),
            ).fetchall()
        ]

    def as_of(self, directory, sequence):
        """Return each entity's latest state at or before ``sequence``."""

        result = []
        for entity in self.connection.execute(
            "SELECT uuid FROM entities WHERE directory=? ORDER BY uuid",
            (directory,),
        ).fetchall():
            row = self.connection.execute(
                "SELECT * FROM entity_versions WHERE directory=? AND uuid=? "
                "AND applied_sequence<=? ORDER BY version DESC LIMIT 1",
                (directory, entity["uuid"], sequence),
            ).fetchone()
            if row is None or row["deleted"]:
                continue
            result.append(self._version_dict(row))
        return result

    def pending(self, directory=None):
        query = "SELECT * FROM pending_relations"
        params = ()
        if directory is not None:
            query += " WHERE directory=?"
            params = (directory,)
        return [
            dict(row)
            for row in self.connection.execute(
                query + " ORDER BY kind, file_id, record_id", params
            ).fetchall()
        ]

    def record_source(self, record_id):
        """Trace one record back to its file and archived capture."""

        row = self.connection.execute(
            """SELECT r.*, f.directory, f.export, f.sequence AS file_sequence,
                      f.kind AS file_kind, f.capture_id, c.url,
                      c.status AS http_status, c.received_at
                 FROM intake_records r
                 JOIN intake_files f ON f.id=r.file_id
                 JOIN captures c ON c.id=f.capture_id
                WHERE r.id=?""",
            (record_id,),
        ).fetchone()
        if row is None:
            raise KeyError(record_id)
        return dict(row)

    def _version_dict(self, row, include_source=False):
        result = {
            "uuid": row["uuid"],
            "version": row["version"],
            "dn": row["dn_raw"],
            "dn_key": row["dn_key"],
            "attributes": json.loads(row["attributes"]),
            "deleted": bool(row["deleted"]),
            "reason": row["reason"],
            "applied_sequence": row["applied_sequence"],
            "record_id": row["record_id"],
        }
        if include_source:
            result["source"] = (
                self.record_source(row["record_id"])
                if row["record_id"] is not None
                else None
            )
        return result

    def _binding_dict(self, row):
        return {
            "dn": row["dn_raw"],
            "dn_key": row["dn_key"],
            "uuid": row["uuid"],
            "from_version": row["from_version"],
            "until_version": row["until_version"],
            "from_record_id": row["from_record_id"],
            "until_record_id": row["until_record_id"],
            "active": bool(row["active"]),
            "reason": row["reason"],
            "closed_reason": row["closed_reason"],
        }


def _require_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise CatalogError("%s must be non-empty text" % name)
    return value.strip()


def _require_sequence(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CatalogError("%s must be a non-negative integer" % name)
    return value
