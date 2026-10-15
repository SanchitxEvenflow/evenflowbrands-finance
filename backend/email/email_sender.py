import logging

import resend

from config import settings

logger = logging.getLogger("email")

# Resend attachment limit is 40 MB total; keep each email under 35 MB raw
MAX_ATTACHMENT_RAW_BYTES = 35 * 1024 * 1024


def _chunk_by_size(
    pdfs: list[tuple[str, bytes]],
    max_bytes: int,
) -> list[list[tuple[str, bytes]]]:
    chunks: list[list[tuple[str, bytes]]] = []
    current: list[tuple[str, bytes]] = []
    current_size = 0

    for name, data in pdfs:
        size = len(data)
        if size > max_bytes:
            if current:
                chunks.append(current)
                current = []
                current_size = 0
            chunks.append([(name, data)])
            continue
        if current_size + size > max_bytes:
            chunks.append(current)
            current = []
            current_size = 0
        current.append((name, data))
        current_size += size

    if current:
        chunks.append(current)
    return chunks


def send_email_with_pdfs(
    to_email: str,
    subject: str,
    pdfs: list[tuple[str, bytes]],
    body: str,
) -> int:
    if not pdfs:
        logger.info("No PDFs to send")
        return 0

    resend.api_key = settings.resend_api_key
    from_address = settings.resend_from_email

    chunks = _chunk_by_size(pdfs, MAX_ATTACHMENT_RAW_BYTES)
    emails_sent = 0

    for i, chunk in enumerate(chunks):
        chunk_subject = f"{subject} ({i + 1}/{len(chunks)})" if len(chunks) > 1 else subject

        attachments: list[resend.Attachment] = [
            {"filename": f"{name}.pdf", "content": list(data)}
            for name, data in chunk
        ]

        params: resend.Emails.SendParams = {
            "from": from_address,
            "to": [to_email],
            "subject": chunk_subject,
            "text": body,
            "attachments": attachments,
        }

        try:
            resend.Emails.send(params)
            emails_sent += 1
            logger.info("Sent email %d/%d with %d PDF(s)", i + 1, len(chunks), len(chunk))
        except Exception:
            logger.exception("Failed to send email %d/%d", i + 1, len(chunks))

    return emails_sent


def send_html(from_email: str, to_email: str, subject: str, html: str, cc: list[str] | None = None) -> None:
    resend.api_key = settings.resend_api_key
    resend.Emails.send({"from": from_email, "to": [to_email], "cc": cc or [], "subject": subject, "html": html})
