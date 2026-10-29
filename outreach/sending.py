"""Stage 6: sending layer (SMTP email with dry-run default + manual Instagram DM queue)."""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
import time
from email.message import EmailMessage

from .enrichment import NOT_FOUND, is_valid_email
from .storage import Database

log = logging.getLogger(__name__)
FINAL_STATUSES = {"sent", "dm_sent_manually"}


class SMTPMailer:
    def __init__(self):
        self.host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
        self.port = int(os.environ.get("SMTP_PORT", "465"))
        self.user = os.environ.get("SMTP_USER", "")
        self.password = os.environ.get("SMTP_PASSWORD", "")
        self.from_email = os.environ.get("FROM_EMAIL") or self.user
        self.from_name = os.environ.get("FROM_NAME", "")
        if not (self.user and self.password and self.from_email):
            raise SystemExit("SMTP_USER, SMTP_PASSWORD and FROM_EMAIL must be set for --live sending.")

    def send(self, to: str, subject: str, body: str) -> None:
        m = EmailMessage()
        m["From"] = f"{self.from_name} <{self.from_email}>" if self.from_name else self.from_email
        m["To"], m["Subject"] = to, subject
        m.set_content(body)
        if self.port == 465:
            with smtplib.SMTP_SSL(self.host, self.port, context=ssl.create_default_context(), timeout=30) as s:
                s.login(self.user, self.password)
                s.send_message(m)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=30) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(self.user, self.password)
                s.send_message(m)


def send_emails(db: Database, cfg: dict, mailer=None, live: bool = False, limit: int | None = None,
                sleep=time.sleep) -> dict:
    """Send (or simulate) approved emails. Idempotent: anyone already 'sent' is never contacted again."""
    s = cfg["sending"]
    limit = limit or s["max_emails_per_run"]
    optout = cfg["campaign"].get("optout_line", "")
    stats = {"sent": 0, "simulated": 0, "failed": 0, "skipped": 0}
    rows = db.fetch(
        "SELECT i.channel_id, i.name, e.email, m.email_subject, m.email_body FROM messages m "
        "JOIN influencers i USING(channel_id) JOIN enrichment e USING(channel_id) WHERE m.status='approved'")
    for r in rows:
        cid, email = r["channel_id"], r["email"]
        if not is_valid_email(email):
            db.record_outreach(cid, "email", email or NOT_FOUND, "skipped_no_email", "no valid contact email")
            stats["skipped"] += 1
            continue
        if db.outreach_status(cid, "email") in FINAL_STATUSES:
            stats["skipped"] += 1
            continue
        if db.is_suppressed(email):
            db.record_outreach(cid, "email", email, "skipped_suppressed", "on suppression list")
            stats["skipped"] += 1
            continue
        if stats["sent"] + stats["simulated"] >= limit:
            break
        body = r["email_body"] + ("\n\n" + optout if optout else "")
        try:
            if live:
                mailer.send(email, r["email_subject"], body)
                db.record_outreach(cid, "email", email, "sent", "delivered to SMTP server")
                stats["sent"] += 1
                sleep(s.get("delay_seconds", 0))
            else:
                db.record_outreach(cid, "email", email, "simulated", "dry-run: not actually sent")
                stats["simulated"] += 1
        except Exception as exc:
            log.warning("Send failed for %s: %s", email, exc)
            db.record_outreach(cid, "email", email, "failed", str(exc)[:200])
            stats["failed"] += 1
    return stats


def queue_dms(db: Database) -> int:
    """Instagram DMs are NOT sent automatically (Meta API does not permit cold outreach).
    Approved DMs are queued for a human to send from the Instagram app."""
    n = 0
    rows = db.fetch("SELECT m.channel_id, e.instagram FROM messages m JOIN enrichment e USING(channel_id) "
                    "WHERE m.status='approved'")
    for r in rows:
        if db.outreach_status(r["channel_id"], "instagram_dm") in FINAL_STATUSES:
            continue
        if r["instagram"] and r["instagram"] != NOT_FOUND:
            db.record_outreach(r["channel_id"], "instagram_dm", r["instagram"], "manual_pending",
                               "send manually from Instagram app, then run `mark-dm`")
            n += 1
        else:
            db.record_outreach(r["channel_id"], "instagram_dm", NOT_FOUND, "skipped_no_instagram",
                               "no Instagram handle found")
    return n
