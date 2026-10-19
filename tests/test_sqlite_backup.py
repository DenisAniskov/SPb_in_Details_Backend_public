from pathlib import Path
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from scripts.sqlite_backup import BackupError, backup_database, database_path, validate_restore


def make_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.executescript("""
        CREATE TABLE places (id INTEGER PRIMARY KEY, catalog_key TEXT UNIQUE, payload BLOB);
        CREATE TABLE upstream_quota_ledger (id INTEGER PRIMARY KEY, product TEXT NOT NULL, reserved_at REAL NOT NULL);
        CREATE TABLE related (place_id INTEGER REFERENCES places(id), note TEXT);
    """)
    connection.execute("INSERT INTO places VALUES (77, 'stable-catalog-id', ?)", (b"catalog",))
    connection.execute("INSERT INTO upstream_quota_ledger VALUES (8, 'suggest', 1780000000)")
    connection.commit()
    return connection


def url(path: Path) -> str:
    return "sqlite+aiosqlite:///" + str(path)


def test_backup_includes_committed_wal_and_excludes_uncommitted_ledger(tmp_path):
    source_path = tmp_path / "main.db"
    connection = make_database(source_path)
    try:
        connection.execute("INSERT INTO upstream_quota_ledger VALUES (9, 'geocoder', 1780000001)")
        connection.commit()
        connection.execute("INSERT INTO upstream_quota_ledger VALUES (10, 'suggest', 1780000002)")
        assert Path(str(source_path) + "-wal").stat().st_size > 0
        source_bytes = source_path.read_bytes()
        wal_bytes = Path(str(source_path) + "-wal").read_bytes()
        output = tmp_path / "online.db"
        result = backup_database(url(source_path), output)
        assert result["integrity_check"] == result["restore_validation"] == "ok"
        assert result["tables"]["upstream_quota_ledger"] == 2
        with sqlite3.connect(output) as restored:
            assert restored.execute("SELECT id FROM upstream_quota_ledger ORDER BY id").fetchall() == [(8,), (9,)]
            assert restored.execute("SELECT id, catalog_key FROM places").fetchall() == [(77, "stable-catalog-id")]
        assert source_path.read_bytes() == source_bytes
        assert Path(str(source_path) + "-wal").read_bytes() == wal_bytes
        assert validate_restore(output)["upstream_quota_ledger"] == 2
    finally:
        connection.rollback()
        connection.close()


def test_backup_is_consistent_while_another_connection_writes(tmp_path):
    source_path = tmp_path / "live.db"
    connection = make_database(source_path)
    # Enough pages to make SQLite's incremental online copy exercise restarts.
    connection.executemany("INSERT INTO places VALUES (?, ?, ?)",
                           ((i, f"seed-{i}", b"x" * 4096) for i in range(100, 1100)))
    connection.commit()
    ready = threading.Event()
    stop = threading.Event()
    writes = []
    failures = []

    def writer():
        writer_connection = sqlite3.connect(source_path, timeout=5)
        writer_connection.execute("PRAGMA foreign_keys=ON")
        try:
            index = 2000
            while not stop.is_set() and index < 2025:
                writer_connection.execute("INSERT INTO places VALUES (?, ?, ?)", (index, f"live-{index}", b"y"))
                writer_connection.execute("INSERT INTO related VALUES (?, ?)", (index, "same-transaction"))
                writer_connection.execute("INSERT INTO upstream_quota_ledger VALUES (?, 'suggest', ?)", (index, float(index)))
                writer_connection.commit()
                writes.append(time.monotonic())
                ready.set()
                index += 1
                time.sleep(0.005)
        except Exception as error:
            failures.append(type(error).__name__)
        finally:
            writer_connection.close()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert ready.wait(5)
        started = time.monotonic()
        result = backup_database(url(source_path), tmp_path / "live-backup.db", timeout_seconds=10)
        finished = time.monotonic()
        assert result["status"] == "ok"
        assert any(started <= instant <= finished for instant in writes)
        with sqlite3.connect(tmp_path / "live-backup.db") as restored:
            assert restored.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert restored.execute("PRAGMA foreign_key_check").fetchall() == []
            related = restored.execute("SELECT count(*) FROM related").fetchone()[0]
            live_places = restored.execute("SELECT count(*) FROM places WHERE id >= 2000").fetchone()[0]
            live_ledger = restored.execute("SELECT count(*) FROM upstream_quota_ledger WHERE id >= 2000").fetchone()[0]
            assert related == live_places == live_ledger
    finally:
        stop.set()
        thread.join(5)
        connection.close()
    assert not thread.is_alive() and failures == []


