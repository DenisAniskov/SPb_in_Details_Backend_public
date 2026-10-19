import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import sys

import pytest

from tools.verify_osm_equivalence import EquivalenceError, verify_databases


def make_database(path):
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE dataset_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE zones (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE buildings (id INTEGER PRIMARY KEY, zone_id INTEGER REFERENCES zones(id), geometry_wkb BLOB, height REAL);
            CREATE INDEX buildings_zone_idx ON buildings(zone_id);
            CREATE VIEW visible_buildings AS SELECT id, height FROM buildings;
            CREATE TRIGGER validate_height BEFORE INSERT ON buildings WHEN NEW.height < 0 BEGIN SELECT RAISE(ABORT, 'negative height'); END;
            CREATE VIRTUAL TABLE buildings_rtree USING rtree(id, min_x, max_x, min_y, max_y);
            INSERT INTO dataset_meta VALUES ('dataset_version', 'public-fixture-v1');
            INSERT INTO zones VALUES (1, 'public-zone');
        """)
        geometry = struct.pack("<BIdd", 1, 1, 30.25, 59.75)
        connection.execute("INSERT INTO buildings VALUES (7, 1, ?, 12.5)", (geometry,))
        connection.execute("INSERT INTO buildings_rtree VALUES (7, 30.25, 30.25, 59.75, 59.75)")


@pytest.fixture
def pair(tmp_path):
    original = tmp_path / "original.sqlite"
    rebuilt = tmp_path / "rebuilt.sqlite"
    make_database(original)
    shutil.copyfile(original, rebuilt)
    return original, rebuilt, hashlib.sha256(original.read_bytes()).hexdigest()


def compare(pair):
    original, rebuilt, expected = pair
    return verify_databases(original, rebuilt, expected_original_sha256=expected)


def test_exact_duplicate_covers_rtree_shadow_schema_and_full_rows_readonly(pair):
    original, rebuilt, _ = pair
    before = original.read_bytes(), rebuilt.read_bytes()
    result = compare(pair)
    assert result["status"] == "pass" and result["byte_equal"] and result["logical_equal"]
    assert result["original"]["rtree_checks"] == {"buildings_rtree": "ok"}
    assert {"buildings", "buildings_rtree", "buildings_rtree_node", "buildings_rtree_rowid",
            "buildings_rtree_parent", "dataset_meta", "zones"} <= result["original"]["tables"].keys()
    assert before == (original.read_bytes(), rebuilt.read_bytes())


def test_writer_header_difference_is_explicit_but_exact_logical_match_passes(pair):
    original, rebuilt, _ = pair
    content = bytearray(rebuilt.read_bytes())
    writer_version = struct.unpack(">I", content[96:100])[0]
    content[96:100] = struct.pack(">I", writer_version ^ 1)
    rebuilt.write_bytes(content)
    result = compare(pair)
    assert result["status"] == "pass" and result["logical_equal"] and not result["byte_equal"]
    assert result["byte_differences"]["different_bytes_including_length_difference"] == 1
    assert result["byte_differences"]["ranges"] == [{"start": 99, "end_exclusive": 100, "length": 1}]
    assert result["original"]["logical_sha256"] == result["rebuilt"]["logical_sha256"]
    assert result["original"]["file_sha256"] != result["rebuilt"]["file_sha256"]
    assert result["rebuilt"]["integrity_check"] == "ok"


@pytest.mark.parametrize("mutation,section", [
    ("UPDATE buildings SET geometry_wkb = X'01020304' WHERE id=7", "rows"),
    ("UPDATE buildings SET height=12.500000000000002 WHERE id=7", "rows"),
    ("UPDATE dataset_meta SET value='changed-version'", "rows"),
    ("ALTER TABLE buildings ADD COLUMN new_metadata TEXT", "columns"),
    ("DROP INDEX buildings_zone_idx", "indexes"),
])
def test_any_geometry_float_metadata_or_schema_change_fails(pair, mutation, section):
    with sqlite3.connect(pair[1]) as connection:
        connection.execute(mutation)
    result = compare(pair)
    assert result["status"] == "fail" and not result["logical_equal"]
    assert any(section in sections for sections in result["table_differences"].values())
    assert result["original"]["logical_sha256"] != result["rebuilt"]["logical_sha256"]


@pytest.mark.parametrize("pragma", ["user_version=17", "application_id=42"])
def test_database_pragmas_are_not_ignored(pair, pragma):
    with sqlite3.connect(pair[1]) as connection:
        connection.execute("PRAGMA " + pragma)
    result = compare(pair)
    assert result["status"] == "fail" and "database-pragmas" in result["mismatches"]


def test_rtree_values_and_shadow_storage_are_included(pair):
    with sqlite3.connect(pair[1]) as connection:
        connection.execute("UPDATE buildings_rtree SET min_x=30.125 WHERE id=7")
    result = compare(pair)
    assert result["status"] == "fail"
    assert "rows" in result["table_differences"]["buildings_rtree"]
    assert "rows" in result["table_differences"]["buildings_rtree_node"]
    assert result["rebuilt"]["rtree_checks"] == {"buildings_rtree": "ok"}


def test_original_pin_mismatch_fails_even_if_both_databases_are_equal(pair):
    result = verify_databases(pair[0], pair[1])
    assert result["status"] == "fail" and result["logical_equal"]
    assert result["mismatches"] == ["original-file-sha256"]


def test_live_sidecars_and_corrupt_artifacts_fail_without_values(pair):
    sidecar = Path(str(pair[1]) + "-wal")
    sidecar.write_bytes(b"untrusted-live-pages")
    with pytest.raises(EquivalenceError, match="frozen"):
        compare(pair)
    sidecar.unlink()
    pair[1].write_bytes(b"invalid-file-content")
    with pytest.raises(EquivalenceError, match="SQLite 3 artifact"):
        compare(pair)


def test_cli_writes_diagnostic_and_fails_logical_mismatch_without_dumping_data(pair, tmp_path):
    with sqlite3.connect(pair[1]) as connection:
        connection.execute("UPDATE dataset_meta SET value='private-value-for-redaction-test'")
    output = tmp_path / "report.json"
    command = [sys.executable, "tools/verify_osm_equivalence.py", "--original", str(pair[0]),
               "--rebuilt", str(pair[1]), "--output", str(output), "--expected-original-sha256", pair[2]]
    result = subprocess.run(command, capture_output=True, text=True, cwd=Path(__file__).parents[1])
    assert result.returncode == 1 and json.loads(output.read_text())["logical_equal"] is False
    assert "private-value-for-redaction-test" not in result.stdout + result.stderr + output.read_text()
