"""Durable, zero-cost quota ledger for the C13 assistant.

The ledger lives in the same persistent SQLite file as the catalog but uses a
separate table and its own limits. `BEGIN IMMEDIATE` serializes count-and-insert
across connections, so concurrent requests cannot oversubscribe the global or
per-source windows. A failed/cancelled upstream attempt is never refunded: it
may already have consumed provider quota and refunding would invite retries.

No raw client address or conversation text is stored. Only a salted hash of the
source address and the server-configured model id are persisted.
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import settings

QUOTA_TABLE = "assistant_quota_ledger"
STATE_TABLE = "assistant_runtime_state"
MINUTE_SECONDS = 60
DAY_SECONDS = 24 * 60 * 60

OUTCOMES = {
    "reserved", "ok", "provider_error", "timeout", "cancelled",
    "guard_error", "paid_usage", "auth_error", "quota_error",
}


class AssistantQuotaError(Exception):
    """Base class for safe quota failures; no SQL or paths are exposed."""


class AssistantQuotaExceeded(AssistantQuotaError):
    def __init__(self, scope: str, retry_after: float):
        super().__init__("assistant quota exceeded")
        self.scope = scope
        self.retry_after = max(1, math.ceil(retry_after))


class AssistantStorageUnavailable(AssistantQuotaError):
    """The durable guard cannot be read or written; no upstream call is allowed."""


async def initialize_assistant_schema(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as connection:
        await connection.execute(text(
            f'CREATE TABLE IF NOT EXISTS {QUOTA_TABLE} ('
            'id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'request_id TEXT NOT NULL, '
            'attempt INTEGER NOT NULL CHECK(attempt BETWEEN 1 AND 2), '
            'source_hash TEXT NOT NULL, '
            'model_id TEXT NOT NULL, '
            'reserved_at REAL NOT NULL, '
            'settled_at REAL, '
            "outcome TEXT NOT NULL DEFAULT 'reserved', "
            'provider_cost REAL, '
            'error_code TEXT, '
            'UNIQUE(request_id, attempt)'
            ')'
        ))
        await connection.execute(text(
            f'CREATE INDEX IF NOT EXISTS ix_assistant_quota_reserved '
            f'ON {QUOTA_TABLE} (reserved_at)'
        ))
        await connection.execute(text(
            f'CREATE INDEX IF NOT EXISTS ix_assistant_quota_source '
            f'ON {QUOTA_TABLE} (source_hash, reserved_at)'
        ))
        await connection.execute(text(
            f'CREATE TABLE IF NOT EXISTS {STATE_TABLE} ('
            'key TEXT PRIMARY KEY, '
            'value TEXT NOT NULL, '
            'updated_at REAL NOT NULL'
            ')'
        ))


def _get_engine() -> AsyncEngine:
    # Resolve lazily: db.session imports this schema during startup.
    from app.db.session import engine
    return engine


class AssistantQuotaLedger:
    def __init__(
        self,
        engine: AsyncEngine | None = None,
        clock: Callable[[], float] | None = None,
        config: Any | None = None,
    ):
        self._engine = engine
        self._clock = clock or time.time
        self._settings = config or settings

    async def _window(
        self,
        connection,
        since: float,
        source_hash: str | None = None,
    ) -> tuple[int, float | None]:
        sql = (f'SELECT COUNT(*), MIN(reserved_at) FROM {QUOTA_TABLE} '
               'WHERE reserved_at > :since')
        params: dict[str, Any] = {"since": since}
        if source_hash is not None:
            sql += ' AND source_hash = :source'
            params["source"] = source_hash
        row = (await connection.execute(text(sql), params)).one()
        return int(row[0]), (float(row[1]) if row[1] is not None else None)

    @staticmethod
    def _retry(now: float, oldest: float | None, window: float) -> float:
        return window if oldest is None else max(1.0, oldest + window - now)

    async def reserve(
        self,
        *,
        source_hash: str,
        model_id: str,
        request_id: str,
        attempt: int,
    ) -> int:
        """Atomically reserve one upstream attempt and return its ledger id."""
        if attempt not in (1, 2):
            raise ValueError("assistant attempt must be 1 or 2")
        if not isinstance(source_hash, str) or not 16 <= len(source_hash) <= 128:
            raise ValueError("assistant source hash must be bounded text")
        if not isinstance(model_id, str) or not model_id.endswith(":free"):
            raise ValueError("assistant model must be a free model id")
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 64:
            raise ValueError("assistant request id must be bounded text")
        try:
            async with (self._engine or _get_engine()).connect() as connection:
                await connection.exec_driver_sql('BEGIN IMMEDIATE')
                now = self._clock()
                retention = int(self._settings.AI_ASSISTANT_QUOTA_RETENTION_SECONDS)
                await connection.execute(text(
                    f'DELETE FROM {QUOTA_TABLE} WHERE reserved_at <= :cutoff'
                ), {'cutoff': now - retention})

                checks = (
                    ('global_minute', now - MINUTE_SECONDS, None,
                     int(self._settings.AI_ASSISTANT_GLOBAL_PER_MINUTE), MINUTE_SECONDS),
                    ('global_day', now - DAY_SECONDS, None,
                     int(self._settings.AI_ASSISTANT_GLOBAL_PER_DAY), DAY_SECONDS),
                    ('source_minute', now - MINUTE_SECONDS, source_hash,
                     int(self._settings.AI_ASSISTANT_SOURCE_PER_MINUTE), MINUTE_SECONDS),
                    ('source_day', now - DAY_SECONDS, source_hash,
                     int(self._settings.AI_ASSISTANT_SOURCE_PER_DAY), DAY_SECONDS),
                )
                for scope, since, source, limit, window in checks:
                    used, oldest = await self._window(connection, since, source)
                    if used >= limit:
                        raise AssistantQuotaExceeded(
                            scope, self._retry(now, oldest, window))
                row = await connection.execute(text(
                    f'INSERT INTO {QUOTA_TABLE} '
                    '(request_id, attempt, source_hash, model_id, reserved_at, outcome) '
                    "VALUES (:request, :attempt, :source, :model, :now, 'reserved') "
                    'RETURNING id'
                ), {
                    'request': request_id,
                    'attempt': attempt,
                    'source': source_hash,
                    'model': model_id,
                    'now': now,
                })
                reservation_id = int(row.scalar_one())
                await connection.commit()
                return reservation_id
        except AssistantQuotaError:
            raise
        except SQLAlchemyError:
            raise AssistantStorageUnavailable() from None

    async def settle(
        self,
        reservation_id: int,
        *,
        outcome: str,
        provider_cost: float | None = None,
        error_code: str | None = None,
    ) -> bool:
        """Record the final outcome. Settlement failure never hides the answer.

        A failed settlement leaves the reservation in place, which is the
        conservative choice for both quota and audit purposes.
        """
        if outcome not in OUTCOMES or outcome == "reserved":
            raise ValueError("assistant outcome must be terminal")
        if provider_cost is not None and (
            isinstance(provider_cost, bool) or not isinstance(provider_cost, (int, float))
            or not math.isfinite(provider_cost) or provider_cost < 0
        ):
            raise ValueError("assistant provider cost must be a finite non-negative number")
        try:
            async with (self._engine or _get_engine()).begin() as connection:
                result = await connection.execute(text(
                    f'UPDATE {QUOTA_TABLE} SET settled_at = :now, outcome = :outcome, '
                    'provider_cost = :cost, error_code = :error '
                    "WHERE id = :id AND outcome = 'reserved'"
                ), {
                    'id': reservation_id,
                    'now': self._clock(),
                    'outcome': outcome,
                    'cost': provider_cost,
                    'error': (error_code or None) if isinstance(error_code, str) else None,
                })
                settled = result.rowcount == 1
            if provider_cost is not None and provider_cost > 0:
                await self.trip_kill_switch("paid_usage_detected")
            return settled
        except SQLAlchemyError:
            return False
        except AssistantQuotaError:
            return False

    async def trip_kill_switch(self, reason_code: str) -> bool:
        if not isinstance(reason_code, str) or not 1 <= len(reason_code) <= 64:
            raise ValueError("assistant kill-switch reason must be bounded text")
        try:
            async with (self._engine or _get_engine()).begin() as connection:
                await connection.execute(text(
                    f'INSERT INTO {STATE_TABLE} (key, value, updated_at) '
                    "VALUES ('kill_switch', :reason, :now) "
                    'ON CONFLICT(key) DO UPDATE SET value = excluded.value, '
                    'updated_at = excluded.updated_at'
                ), {'reason': reason_code, 'now': self._clock()})
            return True
        except SQLAlchemyError:
            raise AssistantStorageUnavailable() from None

    async def kill_switch_reason(self) -> str | None:
        try:
            async with (self._engine or _get_engine()).connect() as connection:
                result = await connection.execute(text(
                    f"SELECT value FROM {STATE_TABLE} WHERE key = 'kill_switch'"
                ))
                row = result.first()
                return row[0] if row else None
        except SQLAlchemyError:
            raise AssistantStorageUnavailable() from None

    async def global_availability(self) -> tuple[bool, int | None]:
        """Report only whether the global windows are exhausted.

        This deliberately does not expose counts or the remaining budget; the
        public capabilities endpoint uses it to fail closed without a reserve.
        """
        now = self._clock()
        try:
            async with (self._engine or _get_engine()).connect() as connection:
                minute_used, minute_oldest = await self._window(
                    connection, now - MINUTE_SECONDS)
                day_used, day_oldest = await self._window(
                    connection, now - DAY_SECONDS)
            minute_limit = int(self._settings.AI_ASSISTANT_GLOBAL_PER_MINUTE)
            day_limit = int(self._settings.AI_ASSISTANT_GLOBAL_PER_DAY)
            waits: list[float] = []
            if minute_used >= minute_limit:
                waits.append(self._retry(now, minute_oldest, MINUTE_SECONDS))
            if day_used >= day_limit:
                waits.append(self._retry(now, day_oldest, DAY_SECONDS))
            if not waits:
                return False, None
            return True, max(1, math.ceil(max(waits)))
        except SQLAlchemyError:
            raise AssistantStorageUnavailable() from None

    async def stats(self) -> dict[str, int]:
        now = self._clock()
        try:
            async with (self._engine or _get_engine()).connect() as connection:
                minute, _ = await self._window(connection, now - MINUTE_SECONDS)
                day, _ = await self._window(connection, now - DAY_SECONDS)
                return {
                    "global_minute_used": minute,
                    "global_day_used": day,
                    "global_minute_limit": int(self._settings.AI_ASSISTANT_GLOBAL_PER_MINUTE),
                    "global_day_limit": int(self._settings.AI_ASSISTANT_GLOBAL_PER_DAY),
                    "source_minute_limit": int(self._settings.AI_ASSISTANT_SOURCE_PER_MINUTE),
                    "source_day_limit": int(self._settings.AI_ASSISTANT_SOURCE_PER_DAY),
                    "max_concurrent": int(self._settings.AI_ASSISTANT_MAX_CONCURRENT),
                }
        except SQLAlchemyError:
            raise AssistantStorageUnavailable() from None
