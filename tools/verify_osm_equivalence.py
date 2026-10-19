"""Prove exact logical equivalence of two frozen OSM SQLite build artifacts.

Binary differences are reported, never hidden. Every schema object, column/index
descriptor, table row (including Rtree shadow blobs), SQLite storage class and
floating-point value must match exactly. There is no geometric tolerance or
metadata exemption. Both files are opened read-only; live DB sidecars are refused.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import sqlite3
import struct


EXPECTED_ORIGINAL_SHA256 = "d5ca19e0d6601ee64c684c15db982b91919e7c2b84b29454e2e30737ea2613de"


class EquivalenceError(RuntimeError):
    """Safe diagnostic, containing no database values or geometry."""


def _atom(value) -> bytes:
    if value is None:
        return b"N"
    if isinstance(value, int):
        payload = str(value).encode("ascii")
        tag = b"I"
    elif isinstance(value, float):
        payload = struct.pack(">d", value)
        tag = b"F"
    elif isinstance(value, str):
        payload = value.encode("utf-8")
        tag = b"T"
    elif isinstance(value, bytes):
        payload = value
        tag = b"B"
    else:
        raise EquivalenceError("Unsupported SQLite value type")
    return tag + struct.pack(">Q", len(payload)) + payload


def _record(values) -> bytes:
    return struct.pack(">Q", len(values)) + b"".join(_atom(value) for value in values)


def _hash_records(records) -> str:
    digest = hashlib.sha256()
    for record in records:
        digest.update(struct.pack(">Q", len(record)))
        digest.update(record)
    return digest.hexdigest()


def _identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _header(data: bytes) -> dict:
    if len(data) < 100 or data[:16] != b"SQLite format 3\x00":
        raise EquivalenceError("Input is not a SQLite 3 artifact")
    word = lambda offset: struct.unpack(">I", data[offset:offset + 4])[0]
    page_size = struct.unpack(">H", data[16:18])[0]
    return {
        "page_size": 65536 if page_size == 1 else page_size,
        "write_format": data[18], "read_format": data[19], "reserved_bytes": data[20],
        "payload_fractions": list(data[21:24]), "change_counter": word(24),
        "database_pages": word(28), "freelist_first_page": word(32), "freelist_pages": word(36),
        "schema_cookie": word(40), "schema_format": word(44), "default_cache_size": word(48),
        "largest_root_page": word(52), "encoding_code": word(56), "user_version": word(60),
        "incremental_vacuum": word(64), "application_id": word(68),
        "reserved_expansion_sha256": hashlib.sha256(data[72:92]).hexdigest(),
        "version_valid_for": word(92), "writer_sqlite_version_number": word(96),
    }


def _byte_differences(original: bytes, rebuilt: bytes, *, maximum_ranges: int = 128) -> dict:
    ranges = []
    count = range_count = 0
    start = None
    for offset in range(max(len(original), len(rebuilt))):
        differs = offset >= len(original) or offset >= len(rebuilt) or original[offset] != rebuilt[offset]
        if differs:
            count += 1
            if start is None:
                start = offset
        elif start is not None:
            range_count += 1
            if len(ranges) < maximum_ranges:
                ranges.append({"start": start, "end_exclusive": offset, "length": offset - start})
            start = None
    if start is not None:
        end = max(len(original), len(rebuilt))
        range_count += 1
        if len(ranges) < maximum_ranges:
            ranges.append({"start": start, "end_exclusive": end, "length": end - start})
    return {"different_bytes_including_length_difference": count, "range_count": range_count,
            "ranges": ranges, "ranges_truncated": range_count > len(ranges),
            "original_extra_bytes": max(0, len(original) - len(rebuilt)),
            "rebuilt_extra_bytes": max(0, len(rebuilt) - len(original))}


def _snapshot(path: Path) -> tuple[dict, dict]:
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(str(path) + suffix).exists():
            raise EquivalenceError("Input must be a frozen SQLite artifact without WAL/journal sidecars")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise EquivalenceError("SQLite integrity_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise EquivalenceError("SQLite foreign_key_check failed")
        pragmas = {name: connection.execute("PRAGMA " + name).fetchone()[0]
                   for name in ("user_version", "application_id", "encoding")}
        schema = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"
        ).fetchall()
        schema_records = [_record(row) for row in schema]
        table_contents = {}
        details = {}
        rtree_checks = {}
        for kind, name, _table, sql in schema:
            if kind != "table":
                continue
            quoted = _identifier(name)
            columns = connection.execute(f"PRAGMA table_xinfo({quoted})").fetchall()
            indexes = connection.execute(f"PRAGMA index_list({quoted})").fetchall()
            index_columns = {index[1]: connection.execute(
                f"PRAGMA index_xinfo({_identifier(index[1])})").fetchall() for index in indexes}
            # Explicitly select hidden/generated columns too, when a virtual
            # table exposes them. Rtree and all shadow tables remain in scope.
            selection = ", ".join(_identifier(column[1]) for column in columns)
            if not selection:
                raise EquivalenceError("SQLite table has no inspectable columns")
            rows = sorted(_record(row) for row in connection.execute(f"SELECT {selection} FROM {quoted}"))
            column_records = [_record(row) for row in columns]
            index_records = [_record(row) for row in indexes]
            index_column_records = {index: [_record(row) for row in values]
                                    for index, values in index_columns.items()}
            table_contents[name] = {"columns": column_records, "indexes": index_records,
                                    "index_columns": index_column_records, "rows": rows}
            details[name] = {"rows": len(rows), "columns_sha256": _hash_records(column_records),
                             "indexes_sha256": _hash_records(index_records),
                             "index_columns_sha256": {index: _hash_records(values)
                                                      for index, values in index_column_records.items()},
                             "typed_rows_sha256": _hash_records(rows)}
            if sql and re.search(r"\bUSING\s+rtree(?:_i32)?\s*\(", sql, re.I):
                if connection.execute("SELECT rtreecheck(?)", (name,)).fetchall() != [("ok",)]:
                    raise EquivalenceError("SQLite Rtree integrity check failed")
                rtree_checks[name] = "ok"
        payload = [_record(("pragmas", len(pragmas)))]
        payload.extend(_record((key, value)) for key, value in sorted(pragmas.items()))
        payload.append(_record(("schema", len(schema_records))))
        payload.extend(schema_records)
        for name, content in sorted(table_contents.items()):
            payload.append(_record(("table", name, len(content["columns"]), len(content["indexes"]),
                                    len(content["index_columns"]), len(content["rows"]))))
            for section in ("columns", "indexes"):
                payload.append(_record((section, len(content[section]))))
                payload.extend(content[section])
            for index, values in sorted(content["index_columns"].items()):
                payload.append(_record(("index_columns", index, len(values))))
                payload.extend(values)
            payload.append(_record(("rows", len(content["rows"]))))
            payload.extend(content["rows"])
        return ({"pragmas": pragmas, "schema": schema_records, "tables": table_contents},
                {"pragmas": pragmas, "schema_sha256": _hash_records(schema_records), "tables": details,
                 "logical_sha256": _hash_records(payload), "integrity_check": "ok",
                 "foreign_key_check": "ok", "rtree_checks": rtree_checks})
    except sqlite3.Error:
        raise EquivalenceError("SQLite artifact could not be completely inspected") from None
    finally:
        connection.close()


def _runtime() -> dict:
    result = {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
              "shapely": None, "geos": None}
    try:
        import shapely
        result.update(shapely=shapely.__version__, geos=shapely.geos_version_string)
    except (ImportError, AttributeError):
        pass
    return result


def verify_databases(original: Path, rebuilt: Path, *, expected_original_sha256: str = EXPECTED_ORIGINAL_SHA256) -> dict:
    original, rebuilt = original.resolve(), rebuilt.resolve()
    if not original.is_file() or not rebuilt.is_file():
        raise EquivalenceError("Both SQLite artifacts must already exist")
    original_bytes, rebuilt_bytes = original.read_bytes(), rebuilt.read_bytes()
    original_hash, rebuilt_hash = (hashlib.sha256(value).hexdigest() for value in (original_bytes, rebuilt_bytes))
    original_header, rebuilt_header = _header(original_bytes), _header(rebuilt_bytes)
    original_snapshot, original_details = _snapshot(original)
    rebuilt_snapshot, rebuilt_details = _snapshot(rebuilt)
    if original.read_bytes() != original_bytes or rebuilt.read_bytes() != rebuilt_bytes:
        raise EquivalenceError("A SQLite artifact changed during read-only inspection")
    mismatches = []
    if original_hash != expected_original_sha256:
        mismatches.append("original-file-sha256")
    if original_snapshot["pragmas"] != rebuilt_snapshot["pragmas"]:
        mismatches.append("database-pragmas")
    if original_snapshot["schema"] != rebuilt_snapshot["schema"]:
        mismatches.append("exact-schema-sql")
    names = sorted(set(original_snapshot["tables"]) | set(rebuilt_snapshot["tables"]))
    table_differences = {}
    for name in names:
        left, right = original_snapshot["tables"].get(name), rebuilt_snapshot["tables"].get(name)
        if left is None or right is None:
            table_differences[name] = ["table-presence"]
        else:
            sections = [section for section in ("columns", "indexes", "index_columns", "rows")
                        if left[section] != right[section]]
            if sections:
                table_differences[name] = sections
    if table_differences:
        mismatches.append("exact-table-content-or-metadata")
    logical_equal = original_snapshot == rebuilt_snapshot
    return {"status": "pass" if not mismatches else "fail", "byte_equal": original_bytes == rebuilt_bytes,
            "logical_equal": logical_equal, "mismatches": mismatches, "table_differences": table_differences,
            "expected_original_sha256": expected_original_sha256,
            "original": {"file_sha256": original_hash, "bytes": len(original_bytes),
                         "header": original_header, **original_details},
            "rebuilt": {"file_sha256": rebuilt_hash, "bytes": len(rebuilt_bytes),
                        "header": rebuilt_header, **rebuilt_details},
            "byte_differences": _byte_differences(original_bytes, rebuilt_bytes), "runtime": _runtime(),
            "comparison": "Exact typed full rows, IEEE754 floats, BLOB/WKB bytes, schema SQL and metadata; includes Rtree shadow storage. No tolerance or ignored metadata.",
            "cause": "Binary difference cause is not inferred by this verifier."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", required=True, type=Path)
    parser.add_argument("--rebuilt", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-original-sha256", default=EXPECTED_ORIGINAL_SHA256)
    args = parser.parse_args()
    try:
        if args.output.resolve() in {args.original.resolve(), args.rebuilt.resolve()}:
            raise EquivalenceError("Diagnostic output cannot overwrite an input database")
        if not re.fullmatch(r"[0-9a-f]{64}", args.expected_original_sha256):
            raise EquivalenceError("Expected original SHA-256 must be an explicit lowercase digest")
        result = verify_databases(args.original, args.rebuilt,
                                  expected_original_sha256=args.expected_original_sha256)
    except (EquivalenceError, OSError) as error:
        reason = str(error) if isinstance(error, EquivalenceError) else "Artifact input/output access failed"
        result = {"status": "error", "logical_equal": False, "reason": reason}
    try:
        if args.output.resolve() not in {args.original.resolve(), args.rebuilt.resolve()}:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        result = {"status": "error", "reason": "Could not write equivalence diagnostic"}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
