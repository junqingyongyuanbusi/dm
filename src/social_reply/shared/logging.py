"""Shared production logging safeguards."""

import logging

_HTTP_CLIENT_LOGGERS = ("httpx", "httpcore")


def configure_safe_http_client_logging() -> None:
    """Prevent HTTP client request URLs from exposing credentials in production logs."""
    for logger_name in _HTTP_CLIENT_LOGGERS:
        client_logger = logging.getLogger(logger_name)
        if client_logger.getEffectiveLevel() < logging.WARNING:
            client_logger.setLevel(logging.WARNING)
