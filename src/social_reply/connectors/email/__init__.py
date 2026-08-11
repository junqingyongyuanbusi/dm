"""IMAP/SMTP email connector."""

from social_reply.connectors.email.adapter import EmailInboundAdapter
from social_reply.connectors.email.client import EmailClient

__all__ = ["EmailClient", "EmailInboundAdapter"]
