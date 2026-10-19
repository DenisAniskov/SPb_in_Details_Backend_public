"""Online application SQLite backup; never restores the running database.

Uses only the Python standard library so the deployed runtime can execute it.
The source is opened read-only and the SQLite backup API includes committed WAL
pages. A restore is exercised in a disposable directory before success is returned.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time


REPO_ROOT = Path(__file__).resolve().parents[1]
OSM_DATASET = REPO_ROOT / "data" / "shade_buildings_spb_pilot.sqlite"
REQUIRED_TABLES = {"places", "upstream_quota_ledger"}


class BackupError(RuntimeError):
    """A safe diagnostic that does not include database content or credentials."""


class _Deadline:
    def __init__(self, seconds: float):
        if not math.isfinite(seconds) or seconds <= 0:
            raise BackupError("Backup timeout must be a positive finite number")
        self.ends = time.monotonic() + seconds

    def remaining(self) -> float:
        self.check()
        return max(0, self.ends - time.monotonic())

    def check(self) -> None:
        if time.monotonic() >= self.ends:
            raise BackupError("SQLite backup/restore validation exceeded its total time limit")

    def interrupt_sql(self) -> int:
        return int(time.monotonic() >= self.ends)


def database_path(database_url: str) -> Path:
    """Resolve the same file URL format supported by application settings."""
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        raise BackupError("DATABASE_URL must be a file-backed SQLite aiosqlite URL")
    raw = database_url[len(prefix):]
    if not raw or raw == ":memory:" or "?" in raw or "#" in raw or raw.startswith("file:"):
        raise BackupError("In-memory databases and SQLite URI options are not supported")
    return Path(raw).expanduser().resolve()


def _readonly(path: Path, deadline: _Deadline) -> sqlite3.Connection:
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                                 timeout=min(0.05, deadline.remaining()))
    try:
        connection.set_progress_handler(deadline.interrupt_sql, 1000)
        connection.execute("PRAGMA query_only = ON")
        deadline.check()
    except Exception:
        connection.close()
        raise
    return connection


def _execute(connection: sqlite3.Connection, sql: str, deadline: _Deadline):
    milliseconds = max(0, int(min(0.05, deadline.remaining()) * 1000))
    connection.execute(f"PRAGMA busy_timeout = {milliseconds}")
    connection.set_progress_handler(deadline.interrupt_sql, 1000)
    result = connection.execute(sql)
    deadline.check()
    return result


def _validate(connection: sqlite3.Connection, deadline: _Deadline) -> None:
    if _execute(connection, "PRAGMA integrity_check", deadline).fetchall() != [("ok",)]:
        raise BackupError("SQLite integrity_check failed")
    deadline.check()
    if _execute(connection, "PRAGMA foreign_key_check", deadline).fetchone() is not None:
        raise BackupError("SQLite foreign_key_check failed")
    names = {row[0] for row in _execute(connection,
        "SELECT name FROM sqlite_master WHERE type = 'table'", deadline)}
    if not REQUIRED_TABLES.issubset(names):
        raise BackupError("Source must contain the application catalog and durable quota ledger")
    ledger_columns = {row[1] for row in _execute(connection, "PRAGMA table_info(upstream_quota_ledger)", deadline)}
    if not {"id", "product", "reserved_at"}.issubset(ledger_columns):
        raise BackupError("Durable quota ledger schema is incomplete")


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _fingerprint(connection: sqlite3.Connection, deadline: _Deadline) -> dict:
    """Compare schema and every row without retaining or printing row values."""
    schema = _execute(connection,
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name", deadline
    ).fetchall()
    tables = {}
    for (name,) in _execute(connection,
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name", deadline
    ).fetchall():
        row_hashes = []
        for row in _execute(connection, f"SELECT * FROM {_quote_identifier(name)}", deadline):
            deadline.check()
            # repr includes SQLite storage classes (bytes, None, str, int, float).
            row_hashes.append(hashlib.sha256(repr(row).encode("utf-8")).digest())
        digest = hashlib.sha256(b"".join(sorted(row_hashes))).hexdigest()
        deadline.check()
        tables[name] = {"rows": len(row_hashes), "sha256": digest}
    return {"schema": hashlib.sha256(repr(schema).encode("utf-8")).hexdigest(), "tables": tables}


def _restore_check(backup_path: Path, deadline: _Deadline) -> dict:
    """Restore into a separate temporary SQLite file, then compare every table."""
    with tempfile.TemporaryDirectory(prefix="smartnavigator-restore-") as temporary:
        restored_path = Path(temporary) / "restored.db"
        source = target = None
        try:
            source = _readonly(backup_path, deadline)
            target = sqlite3.connect(restored_path, timeout=min(0.05, deadline.remaining()))
            _validate(source, deadline)
            before = _fingerprint(source, deadline)

            def progress(_status, _remaining, _total):
                deadline.check()

            source.backup(target, pages=128, progress=progress, sleep=min(0.01, deadline.remaining()))
            _validate(target, deadline)
            if _fingerprint(target, deadline) != before:
                raise BackupError("Restored schema or rows differ from the backup")
            deadline.check()
            return {name: details["rows"] for name, details in before["tables"].items()}
        finally:
            if target is not None:
                target.close()
            if source is not None:
                source.close()


def validate_restore(backup_path: Path, *, timeout_seconds: float = 60) -> dict:
    deadline = _Deadline(timeout_seconds)
    try:
        return _restore_check(backup_path, deadline)
    except (sqlite3.Error, OSError):
        deadline.check()
        raise BackupError("SQLite isolated restore validation failed") from None


def backup_database(database_url: str, output: Path | str, *, timeout_seconds: float = 60) -> dict:
    deadline = _Deadline(timeout_seconds)
    source_path = database_path(database_url)
    destination = Path(output).expanduser().absolute()
    if not source_path.is_file():
        raise BackupError("Source database must already exist")
    if source_path == OSM_DATASET.resolve():
        raise BackupError("The read-only OSM dataset is not the application database")
    if destination.resolve() == source_path:
        raise BackupError("Backup output must differ from the source database")
    if destination.resolve() in {Path(str(source_path) + suffix) for suffix in ("-wal", "-shm", "-journal")}:
        raise BackupError("Backup output cannot replace a source database journal or WAL sidecar")
    if destination.exists() or destination.is_symlink():
        raise BackupError("Backup output already exists; overwriting is forbidden")
    if not destination.parent.is_dir():
        raise BackupError("Backup output directory must already exist")
    destination = destination.parent.resolve() / destination.name
    # O_EXCL rejects existing files, hard links and symlinks, including a race
    # between the checks above and creation. Never modify an existing output.
    try:
        descriptor = os.open(destination, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        raise BackupError("Could not exclusively create a new backup output") from None
    os.close(descriptor)
    source = target = None
    succeeded = False
    try:
        source = _readonly(source_path, deadline)
        # Validate schema before copying; full consistency is checked on snapshot.
        names = {row[0] for row in _execute(source, "SELECT name FROM sqlite_master WHERE type='table'", deadline)}
        if not REQUIRED_TABLES.issubset(names):
            raise BackupError("Source must contain the application catalog and durable quota ledger")
        target = sqlite3.connect(destination, timeout=min(0.05, deadline.remaining()))

        def progress(_status, _remaining, _total):
            deadline.check()

        source.backup(target, pages=128, progress=progress, sleep=min(0.01, deadline.remaining()))
        _validate(target, deadline)
        target.close()
        target = None
        source.close()
        source = None
        tables = _restore_check(destination, deadline)
        deadline.check()
        succeeded = True
        return {"status": "ok", "method": "sqlite3-online-backup", "integrity_check": "ok",
                "restore_validation": "ok", "tables": tables, "bytes": destination.stat().st_size}
    except (sqlite3.Error, OSError):
        deadline.check()
        raise BackupError("SQLite backup or isolated restore validation failed") from None
    finally:
        if target is not None:
            target.close()
        if source is not None:
            source.close()
        if not succeeded:
            # Only the output created by this invocation is removed on failure.
            destination.unlink(missing_ok=True)
            for suffix in ("-journal", "-wal", "-shm"):
                Path(str(destination) + suffix).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--output", type=Path, help="New backup file in an existing directory")
    actions.add_argument("--verify-only", type=Path, help="Verify an exported backup in a separate temporary environment")
    parser.add_argument("--database-url", default=None, help="Defaults to DATABASE_URL environment variable")
    parser.add_argument("--timeout-seconds", type=float, default=60)
    args = parser.parse_args()
    try:
        if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
            raise BackupError("Backup timeout must be a positive finite number")
        if args.verify_only:
            if not args.verify_only.is_file():
                raise BackupError("Backup to verify must already exist")
            tables = validate_restore(args.verify_only.resolve(), timeout_seconds=args.timeout_seconds)
            result = {"status": "ok", "integrity_check": "ok", "restore_validation": "ok", "tables": tables}
        else:
            url = args.database_url or os.environ.get("DATABASE_URL", "")
            if not url:
                raise BackupError("DATABASE_URL must be explicitly set in the environment or --database-url")
            result = backup_database(url, args.output, timeout_seconds=args.timeout_seconds)
        print(json.dumps(result, sort_keys=True))
    except BackupError as error:
        print(json.dumps({"status": "error", "reason": str(error)}))
        return 1
    except (sqlite3.Error, OSError):
        print(json.dumps({"status": "error", "reason": "SQLite isolated restore validation failed"}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
