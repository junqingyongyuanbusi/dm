"""EmailInboundAdapter：RFC822 → CanonicalEvent 归一化与防循环过滤。"""

import base64
from datetime import UTC, datetime
from email.message import EmailMessage

from social_reply.connectors.email.adapter import EmailInboundAdapter
from social_reply.domain.messages.canonical import ChannelType


def _eml(
    *,
    from_addr: str = "alice@example.com",
    from_name: str | None = "Alice",
    to: str = "support@corp.com",
    subject: str = "退款咨询",
    body: str | None = "你好，请问怎么退款？",
    html: str | None = None,
    message_id: str | None = "<msg-1@example.com>",
    date: str | None = "Mon, 10 Aug 2026 10:00:00 +0800",
    headers: dict[str, str] | None = None,
) -> bytes:
    message = EmailMessage()
    message["From"] = f"{from_name} <{from_addr}>" if from_name else from_addr
    message["To"] = to
    message["Subject"] = subject
    if message_id is not None:
        message["Message-ID"] = message_id
    if date is not None:
        message["Date"] = date
    for name, value in (headers or {}).items():
        message[name] = value
    if body is not None:
        message.set_content(body)
    if html is not None:
        if body is not None:
            message.add_alternative(html, subtype="html")
        else:
            message.set_content(html, subtype="html")
    return bytes(message)


def _adapter(**overrides) -> EmailInboundAdapter:
    kwargs = {"account_id": "account-1", "self_address": "support@corp.com"}
    kwargs.update(overrides)
    return EmailInboundAdapter(**kwargs)


def test_email_adapter_normalizes_plain_text_message():
    events, status = _adapter().normalize_message(_eml(), uid=5, uidvalidity=11)

    assert status == "PENDING"
    assert len(events) == 1
    event = events[0]
    assert event.platform == "email"
    assert event.platform_account_key == "account-1"
    assert event.external_event_id == "msg-1@example.com"
    assert event.external_user_id == "alice@example.com"
    assert event.conversation_key == "email:account-1:alice@example.com"
    assert event.channel_type is ChannelType.DM
    assert event.text == "退款咨询\n\n你好，请问怎么退款？"
    assert event.external_conversation_id == "msg-1@example.com"
    assert event.occurred_at == datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    assert event.reply_target == {
        "kind": "email",
        "to": "alice@example.com",
        "to_name": "Alice",
        "subject": "退款咨询",
        "message_id": "<msg-1@example.com>",
        "references": "",
    }
    assert event.attachments == []


def test_email_adapter_threads_replies_by_references():
    raw = _eml(
        message_id="<msg-3@example.com>",
        headers={
            "In-Reply-To": "<msg-2@corp.com>",
            "References": "<msg-1@example.com> <msg-2@corp.com>",
        },
    )

    events, status = _adapter().normalize_message(raw, uid=6, uidvalidity=11)

    assert status == "PENDING"
    event = events[0]
    # 线程根 = References 首项；同一发件人仍聚合到同一会话键。
    assert event.external_conversation_id == "msg-1@example.com"
    assert event.conversation_key == "email:account-1:alice@example.com"
    assert event.reply_target["references"] == "<msg-1@example.com> <msg-2@corp.com>"
    assert event.reply_target["message_id"] == "<msg-3@example.com>"


def test_email_adapter_uppercase_sender_addresses_are_normalized():
    raw = _eml(from_addr="Alice.Wang@Example.COM", from_name=None)

    events, status = _adapter().normalize_message(raw, uid=7, uidvalidity=11)

    assert status == "PENDING"
    event = events[0]
    assert event.external_user_id == "alice.wang@example.com"
    assert event.conversation_key == "email:account-1:alice.wang@example.com"
    assert event.reply_target["to"] == "alice.wang@example.com"
    assert event.reply_target["to_name"] is None


# ---------------------------------------------------------------------------
# 防循环过滤：邮件自动回复最大的翻车点，逐条覆盖（RFC 3834）。
# ---------------------------------------------------------------------------


def test_email_adapter_ignores_auto_submitted_mail():
    raw = _eml(headers={"Auto-Submitted": "auto-replied"})
    events, status = _adapter().normalize_message(raw, uid=8, uidvalidity=11)
    assert events == []
    assert status == "IGNORED_AUTO_SUBMITTED"


