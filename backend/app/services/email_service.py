"""Email service using Azure Communication Services."""

import base64
import logging
import mimetypes
import re
from pathlib import PurePosixPath
from app.config import settings

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


def send_result_email(
    to: str,
    subject: str,
    body: str,
    attachments: list[tuple[str, str | bytes]] | None = None,
) -> bool:
    """Send an email with results. Returns True on success.

    Args:
        to: Recipient email address.
        subject: Email subject line.
        body: Plain-text email body.
        attachments: Optional list of (filename, content) tuples. Content can be
            a str (UTF-8 text) or bytes.
    """
    if not _EMAIL_RE.match(to):
        logger.warning("Invalid email address rejected: %s", to)
        return False

    if not settings.azure_comm_connection_string:
        logger.warning("AZURE_COMM_CONNECTION_STRING not set, skipping email to %s", to)
        return False

    if not settings.email_sender_address:
        logger.warning("EMAIL_SENDER_ADDRESS not set, skipping email to %s", to)
        return False

    try:
        from azure.communication.email import EmailClient

        client = EmailClient.from_connection_string(settings.azure_comm_connection_string)

        message = {
            "senderAddress": settings.email_sender_address,
            "recipients": {
                "to": [{"address": to}],
            },
            "content": {
                "subject": subject,
                "plainText": body,
            },
        }

        if attachments:
            email_attachments = []
            for filename, content in attachments:
                if isinstance(content, str):
                    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
                else:
                    encoded = base64.b64encode(content).decode("ascii")
                content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                email_attachments.append({
                    "name": PurePosixPath(filename).name,
                    "contentType": content_type,
                    "contentInBase64": encoded,
                })
            message["attachments"] = email_attachments

        poller = client.begin_send(message)
        result = poller.result()
        logger.info("Email sent to %s, message ID: %s", to, result.get("id", "unknown"))
        return True

    except Exception:
        logger.exception("Failed to send email to %s", to)
        return False
