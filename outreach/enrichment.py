"""Stage 3: enrich shortlisted influencers (contact email, links, audience fields).

Only publicly published information is used. Emails are *extracted*, never guessed or generated:
  1. the channel description (public text)
  2. websites / link-in-bio pages linked from the description (robots.txt respected)
If nothing is found the email is stored as "Not Found".
"""
from __future__ import annotations

import logging
import re
import urllib.robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .storage import Database, now_iso

log = logging.getLogger(__name__)
NOT_FOUND = "Not Found"
NOT_AVAILABLE = "Not Available"
UA = "MicroInfluencerOutreachBot/1.0 (research prototype; respects robots.txt)"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
BAD_EMAIL_DOMAINS = {"example.com", "domain.com", "email.com", "yourdomain.com", "sentry.io", "wixpress.com"}
BAD_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")
PREFERRED = ("business", "collab", "contact", "partner", "hello", "hi", "pr", "mgmt", "management", "brand", "sponsor")
SOCIAL_HOSTS = ("youtube.com", "youtu.be", "instagram.com", "tiktok.com", "facebook.com", "twitter.com", "x.com",
                "pinterest.com", "snapchat.com", "twitch.tv", "amazon.com", "amzn.to", "bit.ly", "shopmy.us")
LINK_HUBS = ("linktr.ee", "beacons.ai", "bio.link", "lnk.bio", "solo.to", "stan.store", "carrd.co", "campsite.bio")
IG_RE = re.compile(r"instagram\.com/(?!p/|reel/|explore|accounts)([A-Za-z0-9_.]{2,30})", re.I)
TT_RE = re.compile(r"tiktok\.com/@([A-Za-z0-9_.]{2,30})", re.I)


def extract_emails(text: str) -> list[str]:
    out = []
    for m in EMAIL_RE.findall(text or ""):
        e = m.strip(".").lower()
        dom = e.split("@")[1]
        if dom in BAD_EMAIL_DOMAINS or e.endswith(BAD_SUFFIX) or "/" in e:
            continue
        if e not in out:
            out.append(e)
    return out


def pick_best_email(emails: list[str]) -> str | None:
    for e in emails:
        if e.split("@")[0] in PREFERRED or any(e.startswith(p) for p in PREFERRED):
            return e
    return emails[0] if emails else None


def extract_urls(text: str) -> list[str]:
    return [u.rstrip(".,;:!?") for u in URL_RE.findall(text or "")]


def is_valid_email(e: str | None) -> bool:
    return bool(e) and e != NOT_FOUND and EMAIL_RE.fullmatch(e) is not None


class PoliteFetcher:
    def __init__(self, session: requests.Session | None = None, max_bytes: int = 1_000_000):
        self.s = session or requests.Session()
        self.s.headers["User-Agent"] = UA
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self.max_bytes = max_bytes

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.s.get(base + "/robots.txt", timeout=8)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except requests.RequestException:
                rp = None  # unreachable site -> will fail on fetch anyway
            self._robots[base] = rp
        rp = self._robots[base]
        return rp is None or rp.can_fetch(UA, url)

    def get(self, url: str) -> str | None:
        try:
            if not self.allowed(url):
                log.info("robots.txt disallows %s", url)
                return None
            r = self.s.get(url, timeout=10)
            if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
                return None
            return r.text[: self.max_bytes]
        except requests.RequestException as exc:
            log.debug("fetch failed %s: %s", url, exc)
            return None


def scan_html(html: str) -> tuple[list[str], str]:
    soup = BeautifulSoup(html, "html.parser")
    emails = [a["href"][7:].split("?")[0].strip().lower() for a in soup.find_all("a", href=True)
              if a["href"].lower().startswith("mailto:")]
    emails = [e for e in emails if EMAIL_RE.fullmatch(e)]
    text = soup.get_text(" ")
    emails += [e for e in extract_emails(text) if e not in emails]
    links = " ".join(a["href"] for a in soup.find_all("a", href=True))
    return emails, links + " " + text


def enrich_one(inf: dict, fetcher: PoliteFetcher, max_pages: int = 3) -> dict:
    desc = inf.get("description", "")
    email, source = None, None
    emails = extract_emails(desc)
    if emails:
        email, source = pick_best_email(emails), "channel_description"

    urls = extract_urls(desc)
    web = [u for u in urls if not any(h in urlparse(u).netloc.lower() for h in SOCIAL_HOSTS)]
    website = web[0] if web else None
    ig = (IG_RE.search(desc) or [None, None])[1]
    tt = (TT_RE.search(desc) or [None, None])[1]

    pages_left = max_pages
    for u in web[:2]:
        candidates = [u] if any(h in urlparse(u).netloc.lower() for h in LINK_HUBS) else [
            u, urljoin(u, "/contact"), urljoin(u, "/about")]
        for page in candidates:
            if pages_left <= 0 or (email and ig and tt):
                break
            html = fetcher.get(page)
            pages_left -= 1
            if not html:
                continue
            found, blob = scan_html(html)
            if not email and found:
                email, source = pick_best_email(found), f"website:{page}"
            ig = ig or (IG_RE.search(blob).group(1) if IG_RE.search(blob) else None)
            tt = tt or (TT_RE.search(blob).group(1) if TT_RE.search(blob) else None)

    return {
        "channel_id": inf["channel_id"],
        "email": email or NOT_FOUND, "email_source": source or "none found",
        "website": website or NOT_FOUND,
        "instagram": f"https://www.instagram.com/{ig}/" if ig else NOT_FOUND,
        "tiktok": f"https://www.tiktok.com/@{tt}" if tt else NOT_FOUND,
        # Public APIs do not expose audience demographics to third parties - never invented.
        "audience_age": NOT_AVAILABLE, "audience_gender": NOT_AVAILABLE,
        "audience_geography": inf.get("country") and f"Channel country: {inf['country']}" or NOT_AVAILABLE,
        "enriched_at": now_iso(),
    }


def run_enrichment(db: Database, fetcher: PoliteFetcher | None = None, force: bool = False) -> int:
    fetcher = fetcher or PoliteFetcher()
    done = {r["channel_id"] for r in db.fetch("SELECT channel_id FROM enrichment")}
    n = 0
    for inf in db.shortlisted():
        if inf["channel_id"] in done and not force:
            continue
        try:
            db.upsert("enrichment", enrich_one(inf, fetcher))
            n += 1
        except Exception as exc:  # never let one bad profile stop the batch
            log.warning("Enrichment failed for %s: %s", inf["name"], exc)
    return n
