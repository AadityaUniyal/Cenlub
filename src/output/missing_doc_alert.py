"""
src/output/missing_doc_alert.py
================================
Phase 5.3 — Missing Document Alert

If any critical document (P&ID, Vendor List) is MISSING from the checklist,
auto-generate a polite email to the customer contact requesting those items.

send_missing_doc_alert()  : compose and (optionally) send the alert.
compose_missing_doc_email(): returns the subject + body as strings (testable).
"""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from src.core.models import ChecklistDoc

log = logging.getLogger(__name__)

# Documents considered "critical" — their absence blocks quoting
CRITICAL_DOCS = [
    ("pid_drawing", "P&ID Drawing"),
    ("ga_drawing", "GA Drawing"),
    ("vendor_list", "Vendor List"),
    ("customer_ts_los", "Customer Technical Specification for LOS"),
    ("mech_spec", "Mechanical Items Specification"),
    ("instrumentation_spec", "Instrumentation Specification"),
]


def compose_missing_doc_email(
    checklist: ChecklistDoc,
    contact_name: str,
    contact_email: str,
    lead_no: str,
    rfq_ref: str,
    project: str,
) -> Optional[tuple]:
    """
    Phase 5.3: Check which critical documents are MISSING and compose the alert.

    Returns (subject, body) if any documents are missing, or None if all clear.
    """
    missing = [
        label
        for field, label in CRITICAL_DOCS
        if getattr(checklist, field, "MISSING") in ("MISSING", "XX", "")
    ]

    if not missing:
        log.info("Phase 5.3: All critical documents present — no alert needed.")
        return None

    bullet_list = "\n".join(f"  • {doc}" for doc in missing)
    subject = (
        f"Document Request — {rfq_ref} / {project} — Lead: {lead_no}"
    )
    body = (
        f"Dear {contact_name or 'Sir/Madam'},\n\n"
        f"Thank you for your inquiry (Ref: {rfq_ref} / {project}, "
        f"Lead No: {lead_no}).\n\n"
        f"To proceed with the techno-commercial offer, we require the following "
        f"document(s) which appear to be missing from the package:\n\n"
        f"{bullet_list}\n\n"
        f"Request you to kindly share the above at your earliest convenience "
        f"so we may provide an accurate and timely quotation.\n\n"
        f"Regards,\n"
        f"Engineering Team\n"
        f"Cenlub Systems"
    )
    log.info(
        "Phase 5.3: Missing doc alert composed for %s — %d docs missing.",
        lead_no, len(missing),
    )
    return subject, body


def send_missing_doc_alert(
    checklist: ChecklistDoc,
    contact_name: str,
    contact_email: str,
    lead_no: str,
    rfq_ref: str,
    project: str,
    smtp_host: str = "",
    smtp_port: int = 587,
    smtp_user: str = "",
    smtp_pass: str = "",
    from_addr: str = "",
) -> dict:
    """
    Phase 5.3: Send the missing-document alert to the customer.

    Returns {"status": "sent"|"dry_run"|"nothing_missing", "missing": [...]}
    """
    result = compose_missing_doc_email(
        checklist, contact_name, contact_email, lead_no, rfq_ref, project
    )
    if result is None:
        return {"status": "nothing_missing", "missing": []}

    subject, body = result
    missing_labels = [
        label
        for field, label in CRITICAL_DOCS
        if getattr(checklist, field, "MISSING") in ("MISSING", "XX", "")
    ]

    if not smtp_host or not smtp_user or not contact_email:
        log.warning(
            "SMTP not configured or no contact email — "
            "missing doc alert not sent (dry run)."
        )
        return {
            "status": "dry_run",
            "missing": missing_labels,
            "subject": subject,
            "body": body,
        }

    try:
        msg = MIMEMultipart()
        msg["From"] = from_addr or smtp_user
        msg["To"] = contact_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, contact_email, msg.as_string())

        log.info("Missing doc alert sent to %s for lead %s.", contact_email, lead_no)
        return {"status": "sent", "missing": missing_labels}

    except Exception as exc:
        log.error("Failed to send missing doc alert: %s", exc)
        return {"status": "error", "error": str(exc), "missing": missing_labels}
