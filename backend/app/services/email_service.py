"""Email service using Azure Communication Services."""

import base64
import json
import logging
import mimetypes
import re
from pathlib import PurePosixPath
from app.config import settings

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

# Azure Communication Services rejects any email whose total request body
# (subject + body + Base64-encoded attachments + JSON envelope) exceeds 10 MB
# with: "(RequestTooLarge) The request body is too large for scheme: AcsHMAC".
# See https://learn.microsoft.com/azure/communication-services/concepts/service-limits
ACS_MAX_REQUEST_BYTES = 10 * 1024 * 1024

# Budget conservatively below the hard limit to leave headroom for the JSON
# envelope and recipient metadata we don't measure directly. Attachment sizes
# are measured after Base64 encoding, which already reflects the ~33% inflation.
_REQUEST_SIZE_BUDGET = 9 * 1024 * 1024


def _json_bytes(text: str) -> int:
    """Bytes this string occupies in the JSON request body.

    azure-core serializes the request with ``json.dumps`` (``ensure_ascii=True``),
    so non-ASCII characters are escaped (e.g. ``é`` -> ``\\u00e9``). Measuring the
    serialized length upper-bounds the on-the-wire size for any content, which
    keeps us safely under the ACS limit regardless of Unicode usage.
    """
    return len(json.dumps(text))


def _truncate_to_json_bytes(text: str, max_bytes: int) -> str:
    """Truncate text so its JSON-serialized form fits within max_bytes."""
    if max_bytes <= 2:  # need room for the surrounding quotes
        return ""
    if _json_bytes(text) <= max_bytes:
        return text
    # Binary search for the longest character prefix whose serialized form fits.
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _json_bytes(text[:mid]) <= max_bytes:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def send_result_email(
    to: str,
    subject: str,
    body: str,
    attachments: list[tuple[str, str | bytes]] | None = None,
) -> tuple[bool, str]:
    """Send an email with results. Returns (success, error_reason).

    Args:
        to: Recipient email address.
        subject: Email subject line.
        body: Plain-text email body.
        attachments: Optional list of (filename, content) tuples. Content can be
            a str (UTF-8 text) or bytes.
    """
    if not _EMAIL_RE.match(to):
        logger.warning("Invalid email address rejected: %s", to)
        return False, f"Invalid email address: {to}"

    if not settings.azure_comm_connection_string:
        logger.warning("AZURE_COMM_CONNECTION_STRING not set, skipping email to %s", to)
        return False, "AZURE_COMM_CONNECTION_STRING not configured"

    if not settings.email_sender_address:
        logger.warning("EMAIL_SENDER_ADDRESS not set, skipping email to %s", to)
        return False, "EMAIL_SENDER_ADDRESS not configured"

    try:
        from azure.communication.email import EmailClient

        client = EmailClient.from_connection_string(settings.azure_comm_connection_string)

        # Stay under the ACS 10 MB total-request limit so we never trigger a
        # RequestTooLarge failure. Track the running request size as we go.
        used_bytes = _json_bytes(subject) + _json_bytes(body)

        # If the body alone exceeds the budget, truncate it and flag the truncation.
        if used_bytes > _REQUEST_SIZE_BUDGET:
            notice = "\n\n[... output truncated: exceeded the 10 MB email size limit ...]"
            available = _REQUEST_SIZE_BUDGET - _json_bytes(subject) - _json_bytes(notice)
            body = _truncate_to_json_bytes(body, max(0, available)) + notice
            used_bytes = _json_bytes(subject) + _json_bytes(body)
            logger.warning("Email body truncated to fit the ACS 10 MB request limit for %s", to)

        email_attachments = []
        omitted: list[str] = []
        if attachments:
            for filename, content in attachments:
                try:
                    # Ensure content is bytes before base64 encoding
                    if isinstance(content, str):
                        content_bytes = content.encode("utf-8")
                    else:
                        content_bytes = content

                    # Verify base64 encoding worked
                    encoded = base64.b64encode(content_bytes).decode("ascii")
                    if not encoded:
                        logger.warning("Failed to encode attachment %s: empty base64 result", filename)
                        continue

                    # Skip attachments that would push the request over the ACS limit.
                    if used_bytes + len(encoded) > _REQUEST_SIZE_BUDGET:
                        logger.warning(
                            "Skipping attachment %s: encoded size %d bytes would exceed the "
                            "ACS 10 MB email request limit",
                            filename, len(encoded),
                        )
                        omitted.append(filename)
                        continue

                    # Sanitize filename to contain only safe characters
                    safe_filename = PurePosixPath(filename).name
                    # Remove any non-alphanumeric chars except dots, hyphens, underscores
                    safe_filename = "".join(c if c.isalnum() or c in ".-_" else "_" for c in safe_filename)

                    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                    email_attachments.append({
                        "name": safe_filename,
                        "contentType": content_type,
                        "contentInBase64": encoded,
                    })
                    used_bytes += len(encoded)
                except Exception as attach_err:
                    logger.warning("Failed to encode attachment %s: %s", filename, attach_err)
                    continue

        # If we had to drop attachments, tell the recipient in the body.
        if omitted:
            body += (
                "\n\n" + ("=" * 60) + "\n"
                + f"Note: {len(omitted)} attachment(s) were omitted because the email "
                + "exceeded the 10 MB size limit:\n"
                + "\n".join(f"  - {name}" for name in omitted)
                + "\nThese files remain available in your workspace storage."
            )

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

        if email_attachments:
            message["attachments"] = email_attachments

        poller = client.begin_send(message)
        result = poller.result()
        logger.info("Email sent to %s, message ID: %s", to, result.get("id", "unknown"))
        return True, ""

    except Exception as exc:
        logger.exception("Failed to send email to %s", to)
        return False, str(exc)
