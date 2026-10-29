"""Unit + integration tests. Fixtures here are SYNTHETIC TEST DATA used only to exercise code paths;
they are never written to the deliverable dataset."""
import json
from datetime import datetime, timezone

import pytest

from outreach.config import load_config
from outreach.discovery import compute_engagement, extract_themes, parse_channel, collect_metrics
from outreach.enrichment import enrich_one, extract_emails, is_valid_email, pick_best_email, NOT_FOUND
from outreach.export import export_all
from outreach.filtering import evaluate, run_filtering
from outreach.personalization import generate_for, run_personalization, validate, word_count
from outreach.sending import send_emails, queue_dms
from outreach.storage import Database

CFG = load_config()
CAT = CFG["categories"]["fashion_beauty"]
NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)


def inf(**kw):
    base = dict(channel_id="C1", platform="YouTube", name="Test Chan", url="u", followers=20000,
                engagement_rate=4.0, videos_sampled=10, metrics_status="ok", country="US",
                description="skincare and makeup reviews", last_upload="2026-10-20T00:00:00Z",
                recent_videos=[{"title": f"skincare routine {i}", "tags": ["makeup", "beauty"]} for i in range(6)],
                themes=["skincare"], category="fashion_beauty")
    base.update(kw)
    return base


# ---------- filtering
def test_passes_when_all_criteria_met():
    ok, checks = evaluate(inf(), CAT, CFG["filters"], NOW)
    assert ok and all(c["passed"] for c in checks)


@pytest.mark.parametrize("override,failed", [
    ({"followers": 500000}, "follower_range"), ({"followers": None}, "follower_range"),
    ({"engagement_rate": 0.4}, "engagement_rate"), ({"engagement_rate": None}, "engagement_rate"),
    ({"last_upload": "2025-01-01T00:00:00Z"}, "activity"),
    ({"description": "gambling tips"}, "brand_safety"),
    ({"name": "x", "description": "cooking", "recent_videos": [{"title": "pasta", "tags": []}] * 6}, "niche_relevance"),
])
def test_each_failure_is_reported(override, failed):
    ok, checks = evaluate(inf(**override), CAT, CFG["filters"], NOW)
    assert not ok
    assert failed in [c["check"] for c in checks if not c["passed"]]


def test_geography_restriction():
    f = dict(CFG["filters"], allowed_countries=["IN"])
    assert not evaluate(inf(country="US"), CAT, f, NOW)[0]
    assert evaluate(inf(country=None), CAT, f, NOW)[0]
    assert not evaluate(inf(country=None), CAT, dict(f, require_known_country=True), NOW)[0]


# ---------- metrics
def test_engagement_math_and_hidden_likes():
    rate, avg, n = compute_engagement([{"views": 1000, "likes": 50, "comments": 10},
                                       {"views": 2000, "likes": 40, "comments": 0},
                                       {"views": 500, "likes": None, "comments": 5},
                                       {"views": 0, "likes": 1, "comments": 0}])
    assert n == 2 and avg == 1500 and rate == round((6.0 + 2.0) / 2, 2)
    assert compute_engagement([]) == (None, None, 0)


def test_themes_prefer_niche_keywords():
    t = extract_themes([{"title": "Skincare routine for dry skin", "tags": ["makeup"]}], CAT["keywords"])
    assert t[0] in ("skincare", "makeup") and "skincare" in t


# ---------- enrichment
def test_email_extraction_rejects_junk():
    assert extract_emails("mail Business@Foo.com or a@example.com or logo@2x.png") == ["business@foo.com"]
    assert pick_best_email(["zed@x.com", "collab@y.com"]) == "collab@y.com"
    assert not is_valid_email(NOT_FOUND) and not is_valid_email("bad@") and is_valid_email("a@b.co")


class NoNet:
    def get(self, url):
        return None


def test_enrichment_not_found_is_never_guessed():
    r = enrich_one(inf(description="no contact here"), NoNet())
    assert r["email"] == NOT_FOUND and r["audience_age"] == "Not Available"


def test_enrichment_from_description_and_links():
    r = enrich_one(inf(description="biz: collab@brandmail.com ig instagram.com/glow_with_me tiktok.com/@glow.tt"), NoNet())
    assert r["email"] == "collab@brandmail.com" and r["email_source"] == "channel_description"
    assert r["instagram"].endswith("/glow_with_me/") and "glow.tt" in r["tiktok"]


class SiteFetcher:
    def get(self, url):
        return '<a href="mailto:Hello@Creator.com">mail</a>' if url.endswith("/contact") else "<p>hi</p>"


def test_enrichment_from_website_contact_page():
    r = enrich_one(inf(description="site https://creator.com"), SiteFetcher())
    assert r["email"] == "hello@creator.com" and r["email_source"].startswith("website:")


# ---------- personalization
class StubLLM:
    model = "stub"

    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0

    def generate(self, system, user):
        self.calls += 1
        return self.replies.pop(0)


