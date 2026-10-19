"""Avoid ASGI server traceback logs containing request inputs or credentials."""
import logging


class SafeServerErrors(logging.Filter):
    def filter(self, record):
        if record.exc_info:
            kind = record.exc_info[0].__name__
            record.msg = "ASGI server error (%s)"
            record.args = (kind,)
            record.exc_info = None
            record.exc_text = None
        elif "Traceback (most recent call last)" in record.getMessage():
            # Uvicorn lifespan logs a preformatted startup traceback string.
            record.msg = "Application lifecycle failed; check configuration, volume permissions and schema."
            record.args = ()
            record.exc_text = None
        return True
