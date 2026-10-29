"""Stage 1: discover candidate channels and collect real public metrics."""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from .storage import Database, now_iso
from .youtube import QuotaExceeded, YouTubeClient, YouTubeError

log = logging.getLogger(__name__)

STOPWORDS = set("""a an the and or of to in on for with my your our is are was be this that these those it its i you we
they he she at by from as how what why when who which vs video videos new full best top ep part official
2020 2021 2022 2023 2024 2025 2026 shorts short youtube vlog""".split())


def discover_channel_ids(yt: YouTubeClient, cfg: dict) -> dict[str, str]:
    """Return {channel_id: 'query | mode'} gathered via keyword searches."""
    d = cfg["discovery"]
    cat = cfg["categories"][d["category"]]
    found: dict[str, str] = {}
    after = (datetime.now(timezone.utc) - timedelta(days=d.get("video_search_days", 90))).strftime("%Y-%m-%dT%H:%M:%SZ")
    for mode in d["search_modes"]:
        for kw in cat["discovery_keywords"]:
            for region in d.get("region_codes") or [None]:
                token = None
                for _ in range(d.get("pages_per_query", 1)):
                    try:
                        data = yt.search(kw, "channel" if mode == "channel" else "video", region, token,
                                         published_after=after if mode == "video" else None)
                    except QuotaExceeded:
                        log.warning("Quota exhausted during discovery; continuing with %d candidates", len(found))
                        return found
                    except YouTubeError as exc:
                        log.warning("Search failed for %r (%s): %s", kw, mode, exc)
                        break
                    for it in data.get("items", []):
                        cid = it.get("snippet", {}).get("channelId") or it.get("id", {}).get("channelId")
                        if cid and cid not in found:
                            found[cid] = f"{kw} | {mode}"
                    token = data.get("nextPageToken")
                    if not token or len(found) >= d["max_candidates"]:
                        break
                if len(found) >= d["max_candidates"]:
                    return found
    return found


def parse_channel(item: dict, via: str, category: str) -> dict:
    sn, st = item.get("snippet", {}), item.get("statistics", {})
    custom = sn.get("customUrl", "")
    url = f"https://www.youtube.com/{custom}" if custom.startswith("@") else f"https://www.youtube.com/channel/{item['id']}"
    followers = None if st.get("hiddenSubscriberCount") or "subscriberCount" not in st else int(st["subscriberCount"])
    return {
        "channel_id": item["id"], "platform": "YouTube", "name": sn.get("title", ""), "url": url,
        "handle": custom or None, "country": sn.get("country"), "followers": followers,
        "video_count": int(st.get("videoCount", 0)), "description": sn.get("description", ""),
        "discovered_via": via, "category": category,
        "uploads_playlist": item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads"),
        "engagement_rate": None, "avg_views": None, "videos_sampled": 0, "last_upload": None,
        "recent_videos": [], "themes": [], "metrics_status": "not_collected", "collected_at": now_iso(),
    }


def compute_engagement(videos: list[dict]) -> tuple[float | None, float | None, int]:
    """Mean per-video (likes+comments)/views, in percent. Videos with hidden likes or 0 views are skipped.
    Returns (rate, avg_views, videos_used)."""
    rates, views = [], []
    for v in videos:
        if v.get("views", 0) > 0 and v.get("likes") is not None:
            rates.append((v["likes"] + (v.get("comments") or 0)) / v["views"] * 100)
            views.append(v["views"])
    if not rates:
        return None, None, 0
    return round(sum(rates) / len(rates), 2), round(sum(views) / len(views), 1), len(rates)


def extract_themes(videos: list[dict], keywords: list[str], top_n: int = 8) -> list[str]:
    text = " ".join(v["title"] + " " + " ".join(v.get("tags", [])) for v in videos).lower()
    matched = [(text.count(k.lower()), k) for k in keywords if re.search(rf"\b{re.escape(k.lower())}\b", text)]
    themes = [k for _, k in sorted(matched, reverse=True)]
    words = [w for w in re.findall(r"[a-z][a-z'-]{3,}", text) if w not in STOPWORDS]
    for w, _ in Counter(words).most_common(20):
        if len(themes) >= top_n:
            break
        if w not in themes:
            themes.append(w)
    return themes[:top_n]


def parse_video(item: dict) -> dict:
    st, sn = item.get("statistics", {}), item.get("snippet", {})
    return {
        "id": item["id"], "title": sn.get("title", ""), "published": sn.get("publishedAt"),
        "tags": sn.get("tags", [])[:15], "views": int(st.get("viewCount", 0)),
        "likes": int(st["likeCount"]) if "likeCount" in st else None,
        "comments": int(st["commentCount"]) if "commentCount" in st else None,
    }


def collect_metrics(yt: YouTubeClient, ch: dict, cfg: dict) -> dict:
    """Fetch recent uploads for channels in follower range (saves API quota on the rest)."""
    f = cfg["filters"]
    if ch["followers"] is None:
        ch["metrics_status"] = "skipped: subscriber count hidden"
        return ch
    if not (f["min_followers"] <= ch["followers"] <= f["max_followers"]):
        ch["metrics_status"] = "skipped: outside follower range"
        return ch
    if not ch["uploads_playlist"]:
        ch["metrics_status"] = "skipped: no uploads playlist"
        return ch
    try:
        ids = yt.recent_video_ids(ch["uploads_playlist"], cfg["discovery"]["recent_videos_sampled"])
        vids = [parse_video(v) for v in yt.videos(ids)]
    except QuotaExceeded:
        raise
    except YouTubeError as exc:
        ch["metrics_status"] = f"error: {exc}"[:200]
        return ch
    vids.sort(key=lambda v: v["published"] or "", reverse=True)
    rate, avg_views, used = compute_engagement(vids)
    ch.update(engagement_rate=rate, avg_views=avg_views, videos_sampled=used,
              last_upload=vids[0]["published"] if vids else None,
              recent_videos=[{"title": v["title"], "published": v["published"], "views": v["views"], "tags": v["tags"]}
                             for v in vids],
              themes=extract_themes(vids, cfg["categories"][cfg["discovery"]["category"]]["keywords"]),
              metrics_status="ok" if rate is not None else "no engagement data (likes hidden or no views)")
    return ch


def run_discovery(yt: YouTubeClient, db: Database, cfg: dict) -> int:
    category = cfg["discovery"]["category"]
    ids = discover_channel_ids(yt, cfg)
    known = {r["channel_id"] for r in db.influencers()}
    new_ids = [i for i in ids if i not in known]
    log.info("Found %d candidate channels (%d new)", len(ids), len(new_ids))
    count = 0
    try:
        for item in yt.channels(new_ids):
            ch = parse_channel(item, ids[item["id"]], category)
            ch = collect_metrics(yt, ch, cfg)
            db.upsert("influencers", ch)
            count += 1
    except QuotaExceeded:
        log.warning("Quota exhausted while collecting metrics; %d channels saved. Re-run tomorrow to resume.", count)
    return count