@pytest.mark.parametrize("value", ["", "sqlite:///main.db", "sqlite+aiosqlite:///:memory:",
                                  "sqlite+aiosqlite:///file:main.db", "sqlite+aiosqlite:///main.db?mode=rw"])
def test_reject_unsupported_database_urls(value):
    with pytest.raises(BackupError):
        database_path(value)


def test_output_source_alias_and_existing_file_are_never_overwritten(tmp_path):
    source_path = tmp_path / "main.db"
    connection = make_database(source_path)
    connection.close()
    original = source_path.read_bytes()
    with pytest.raises(BackupError, match="differ"):
        backup_database(url(source_path), source_path)
    existing = tmp_path / "existing.db"
    existing.write_bytes(b"must-preserve")
    with pytest.raises(BackupError, match="already exists"):
        backup_database(url(source_path), existing)
    hardlink = tmp_path / "alias.db"
    os.link(source_path, hardlink)
    with pytest.raises(BackupError):
        backup_database(url(source_path), hardlink)
    assert existing.read_bytes() == b"must-preserve" and source_path.read_bytes() == original


def test_missing_source_wrong_database_and_sidecar_outputs_are_rejected(tmp_path):
    with pytest.raises(BackupError, match="already exist"):
        backup_database(url(tmp_path / "missing.db"), tmp_path / "out.db")
    assert not (tmp_path / "missing.db").exists()
    wrong = tmp_path / "osm.sqlite"
    with sqlite3.connect(wrong) as connection:
        connection.execute("CREATE TABLE buildings (id INTEGER)")
    with pytest.raises(BackupError, match="durable quota ledger"):
        backup_database(url(wrong), tmp_path / "out.db")
    assert not (tmp_path / "out.db").exists()
    source = tmp_path / "main.db"
    connection = make_database(source)
    connection.close()
    with pytest.raises(BackupError, match="sidecar"):
        backup_database(url(source), Path(str(source) + "-wal"))
    with pytest.raises(BackupError, match="directory"):
        backup_database(url(source), tmp_path / "absent" / "out.db")


def test_integrity_failure_removes_only_new_output_and_verify_only_is_readonly(tmp_path):
    source = tmp_path / "bad.db"
    source.write_bytes(b"not a sqlite database")
    with pytest.raises(BackupError):
        backup_database(url(source), tmp_path / "new.db")
    assert source.read_bytes() == b"not a sqlite database" and not (tmp_path / "new.db").exists()
    source.unlink()
    connection = make_database(source)
    connection.close()
    output = tmp_path / "export.db"
    backup_database(url(source), output)
    original = output.read_bytes()
    command = [sys.executable, "scripts/sqlite_backup.py", "--verify-only", str(output)]
    completed = subprocess.run(command, capture_output=True, text=True, cwd=Path(__file__).parents[1])
    assert completed.returncode == 0
    assert json.loads(completed.stdout)["restore_validation"] == "ok"
    assert output.read_bytes() == original


def test_failed_cli_does_not_echo_input_or_database_contents(tmp_path):
    sample = "sensitive-example-url"
    command = [sys.executable, "scripts/sqlite_backup.py", "--database-url", sample,
               "--output", str(tmp_path / "output.db")]
    completed = subprocess.run(command, capture_output=True, text=True, cwd=Path(__file__).parents[1])
    assert completed.returncode == 1
    assert sample not in completed.stdout + completed.stderr


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout_is_rejected_before_file_creation(tmp_path, timeout):
    with pytest.raises(BackupError, match="finite"):
        backup_database(url(tmp_path / "unused.db"), tmp_path / "never.db", timeout_seconds=timeout)
    assert not (tmp_path / "never.db").exists()


def test_total_timeout_bounds_a_real_exclusive_database_lock(tmp_path):
    source = tmp_path / "locked.db"
    connection = make_database(source)
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("BEGIN EXCLUSIVE")
    started = time.monotonic()
    try:
        with pytest.raises(BackupError):
            backup_database(url(source), tmp_path / "never.db", timeout_seconds=0.02)
        assert time.monotonic() - started < 0.5
        assert not (tmp_path / "never.db").exists()
    finally:
        connection.rollback()
        connection.close()


def test_same_total_deadline_includes_restore_fingerprint(tmp_path, monkeypatch):
    import scripts.sqlite_backup as module
    source = tmp_path / "main.db"
    connection = make_database(source)
    connection.close()
    original = module._fingerprint

    def delayed(connection, deadline):
        time.sleep(0.03)
        return original(connection, deadline)

    monkeypatch.setattr(module, "_fingerprint", delayed)
    with pytest.raises(BackupError, match="total time limit"):
        backup_database(url(source), tmp_path / "never.db", timeout_seconds=0.02)
    assert not (tmp_path / "never.db").exists()
