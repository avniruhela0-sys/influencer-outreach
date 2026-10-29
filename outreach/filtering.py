"""Stage 2: rule-based filtering/classification with explicit pass/fail reasons."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .storage import Database, now_iso


def _corpus(inf: dict) -> str:
    parts = [inf.get("name", ""), inf.get("description", "")]
    for v in inf.get("recent_videos") or []:
        parts.append(v["title"])
        parts.extend(v.get("tags", []))
    return " ".join(parts).lower()


def _hits(text: str, words: list[str]) -> list[str]:
    return [w for w in words if re.search(rf"(?<![\w]){re.escape(w.lower())}(?![\w])", text)]


def evaluate(inf: dict, cat: dict, f: dict, now: datetime | None = None) -> tuple[bool, list[dict]]:
    now = now or datetime.now(timezone.utc)
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str):
        checks.append({"check": name, "passed": ok, "detail": detail})

    fol = inf.get("followers")
    if fol is None:
        add("follower_range", False, "subscriber count hidden/unavailable")
    else:
        add("follower_range", f["min_followers"] <= fol <= f["max_followers"],
            f"{fol:,} followers (allowed {f['min_followers']:,}-{f['max_followers']:,})")

    er = inf.get("engagement_rate")
    if er is None:
        add("engagement_rate", False, f"not available ({inf.get('metrics_status')})")
    else:
        add("engagement_rate", er >= f["min_engagement_rate"],
            f"{er}% over {inf.get('videos_sampled')} videos (min {f['min_engagement_rate']}%)")

    vids = inf.get("recent_videos") or []
    if len(vids) < f["min_recent_videos"]:
        add("activity", False, f"only {len(vids)} recent videos found (min {f['min_recent_videos']})")
    else:
        last = datetime.fromisoformat(inf["last_upload"].replace("Z", "+00:00"))
        days = (now - last).days
        add("activity", days <= f["max_days_since_last_upload"],
            f"last upload {days} days ago (max {f['max_days_since_last_upload']})")

    text = _corpus(inf)
    matched = _hits(text, cat["keywords"])
    add("niche_relevance", len(matched) >= f["min_keyword_matches"],
        f"{len(matched)} niche keywords matched {matched[:6]} (min {f['min_keyword_matches']})")

    bad = _hits(text, cat.get("blocklist", []))
    add("brand_safety", not bad, "no blocklisted terms" if not bad else f"blocklisted terms found: {bad}")

    country = inf.get("country")
    allowed = f.get("allowed_countries") or []
    if country is None:
        add("geography", not f.get("require_known_country", False), "country not set on channel (unknown)")
    elif allowed:
        add("geography", country in allowed, f"{country} (allowed {allowed})")
    else:
        add("geography", True, f"{country} (no country restriction)")

    return all(c["passed"] for c in checks), checks


def run_filtering(db: Database, cfg: dict) -> tuple[int, int]:
    cat = cfg["categories"][cfg["discovery"]["category"]]
    passed = failed = 0
    for inf in db.influencers():
        ok, checks = evaluate(inf, cat, cfg["filters"])
        db.upsert("filter_results", {"channel_id": inf["channel_id"], "passed": int(ok),
                                     "reasons": checks, "evaluated_at": now_iso()})
        passed += ok
        failed += not ok
    return passed, failed


def summarize(checks: list[dict]) -> str:
    failing = [f"{c['check']}: {c['detail']}" for c in checks if not c["passed"]]
    return "PASS - all criteria met" if not failing else "FAIL - " + "; ".join(failing)
