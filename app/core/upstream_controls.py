"""Bound Yandex attempts without retaining any search, result, or client data.

The durable limits use a conservative rolling 24 hours, not an assumed Yandex
calendar reset. They protect attempts through this database only. Concurrency
is shared within one process; production therefore defaults to one worker.
"""
import math
import time
from collections.abc import Callable

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import settings


QUOTA_TABLE = 'upstream_quota_ledger'
ROLLING_DAY_SECONDS = 24 * 60 * 60


async def initialize_quota_schema(db_engine: AsyncEngine) -> None:
    """Create the attempt ledger in the same persistent SQLite database as POIs."""
    async with db_engine.begin() as connection:
        await connection.execute(text(
            f'CREATE TABLE IF NOT EXISTS {QUOTA_TABLE} ('
            'id INTEGER PRIMARY KEY, '
            "product TEXT NOT NULL CHECK(product IN ('suggest', 'geocoder')), "
            "reserved_at REAL NOT NULL CHECK(typeof(reserved_at) IN ('integer', 'real'))"
            ')'
        ))
        await connection.execute(text(
            f'CREATE INDEX IF NOT EXISTS ix_upstream_quota_product_time '
            f'ON {QUOTA_TABLE} (product, reserved_at)'
        ))
        await connection.execute(text(
            f'CREATE INDEX IF NOT EXISTS ix_upstream_quota_time '
            f'ON {QUOTA_TABLE} (reserved_at)'
        ))


def _get_engine() -> AsyncEngine:
    # Resolve lazily: db.session imports schema initialization during startup.
    from app.db.session import engine
    return engine


class _Admission:
    """Idempotent release also covers a task cancelled before its first step."""
    def __init__(self, controls: 'UpstreamControls'):
        self.controls = controls
        self.released = False

    def release(self) -> None:
        if not self.released:
            self.released = True
            self.controls._active -= 1


class UpstreamControls:
    def __init__(self, engine: AsyncEngine | None = None,
                 clock: Callable[[], float] | None = None):
        self._engine = engine
        self._clock = clock or time.time
        self._active = 0

    def acquire(self) -> _Admission:
        """No await or queue: admission is atomic within the event-loop thread."""
        if self._active >= settings.YANDEX_MAX_CONCURRENT:
            raise HTTPException(503, 'Сервис поиска перегружен. Повторите попытку позже.',
                                headers={'Retry-After': '1'})
        self._active += 1
        return _Admission(self)

    @staticmethod
    def _limited(detail: str, retry_after: float) -> HTTPException:
        return HTTPException(429, detail,
                             headers={'Retry-After': str(max(1, math.ceil(retry_after)))})

    async def reserve(self, product: str) -> None:
        """Charge immediately before GET; failed/cancelled attempts are never refunded.

        BEGIN IMMEDIATE serializes count-and-insert across SQLite connections
        (also across processes sharing the same file). No external call is
        allowed if reading, writing, locking, or committing the ledger fails.
        """
        if product not in ('suggest', 'geocoder'):
            raise ValueError('Unknown upstream product')
        budget = (settings.YANDEX_SUGGEST_DAILY_BUDGET if product == 'suggest'
                  else settings.YANDEX_GEOCODER_DAILY_BUDGET)
        rate_window = settings.YANDEX_RATE_WINDOW_SECONDS
        try:
            async with (self._engine or _get_engine()).connect() as connection:
                await connection.exec_driver_sql('BEGIN IMMEDIATE')
                # Waiting for SQLite's write lock must not backdate an attempt.
                now = self._clock()
                await connection.execute(text(
                    f'DELETE FROM {QUOTA_TABLE} WHERE reserved_at <= :cutoff'
                ), {'cutoff': now - max(ROLLING_DAY_SECONDS, rate_window)})
                daily = (await connection.execute(text(
                    f'SELECT COUNT(*), MIN(reserved_at) FROM {QUOTA_TABLE} '
                    'WHERE product = :product AND reserved_at > :cutoff'
                ), {'product': product, 'cutoff': now - ROLLING_DAY_SECONDS})).one()
                if daily[0] >= budget:
                    retry = ROLLING_DAY_SECONDS if daily[1] is None else (
                        daily[1] + ROLLING_DAY_SECONDS - now)
                    raise self._limited(
                        'Превышен суточный бюджет сервиса поиска. Повторите попытку позже.', retry)
                recent = (await connection.execute(text(
                    f'SELECT COUNT(*), MIN(reserved_at) FROM {QUOTA_TABLE} '
                    'WHERE reserved_at > :cutoff'
                ), {'cutoff': now - rate_window})).one()
                if recent[0] >= settings.YANDEX_SERVER_RATE_LIMIT:
                    raise self._limited(
                        'Превышен лимит запросов сервиса поиска. Повторите попытку позже.',
                        recent[1] + rate_window - now)
                await connection.execute(text(
                    f'INSERT INTO {QUOTA_TABLE} (product, reserved_at) VALUES (:product, :now)'
                ), {'product': product, 'now': now})
                await connection.commit()
        except SQLAlchemyError:
            # No exception detail: filesystem paths and SQL must stay private.
            raise HTTPException(503, 'Защита квоты сервиса поиска временно недоступна.',
                                headers={'Retry-After': '1'}) from None


# Every YandexSearchService instance in this process shares admission slots.
upstream_controls = UpstreamControls()
