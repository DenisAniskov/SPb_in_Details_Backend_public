"""Pre-deployment online backup for the assistant.

Railway's SSH/SFTP endpoint is not reachable from every operator network, so
the container performs the same tested stdlib online backup at startup, BEFORE
`init_db` can change the schema. On the first C13 deployment this is the
pre-C13 boundary; on C13.1 and later deployments a new label also snapshots an
already-migrated database. The file is independently integrity-checked and
restore-validated by scripts/sqlite_backup.py, and an evidence JSON is written
next to it.

The evidence contains only sizes, hashes and table counts. The temporary
token-gated export endpoint can stream the file for an independent local check;
it is disabled after the operator finishes.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url

from app.core.config import settings

logger = logging.getLogger("app.services.assistant.backup")


class AssistantBackupError(RuntimeError):
    """Safe backup failure; no database content or credentials are included."""


def _database_path(config: Any) -> Path | None:
    try:
        url = make_url(str(config.DATABASE_URL))
    except Exception:
        return None
    if url.drivername != "sqlite+aiosqlite" or not url.database:
        return None
    if url.database == ":memory:":
        return None
    return Path(url.database).expanduser().resolve()


def _backup_dir(source: Path) -> Path:
    return source.parent / "assistant-backups"


def _evidence_path(config: Any) -> Path | None:
    source = _database_path(config)
    if source is None:
        return None
    return _backup_dir(source) / f"{config.AI_ASSISTANT_BACKUP_LABEL}.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _valid_evidence(value: dict[str, Any] | None, label: str) -> bool:
    if not value or value.get("status") != "ok":
        return False
    name = value.get("file")
    sha = value.get("sha256")
    size = value.get("bytes")
    if not isinstance(name, str) or not name.startswith(f"{label}-") \
            or not name.endswith(".db") or "/" in name or "\\" in name:
        return False
    if not isinstance(sha, str) or len(sha) != 64 \
            or any(ch not in "0123456789abcdef" for ch in sha.lower()):
        return False
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        return False
    return True


def read_backup_evidence(config: Any | None = None) -> dict[str, Any] | None:
    config = config or settings
    path = _evidence_path(config)
    if path is None or not path.is_file():
        return None
    value = _read_json(path)
    return value if _valid_evidence(value, config.AI_ASSISTANT_BACKUP_LABEL) else None


def backup_export_file(config: Any | None = None) -> tuple[Path, dict[str, Any]] | None:
    """Return the verified backup file only if it is inside our backup dir."""
    config = config or settings
    evidence = read_backup_evidence(config)
    source = _database_path(config)
    if evidence is None or source is None:
        return None
    directory = _backup_dir(source).resolve()
    candidate = (directory / evidence["file"]).resolve()
    try:
        candidate.relative_to(directory)
    except ValueError:
        return None
    if not candidate.is_file() or candidate.is_symlink():
        return None
    return candidate, evidence


def _has_table(path: Path, table: str) -> bool:
    try:
        connection = sqlite3.connect(
            path.as_uri() + "?mode=ro", uri=True, timeout=1.0)
        try:
            row = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            return row is not None
        finally:
            connection.close()
    except (sqlite3.Error, OSError):
        return False


def _load_backup_script():
    root = Path(__file__).resolve().parents[3]
    script = root / "scripts" / "sqlite_backup.py"
    if not script.is_file():
        raise AssistantBackupError("Backup script is not available in the image")
    spec = importlib.util.spec_from_file_location(
        "smartnavigator_sqlite_backup", script)
    if spec is None or spec.loader is None:
        raise AssistantBackupError("Backup script could not be loaded")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        raise AssistantBackupError("Backup script could not be initialized") from None
    return module


def maybe_backup_before_migration(
    config: Any | None = None,
) -> dict[str, Any] | None:
    """Create one verified backup for this label. Idempotent.

    A new label intentionally backs up an already-migrated database too: the
    C13.1 redeploy must have a current-state snapshot, not only the original
    pre-C13 boundary file.
    """
    config = config or settings
    if not config.AI_ASSISTANT_BACKUP_ON_START:
        return None
    source = _database_path(config)
    if source is None or not source.is_file():
        return None
    if not _has_table(source, "places"):
        return None
    migrated = _has_table(source, "assistant_quota_ledger")
    evidence_path = _evidence_path(config)
    if evidence_path is None:
        return None
    existing = read_backup_evidence(config)
    if existing is not None:
        return existing

    directory = _backup_dir(source)
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        raise AssistantBackupError("Backup directory is not writable") from None

    label = config.AI_ASSISTANT_BACKUP_LABEL
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    output = directory / f"{label}-{timestamp}.db"
    module = _load_backup_script()
    try:
        result = module.backup_database(
            str(config.DATABASE_URL), output, timeout_seconds=60)
    except Exception:
        raise AssistantBackupError("Online backup or restore validation failed") from None
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise AssistantBackupError("Online backup did not report success")
    try:
        digest = hashlib.sha256()
        with output.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        evidence = {
            "status": "ok",
            "method": result.get("method"),
            "file": output.name,
            "bytes": output.stat().st_size,
            "sha256": digest.hexdigest(),
            "integrity_check": result.get("integrity_check"),
            "restore_validation": result.get("restore_validation"),
            "tables": result.get("tables"),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "boundary": (
                "predeploy_existing_schema" if migrated else "pre_c13_schema"
            ),
        }
        temporary = directory / f".{label}.json.tmp"
        temporary.write_text(
            json.dumps(evidence, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, evidence_path)
    except OSError:
        raise AssistantBackupError("Backup evidence could not be written") from None

    tables = evidence.get("tables") if isinstance(evidence.get("tables"), dict) else {}
    logger.info(
        "C13 pre-migration backup verified: bytes=%s sha256=%s tables=%s",
        evidence["bytes"], evidence["sha256"],
        {key: tables.get(key) for key in sorted(tables) if key in {
            "places", "upstream_quota_ledger"}},
    )
    return evidence
