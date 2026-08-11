"""IMAP/SMTP email connector."""

from social_reply.connectors.email.adapter import EmailInboundAdapter
from social_reply.connectors.email.client import EmailClient
from social_reply.connectors.email.imap_client import EmailImapClient, ImapClientError
from social_reply.connectors.email.network import EmailNetworkError, ResolvedNetworkTarget

__all__ = [
    "EmailClient",
    "EmailImapClient",
    "EmailInboundAdapter",
    "EmailNetworkError",
    "ImapClientError",
    "ResolvedNetworkTarget",
]
