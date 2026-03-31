"""Email service using Azure Communication Services."""

import logging
from app.config import settings

logger = logging.getLogger(__name__)


def send_result_email(to: str, subject: str, body: str) -> bool:
    """Send an email with cron job results. Returns True on success."""
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

        poller = client.begin_send(message)
        result = poller.result()
        logger.info("Email sent to %s, message ID: %s", to, result.get("id", "unknown"))
        return True

    except Exception:
        logger.exception("Failed to send email to %s", to)
        return False