GOOD = {"email_subject": "UGC idea for your skincare videos",
        "email_body": ("Hi Test, your skincare routine videos are clear and calm, which suits our brand well. "
                       "YourBrand makes dermatologist tested products, and we would love to collaborate with you on "
                       "paid UGC content. We would send a free kit plus a creator fee, and you would keep full creative "
                       "control over how the products appear in your videos. Would you be open to hearing "
                       "the details this week? Best, Your Name, Partnerships at YourBrand"),
        "instagram_dm": "Hi Test, loved your skincare routine videos. Your audience fits our upcoming paid UGC campaign. Interested?",
        "signals_used": ["themes: skincare"]}


def test_word_limits_enforced():
    assert validate(GOOD) == []
    assert any("email_body" in p for p in validate(dict(GOOD, email_body="too short")))
    assert any("instagram_dm" in p for p in validate(dict(GOOD, instagram_dm="hi " * 40)))
    assert 60 <= word_count(GOOD["email_body"]) <= 90


def test_regenerates_after_invalid_then_succeeds():
    llm = StubLLM(["not json", json.dumps(dict(GOOD, email_body="short")), "```json\n" + json.dumps(GOOD) + "\n```"])
    assert generate_for(inf(), None, CFG["campaign"], llm)["email_subject"] == GOOD["email_subject"]
    assert llm.calls == 3


def test_gives_up_instead_of_returning_invalid():
    with pytest.raises(ValueError):
        generate_for(inf(), None, CFG["campaign"], StubLLM(["nope"] * 3))


# ---------- end to end (stub YouTube + stub LLM + dry-run sending)
class StubYT:
    def recent_video_ids(self, pl, n):
        return ["v1", "v2", "v3", "v4", "v5", "v6"]

    def videos(self, ids):
        return [{"id": i, "snippet": {"title": f"skincare routine makeup beauty {i}", "publishedAt": f"2026-10-{20 - k:02d}T00:00:00Z",
                                       "tags": ["skincare"]},
                 "statistics": {"viewCount": "1000", "likeCount": "60", "commentCount": "10"}} for k, i in enumerate(ids)]


def make_channel(cid, subs, desc):
    return {"id": cid, "snippet": {"title": f"Chan {cid}", "description": desc, "country": "US", "customUrl": f"@{cid}"},
            "statistics": {"subscriberCount": str(subs), "videoCount": "50"},
            "contentDetails": {"relatedPlaylists": {"uploads": "UU" + cid}}}


@pytest.fixture
def db():
    return Database(":memory:")


def test_end_to_end(db, tmp_path, monkeypatch):
    import outreach.filtering as flt
    monkeypatch.setattr(flt, "datetime", type("D", (datetime,), {"now": classmethod(lambda c, tz=None: NOW)}))
    cfg = dict(CFG)
    chans = [make_channel("A", 30000, "contact: collab@a-brand.com instagram.com/a_ig"),
             make_channel("B", 40000, "no email here"),
             make_channel("BIG", 9_000_000, "skincare makeup beauty")]
    for ch in chans:
        row = collect_metrics(StubYT(), parse_channel(ch, "q | channel", "fashion_beauty"), cfg)
        db.upsert("influencers", row)
    big = db.one("SELECT * FROM influencers WHERE channel_id='BIG'")
    assert big["metrics_status"].startswith("skipped") and big["engagement_rate"] is None  # quota saved, no fake metric

    p, f = run_filtering(db, cfg)
    assert (p, f) == (2, 1)
    from outreach.enrichment import run_enrichment
    run_enrichment(db, NoNet())
    assert db.one("SELECT email FROM enrichment WHERE channel_id='A'")["email"] == "collab@a-brand.com"
    assert db.one("SELECT email FROM enrichment WHERE channel_id='B'")["email"] == NOT_FOUND

    ok, bad = run_personalization(db, cfg, StubLLM([json.dumps(GOOD)] * 2))
    assert (ok, bad) == (2, 0)
    assert send_emails(db, cfg, live=False)["simulated"] == 0       # drafts are not sent: review gate
    db.conn.execute("UPDATE messages SET status='approved'"); db.conn.commit()

    stats = send_emails(db, cfg, live=False)
    assert stats["simulated"] == 1 and stats["skipped"] == 1        # B has no email

    class FakeMailer:
        sent = []
        def send(self, to, subj, body): self.sent.append((to, body))
    m = FakeMailer()
    assert send_emails(db, cfg, m, live=True, sleep=lambda s: None)["sent"] == 1
    assert "no thanks" in m.sent[0][1]                              # opt-out footer appended
    again = send_emails(db, cfg, m, live=True, sleep=lambda s: None)  # duplicate prevention
    assert again["sent"] == 0 and len(m.sent) == 1

    db.suppress("collab@a-brand.com")
    assert queue_dms(db) == 1                                       # only A has an Instagram handle
    files = export_all(db, str(tmp_path))
    assert all(p.exists() for p in files)
    ds = (tmp_path / "influencer_dataset.csv").read_text(encoding="utf-8-sig")
    assert "Rejected" in ds and "follower_range" in ds and "Shortlisted" in ds
