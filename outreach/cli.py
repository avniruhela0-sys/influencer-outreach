"""Command-line interface. Dry-run is the default for sending."""
from __future__ import annotations

import argparse
import logging

from .config import load_config, require_env
from .storage import Database


def _yt(cfg):
    from .youtube import YouTubeClient
    return YouTubeClient(require_env("YOUTUBE_API_KEY"))


def _llm():
    from .personalization import AnthropicLLM
    require_env("ANTHROPIC_API_KEY")
    return AnthropicLLM()


def cmd_discover(a, cfg, db):
    from .discovery import run_discovery
    print(f"Saved {run_discovery(_yt(cfg), db, cfg)} new channels. Total: {len(db.influencers())}")


def cmd_filter(a, cfg, db):
    from .filtering import run_filtering
    p, f = run_filtering(db, cfg)
    print(f"Shortlisted: {p}  Rejected: {f}")


def cmd_enrich(a, cfg, db):
    from .enrichment import run_enrichment
    print(f"Enriched {run_enrichment(db, force=a.force)} shortlisted profiles")


def cmd_personalize(a, cfg, db):
    from .personalization import run_personalization
    ok, bad = run_personalization(db, cfg, _llm(), a.limit, a.force)
    print(f"Generated {ok} message sets ({bad} failed). Status=draft; review before sending.")


def cmd_review(a, cfg, db):
    if a.approve_all:
        db.conn.execute("UPDATE messages SET status='approved' WHERE status='draft'")
        db.conn.commit()
    for cid in a.approve or []:
        db.conn.execute("UPDATE messages SET status='approved' WHERE channel_id=?", (cid,))
    for cid in a.reject or []:
        db.conn.execute("UPDATE messages SET status='rejected' WHERE channel_id=?", (cid,))
    db.conn.commit()
    for m in db.fetch("SELECT m.*, i.name FROM messages m JOIN influencers i USING(channel_id)"):
        if a.show:
            print(f"\n[{m['status']}] {m['name']} ({m['channel_id']})\nSubject: {m['email_subject']}\n"
                  f"{m['email_body']}\nDM: {m['instagram_dm']}")
        else:
            print(f"{m['status']:9} {m['channel_id']}  {m['name']}")


def cmd_send(a, cfg, db):
    from .sending import SMTPMailer, queue_dms, send_emails
    mailer = SMTPMailer() if a.live else None
    stats = send_emails(db, cfg, mailer, live=a.live, limit=a.limit)
    print(("LIVE" if a.live else "DRY-RUN"), stats, "| DMs queued for manual sending:", queue_dms(db))


def cmd_mark_dm(a, cfg, db):
    for cid in a.channel_ids:
        db.record_outreach(cid, "instagram_dm", "", "dm_sent_manually", "marked sent by operator")


def cmd_suppress(a, cfg, db):
    db.suppress(a.email, "opt-out")
    print("Suppressed", a.email)


def cmd_export(a, cfg, db):
    from .export import export_all
    for f in export_all(db, cfg["export_dir"]):
        print("wrote", f)


def cmd_stats(a, cfg, db):
    for label, sql in [("discovered", "SELECT COUNT(*) c FROM influencers"),
                       ("shortlisted", "SELECT COUNT(*) c FROM filter_results WHERE passed=1"),
                       ("with email", "SELECT COUNT(*) c FROM enrichment WHERE email!='Not Found'"),
                       ("messages", "SELECT COUNT(*) c FROM messages"),
                       ("approved", "SELECT COUNT(*) c FROM messages WHERE status='approved'")]:
        print(f"{label:12}", db.one(sql)["c"])


def cmd_run(a, cfg, db):
    a.limit, a.force = None, False
    cmd_discover(a, cfg, db)
    cmd_filter(a, cfg, db)
    cmd_enrich(a, cfg, db)
    cmd_personalize(a, cfg, db)
    print("Next: `python -m outreach review --show`, then `review --approve-all`, then `send`.")


def main(argv=None):
    p = argparse.ArgumentParser(prog="outreach", description=__doc__)
    p.add_argument("--config")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("discover").set_defaults(fn=cmd_discover)
    sub.add_parser("filter").set_defaults(fn=cmd_filter)
    s = sub.add_parser("enrich"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_enrich)
    s = sub.add_parser("personalize"); s.add_argument("--limit", type=int); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_personalize)
    s = sub.add_parser("review")
    s.add_argument("--show", action="store_true"); s.add_argument("--approve-all", action="store_true")
    s.add_argument("--approve", nargs="*"); s.add_argument("--reject", nargs="*"); s.set_defaults(fn=cmd_review)
    s = sub.add_parser("send"); s.add_argument("--live", action="store_true"); s.add_argument("--limit", type=int); s.set_defaults(fn=cmd_send)
    s = sub.add_parser("mark-dm"); s.add_argument("channel_ids", nargs="+"); s.set_defaults(fn=cmd_mark_dm)
    s = sub.add_parser("suppress"); s.add_argument("email"); s.set_defaults(fn=cmd_suppress)
    sub.add_parser("export").set_defaults(fn=cmd_export)
    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    sub.add_parser("run", help="discover -> filter -> enrich -> personalize").set_defaults(fn=cmd_run)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(levelname)s %(message)s")
    cfg = load_config(a.config)
    a.fn(a, cfg, Database(cfg["db_path"]))
