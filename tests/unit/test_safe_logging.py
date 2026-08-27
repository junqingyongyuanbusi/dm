import logging

import pytest

from social_reply.shared.logging import configure_safe_http_client_logging


def test_safe_http_client_logging_blocks_request_url_telemetry() -> None:
    root_logger = logging.getLogger()
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    httpcore_protocol_logger = logging.getLogger("httpcore.http11")
    original_root_level = root_logger.level
    original_httpx_level = httpx_logger.level
    original_httpcore_level = httpcore_logger.level
    original_protocol_level = httpcore_protocol_logger.level

    try:
        root_logger.setLevel(logging.INFO)
        httpx_logger.setLevel(logging.NOTSET)
        httpcore_logger.setLevel(logging.NOTSET)
        httpcore_protocol_logger.setLevel(logging.NOTSET)

        configure_safe_http_client_logging()

        assert httpx_logger.level == logging.WARNING
        assert httpcore_logger.level == logging.WARNING
        assert httpcore_protocol_logger.getEffectiveLevel() == logging.WARNING
        assert not httpx_logger.isEnabledFor(logging.INFO)
        assert not httpcore_protocol_logger.isEnabledFor(logging.DEBUG)
        assert httpx_logger.isEnabledFor(logging.ERROR)
        assert httpcore_protocol_logger.isEnabledFor(logging.CRITICAL)
    finally:
        root_logger.setLevel(original_root_level)
        httpx_logger.setLevel(original_httpx_level)
        httpcore_logger.setLevel(original_httpcore_level)
        httpcore_protocol_logger.setLevel(original_protocol_level)


@pytest.mark.parametrize("root_level", [logging.ERROR, logging.CRITICAL])
def test_safe_http_client_logging_does_not_lower_inherited_error_threshold(
    root_level: int,
) -> None:
    root_logger = logging.getLogger()
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    original_root_level = root_logger.level
    original_httpx_level = httpx_logger.level
    original_httpcore_level = httpcore_logger.level

    try:
        root_logger.setLevel(root_level)
        httpx_logger.setLevel(logging.NOTSET)
        httpcore_logger.setLevel(logging.NOTSET)

        configure_safe_http_client_logging()

        assert httpx_logger.level == logging.NOTSET
        assert httpcore_logger.level == logging.NOTSET
        assert httpx_logger.getEffectiveLevel() == root_level
        assert httpcore_logger.getEffectiveLevel() == root_level
    finally:
        root_logger.setLevel(original_root_level)
        httpx_logger.setLevel(original_httpx_level)
        httpcore_logger.setLevel(original_httpcore_level)


def test_safe_http_client_logging_preserves_explicit_error_threshold() -> None:
    httpx_logger = logging.getLogger("httpx")
    original_httpx_level = httpx_logger.level

    try:
        httpx_logger.setLevel(logging.ERROR)

        configure_safe_http_client_logging()

        assert httpx_logger.level == logging.ERROR
    finally:
        httpx_logger.setLevel(original_httpx_level)
