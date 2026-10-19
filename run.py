import logging
import os

import uvicorn
from app.core.config import settings


def server_options():
    return dict(
        host=settings.HOST, port=settings.PORT, workers=settings.WORKERS,
        reload=settings.RELOAD, log_level=settings.LOG_LEVEL,
        # Access logs contain URLs and coordinates; keep operational logs only.
        access_log=False, proxy_headers=bool(settings.TRUSTED_PROXY_IPS),
        forwarded_allow_ips=settings.TRUSTED_PROXY_IPS,
        limit_concurrency=settings.SERVER_CONCURRENCY_LIMIT,
        timeout_keep_alive=5, timeout_graceful_shutdown=15,
    )


if __name__ == "__main__":
    os.umask(0o077)
    logging.basicConfig(level=settings.LOG_LEVEL.upper())
    uvicorn.run("app.main:app", **server_options())