def test_email_adapter_accepts_auto_submitted_no():
    raw = _eml(headers={"Auto-Submitted": "no"})
    _events, status = _adapter().normalize_message(raw, uid=9, uidvalidity=11)
    assert status == "PENDING"


def test_email_adapter_ignores_x_autoreply_mail():
    for header in ("X-Autoreply", "X-Autorespond"):
        raw = _eml(headers={header: "yes"})
        events, status = _adapter().normalize_message(raw, uid=10, uidvalidity=11)
        assert events == [], header
        assert status == "IGNORED_AUTO_SUBMITTED", header


def test_email_adapter_ignores_bulk_precedence_and_list_mail():
    for headers in (
        {"Precedence": "bulk"},
        {"Precedence": "junk"},
        {"Precedence": "list"},
        {"List-Id": "<news.example.com>"},
        {"List-Unsubscribe": "<mailto:unsub@example.com>"},
    ):
        events, status = _adapter().normalize_message(_eml(headers=headers), uid=11, uidvalidity=11)
        assert events == [], headers
        assert status == "IGNORED_BULK_LIST", headers


def test_email_adapter_ignores_system_senders():
    for from_addr in (
        "mailer-daemon@bounce.example.com",
        "postmaster@example.com",
        "no-reply@example.com",
        "noreply@example.com",
        "donotreply@example.com",
    ):
        events, status = _adapter().normalize_message(
            _eml(from_addr=from_addr, from_name=None), uid=12, uidvalidity=11
        )
        assert events == [], from_addr
        assert status == "IGNORED_SYSTEM_SENDER", from_addr


def test_email_adapter_ignores_system_return_paths():
    for return_path in ("<>", "<mailer-daemon@example.net>", "<bounces@example.net>"):
        raw = _eml(headers={"Return-Path": return_path})
        events, status = _adapter().normalize_message(raw, uid=13, uidvalidity=11)
        assert events == [], return_path
        assert status == "IGNORED_SYSTEM_SENDER", return_path


def test_email_adapter_ignores_mail_from_self():
    raw = _eml(from_addr="Support@Corp.com", from_name=None)
    events, status = _adapter().normalize_message(raw, uid=14, uidvalidity=11)
    assert events == []
    assert status == "IGNORED_SELF"


def test_email_adapter_ignores_internal_domain_by_default():
    raw = _eml(from_addr="colleague@corp.com", from_name=None)
    events, status = _adapter().normalize_message(raw, uid=15, uidvalidity=11)
    assert events == []
    assert status == "IGNORED_INTERNAL"


def test_email_adapter_allows_internal_domain_when_policy_allows():
    adapter = _adapter(internal_domain_policy="allow")
    raw = _eml(from_addr="colleague@corp.com", from_name=None)
    _events, status = adapter.normalize_message(raw, uid=16, uidvalidity=11)
    assert status == "PENDING"


# ---------------------------------------------------------------------------
# 内容处理：HTML、编码、引用裁剪、截断、缺失字段。
# ---------------------------------------------------------------------------


def test_email_adapter_extracts_text_from_html_only_mail():
    raw = _eml(
        body=None,
        html="<html><body><p>你好，</p><p>怎么 <b>退款</b>？</p>"
        "<style>p{color:red}</style></body></html>",
    )
    events, status = _adapter().normalize_message(raw, uid=17, uidvalidity=11)
    assert status == "PENDING"
    text = events[0].text
    assert "怎么 退款 ？" in text or "怎么 退款？" in text or "怎么 退款 ?" in text
    assert "color:red" not in text
    assert "<p>" not in text


def test_email_adapter_uses_html_when_plain_alternative_is_empty():
    raw = _eml(body=" \n", html="<p>真正的客户消息</p>")
    events, status = _adapter().normalize_message(raw, uid=18, uidvalidity=11)
    assert status == "PENDING"
    assert "真正的客户消息" in events[0].text


