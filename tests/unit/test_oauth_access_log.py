import io
import logging

from apps.api.main import OAuthCallbackAccessLogFilter, _install_application_logging


def _record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1", "GET", path, "1.1", 303),
        exc_info=None,
    )


def test_x_oauth_callback_access_log_redacts_query():
    record = _record("/admin/oauth/x/callback?oauth_token=request-token&oauth_verifier=verifier")

    assert OAuthCallbackAccessLogFilter().filter(record) is True
    rendered = record.getMessage()
    assert "/admin/oauth/x/callback HTTP/1.1" in rendered
    assert "request-token" not in rendered
    assert "verifier" not in rendered
    assert "oauth_token" not in rendered


def test_x_oauth_callback_access_log_redacts_trailing_slash_variant():
    record = _record("/admin/oauth/x/callback/?oauth_token=request-token&oauth_verifier=verifier")

    assert OAuthCallbackAccessLogFilter().filter(record) is True
    rendered = record.getMessage()
    assert "/admin/oauth/x/callback HTTP/1.1" in rendered
    assert "request-token" not in rendered
    assert "verifier" not in rendered


def test_access_log_filter_leaves_other_paths_unchanged():
    record = _record("/healthz?verbose=true")

    assert OAuthCallbackAccessLogFilter().filter(record) is True
    assert "/healthz?verbose=true" in record.getMessage()


def test_application_info_logs_use_uvicorn_handler(monkeypatch):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    app_logger = logging.getLogger("social_reply")
    uvicorn_error = logging.getLogger("uvicorn.error")
    monkeypatch.setattr(app_logger, "handlers", [])
    monkeypatch.setattr(app_logger, "propagate", True)
    monkeypatch.setattr(uvicorn_error, "handlers", [handler])

    _install_application_logging()
    logging.getLogger("social_reply.oauth.audit").info("oauth-stage-visible")

    assert app_logger.level == logging.INFO
    assert app_logger.propagate is False
    assert "oauth-stage-visible" in stream.getvalue()
