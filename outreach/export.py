"""Stage 8: CSV/Markdown deliverables (dataset, messages, outreach tracker)."""
from __future__ import annotations

import csv
from pathlib import Path

from .enrichment import NOT_AVAILABLE, NOT_FOUND
from .filtering import summarize
from .storage import Database


def _write(path: Path, header: list[str], rows: list[list]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return path


def export_all(db: Database, out_dir: str) -> list[Path]:
    out = Path(out_dir)
    files = []
    # A. full dataset (every discovered influencer, pass or fail, with reasons)
    rows = []
    for i in db.influencers():
        f = db.one("SELECT * FROM filter_results WHERE channel_id=?", (i["channel_id"],))
        e = db.one("SELECT * FROM enrichment WHERE channel_id=?", (i["channel_id"],)) or {}
        status = "not evaluated" if not f else ("Shortlisted" if f["passed"] else "Rejected")
        rows.append([i["name"], i["platform"], i["followers"] if i["followers"] is not None else "Hidden",
                     i["engagement_rate"] if i["engagement_rate"] is not None else "Not Available",
                     i["category"], e.get("email", "Not enriched (not shortlisted)"), i["url"],
                     ", ".join(i["themes"] or []) or "Not Available", i["country"] or "Unknown", status,
                     summarize(f["reasons"]) if f else ""])
    files.append(_write(out / "influencer_dataset.csv",
                        ["Name", "Platform", "Followers", "Engagement %", "Niche", "Email", "Profile URL",
                         "Content Themes", "Country", "Status", "Filter Result / Reasons"], rows))
    # B. enriched shortlist
    rows = []
    for i in db.shortlisted():
        e = db.one("SELECT * FROM enrichment WHERE channel_id=?", (i["channel_id"],)) or {}
        rows.append([i["name"], i["platform"], i["url"], i["followers"], i["engagement_rate"], i["category"],
                     ", ".join(i["themes"] or []), e.get("email", NOT_FOUND), e.get("email_source", ""),
                     e.get("instagram", NOT_FOUND), e.get("tiktok", NOT_FOUND), e.get("website", NOT_FOUND),
                     e.get("audience_age", NOT_AVAILABLE), e.get("audience_gender", NOT_AVAILABLE),
                     e.get("audience_geography", NOT_AVAILABLE)])
    files.append(_write(out / "shortlisted_enriched_profiles.csv",
                        ["Name", "Platform", "Profile URL", "Followers", "Engagement %", "Niche", "Content Themes",
                         "Contact Email", "Email Source", "Instagram", "TikTok", "Website", "Audience Age",
                         "Audience Gender", "Audience Geography"], rows))
    # C. messages
    msgs = db.fetch("SELECT m.*, i.name FROM messages m JOIN influencers i USING(channel_id)")
    files.append(_write(out / "personalized_messages.csv",
                        ["Influencer", "Email Subject", "Email Body", "Instagram DM", "Signals Used", "Model", "Review Status"],
                        [[m["name"], m["email_subject"], m["email_body"], m["instagram_dm"],
                          "; ".join(m["signals_used"] or []), m["model"], m["status"]] for m in msgs]))
    md = ["# Sample personalized messages\n"]
    for m in msgs:
        md += [f"## {m['name']}", f"**Subject:** {m['email_subject']}\n", m["email_body"], "",
               f"**Instagram DM:** {m['instagram_dm']}\n", "---"]
    p = out / "personalized_messages.md"
    p.write_text("\n".join(md), encoding="utf-8")
    files.append(p)
    # D. tracker
    rows = []
    for m in msgs:
        e = db.one("SELECT email FROM enrichment WHERE channel_id=?", (m["channel_id"],)) or {}
        for ch in ("email", "instagram_dm"):
            o = db.one("SELECT * FROM outreach WHERE channel_id=? AND channel=?", (m["channel_id"], ch))
            rows.append([m["name"], e.get("email", NOT_FOUND), ch, "Yes", m["status"],
                         "Yes" if o and o["status"] in ("sent", "dm_sent_manually") else "No",
                         o["updated_at"] if o else "", o["status"] if o else "not_attempted", o["detail"] if o else ""])
    files.append(_write(out / "outreach_tracker.csv",
                        ["Influencer", "Email", "Channel", "Message Generated", "Review Status", "Sent",
                         "Date", "Status", "Detail"], rows))
    return files