def test_email_adapter_decodes_gbk_body():
    payload = "你好，请问如何开发票？".encode("gb18030")
    raw = (
        b"From: alice@example.com\r\n"
        b"To: support@corp.com\r\n"
        b"Subject: =?gb18030?B?" + base64.b64encode("发票".encode("gb18030")) + b"?=\r\n"
        b"Message-ID: <gbk-1@example.com>\r\n"
        b"Date: Mon, 10 Aug 2026 10:00:00 +0800\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: text/plain; charset="gb18030"\r\n'
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n" + payload + b"\r\n"
    )
    events, status = _adapter().normalize_message(raw, uid=18, uidvalidity=11)
    assert status == "PENDING"
    assert "如何开发票" in events[0].text
    assert events[0].text.startswith("发票")


def test_email_adapter_falls_back_for_unknown_charset():
    raw = (
        b"From: alice@example.com\r\n"
        b"To: support@corp.com\r\n"
        b"Subject: unknown charset\r\n"
        b"Message-ID: <charset-1@example.com>\r\n"
        b'MIME-Version: 1.0\r\nContent-Type: text/plain; charset="x-unknown"\r\n'
        b"Content-Transfer-Encoding: 8bit\r\n\r\nhello \xff\r\n"
    )
    events, status = _adapter().normalize_message(raw, uid=19, uidvalidity=11)
    assert status == "PENDING"
    assert "hello" in events[0].text


def test_email_adapter_strips_quoted_history():
    body = (
        "好的，那我提供订单号 12345。\n"
        "\n"
        "On Mon, Aug 10, 2026 at 9:00 AM Support <support@corp.com> wrote:\n"
        "> 请提供订单号。\n"
        "> 谢谢。\n"
    )
    events, status = _adapter().normalize_message(_eml(body=body), uid=19, uidvalidity=11)
    assert status == "PENDING"
    assert "订单号 12345" in events[0].text
    assert "请提供订单号" not in events[0].text


def test_email_adapter_truncates_very_long_body():
    events, status = _adapter().normalize_message(_eml(body="问" * 10_000), uid=20, uidvalidity=11)
    assert status == "PENDING"
    assert len(events[0].text) <= 4000


def test_email_adapter_missing_message_id_gets_deterministic_fallback():
    first, status_a = _adapter().normalize_message(_eml(message_id=None), uid=21, uidvalidity=11)
    second, status_b = _adapter().normalize_message(_eml(message_id=None), uid=21, uidvalidity=11)
    assert status_a == status_b == "PENDING"
    assert first[0].external_event_id == second[0].external_event_id
    assert first[0].external_event_id  # 非空


def test_email_adapter_collects_attachment_metadata_only():
    message = EmailMessage()
    message["From"] = "alice@example.com"
    message["To"] = "support@corp.com"
    message["Subject"] = "带附件"
    message["Message-ID"] = "<att-1@example.com>"
    message["Date"] = "Mon, 10 Aug 2026 10:00:00 +0800"
    message.set_content("见附件")
    message.add_attachment(
        b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="发票.pdf"
    )
    events, status = _adapter().normalize_message(bytes(message), uid=22, uidvalidity=11)
    assert status == "PENDING"
    assert events[0].attachments == [{"filename": "发票.pdf", "content_type": "application/pdf"}]
    assert "见附件" in events[0].text


def test_email_adapter_does_not_read_attached_email_body():
    attached = EmailMessage()
    attached["From"] = "other@example.com"
    attached["To"] = "alice@example.com"
    attached["Subject"] = "Forwarded secret"
    attached.set_content("attachment-only secret")

    message = EmailMessage()
    message["From"] = "alice@example.com"
    message["To"] = "support@corp.com"
    message["Subject"] = "Forwarded message"
    message["Message-ID"] = "<attached-email@example.com>"
    message.set_content("customer body")
    message.add_attachment(attached, filename="forwarded.eml")

    events, status = _adapter().normalize_message(bytes(message), uid=23, uidvalidity=11)
    assert status == "PENDING"
    assert "customer body" in events[0].text
    assert "attachment-only secret" not in events[0].text
    assert events[0].attachments == [
        {"filename": "forwarded.eml", "content_type": "message/rfc822"}
    ]


def test_email_adapter_unparseable_from_is_rejected():
    raw = (
        b"To: support@corp.com\r\n"
        b"Subject: no sender\r\n"
        b"Message-ID: <broken-1@example.com>\r\n"
        b"\r\nhello\r\n"
    )
    events, status = _adapter().normalize_message(raw, uid=23, uidvalidity=11)
    assert events == []
    assert status == "EMAIL_SCHEMA_UNSUPPORTED"
