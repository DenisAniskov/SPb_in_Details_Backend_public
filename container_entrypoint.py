"""Prepare only the mounted data directory, then exec FastAPI as UID 10001.

The image defaults to UID 10001. Railway's root-owned mount may require the
platform's RAILWAY_RUN_UID=0 override for this bootstrap, never for the server.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys

UID = GID = 10001
DATA = Path('/data')
DATABASE_URL = 'sqlite+aiosqlite:////data/smart_navigator_spb.db'


def prepare_volume() -> None:
    if os.environ.get('RAILWAY_VOLUME_MOUNT_PATH') != '/data':
        raise RuntimeError('Root bootstrap requires the explicit /data volume mount')
    if os.environ.get('DATABASE_URL', DATABASE_URL) != DATABASE_URL:
        raise RuntimeError('Root bootstrap requires the fixed persistent database path')
    if not os.path.ismount(DATA):
        raise RuntimeError('/data must be an actual mounted volume')
    fd = os.open(DATA, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # Only the mount root changes ownership. Never recursively follow paths.
        os.fchown(fd, UID, GID)
        os.fchmod(fd, 0o700)
        for name in ('smart_navigator_spb.db', 'smart_navigator_spb.db-wal',
                     'smart_navigator_spb.db-shm'):
            try:
                item = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(item.st_mode) or item.st_uid != UID:
                raise RuntimeError('Existing SQLite files require a reviewed ownership repair')
    finally:
        os.close(fd)


def main() -> int:
    os.umask(0o077)
    try:
        if os.environ.get('BACKEND_ENV', 'production') in ('staging', 'production'):
            if not os.path.ismount(DATA):
                raise RuntimeError('Staging/production requires an actual /data volume')
            if os.environ.get('DATABASE_URL', DATABASE_URL) != DATABASE_URL:
                raise RuntimeError('Staging/production requires the fixed persistent database path')
        if os.geteuid() == 0:
            prepare_volume()
            os.setgroups([])
            os.setgid(GID)
            os.setuid(UID)
        if os.geteuid() != UID or os.getegid() != GID:
            raise RuntimeError('Backend must run as UID/GID 10001')
        if not os.access(DATA, os.W_OK | os.X_OK):
            raise RuntimeError('Persistent data directory is not writable by the backend')
    except (OSError, RuntimeError) as error:
        # No environment values, credentials, request data or traceback.
        print(f'Container startup refused: {error}', file=sys.stderr)
        return 1
    os.execv(sys.executable, [sys.executable, '/app/run.py'])
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
