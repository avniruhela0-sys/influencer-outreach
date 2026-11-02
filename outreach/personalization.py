"""Stage 4: LLM-based personalization of the email pitch and Instagram DM.

Every message is generated per influencer from real profile signals. Output is validated against the
brief's length limits (email 60-90 words, DM 15-30 words) and regenerated with feedback if it fails.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Protocol

from .enrichment import NOT_FOUND, is_valid_email
from .storage import Database, now_iso

log = logging.getLogger(__name__)
EMAIL_RANGE, DM_RANGE = (60, 90), (15, 30)

SYSTEM_PROMPT = """You write brand-to-creator outreach for a partnerships team. You write like a thoughtful human, not a template.

HARD RULES
1. Use ONLY the facts provided in the creator profile. Never invent follower counts, video details, products, prior contact, or praise for content not listed.
2. You may refer to a recent video only by what its title says. Do not claim you watched details beyond the title.
3. EMAIL BODY: between 70 and 85 words INCLUDING greeting and sign-off. Structure: greeting using the creator's first name (if the channel name is clearly a personal name; otherwise use the channel name), one specific personalization hook (niche, tone, or a recent video title), the brand and a concrete collaboration proposal with the value offered, one low-friction call to action. Sign off with the sender name and role.
4. INSTAGRAM DM: between 18 and 27 words, casual and natural, one specific reference to their content/niche, mention the collaboration idea, no links, at most one emoji.
5. Avoid cliches ("I hope this finds you well", "reaching out", "amazing content", "huge fan") and avoid ALL-CAPS or hype.
6. Vary phrasing; do not reuse a stock opener.
7. Subject line: under 9 words, specific, not clickbait.
8. Do not mention opt-out text; it is appended automatically.

Return ONLY a JSON object, no markdown fences:
{"email_subject": str, "email_body": str, "instagram_dm": str, "signals_used": [str, ...]}
signals_used lists which profile facts you used (e.g. "recent video: ...", "themes: skincare").
"""


class LLM(Protocol):
    model: str

    def generate(self, system: str, user: str) -> str: ...


class AnthropicLLM:
    def __init__(self, model: str | None = None):
        import anthropic  # imported lazily so tests/other stages work without it

        self.model = model or os.environ.get("LLM_MODEL", "claude-sonnet-5-5")
        self.client = anthropic.Anthropic()

    def generate(self, system: str, user: str) -> str:
        resp = self.client.messages.create(model=self.model, max_tokens=700, temperature=0.8, system=system,
                                           messages=[{"role": "user", "content": user}])
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


class HTTPLLM:
    """Free-tier friendly providers over plain REST (no extra dependency): Gemini and Groq."""

    def __init__(self, provider: str, api_key: str, model: str | None = None, session=None):
        import requests
        self.provider, self.key = provider, api_key
        self.session = session or requests.Session()
        defaults = {"gemini": "gemini-2.5-flash", "groq": "llama-3.3-70b-versatile"}
        self.model = model or defaults[provider]

    def generate(self, system: str, user: str) -> str:
        import time
        for attempt in range(4):
            if self.provider == "gemini":
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
                r = self.session.post(url, params={"key": self.key}, timeout=60, json={
                    "systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": user}]}],
                    "generationConfig": {"temperature": 0.8, "responseMimeType": "application/json"}})
            else:  # groq (OpenAI-compatible)
                r = self.session.post("https://api.groq.com/openai/v1/chat/completions", timeout=60,
                                      headers={"Authorization": f"Bearer {self.key}"},
                                      json={"model": self.model, "temperature": 0.8,
                                            "response_format": {"type": "json_object"},
                                            "messages": [{"role": "system", "content": system},
                                                         {"role": "user", "content": user}]})
            if r.status_code == 429 or r.status_code >= 500:  # free tiers rate-limit: back off and retry
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise RuntimeError(f"{self.provider} HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
            if self.provider == "gemini":
                return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
            return data["choices"][0]["message"]["content"]
        raise RuntimeError(f"{self.provider}: rate limited after retries")


def get_llm() -> "LLM":
    """Choose provider via LLM_PROVIDER = gemini | groq | anthropic (default: whichever key is set)."""
    env = os.environ.get
    provider = (env("LLM_PROVIDER") or "").lower()
    if not provider:
        provider = next((p for p, k in (("gemini", "GEMINI_API_KEY"), ("groq", "GROQ_API_KEY"),
                                        ("anthropic", "ANTHROPIC_API_KEY")) if env(k)), "")
    if provider == "anthropic":
        return AnthropicLLM(env("LLM_MODEL"))
    if provider in ("gemini", "groq"):
        key = env(f"{provider.upper()}_API_KEY")
        if not key:
            raise SystemExit(f"Set {provider.upper()}_API_KEY in .env")
        return HTTPLLM(provider, key, env("LLM_MODEL"))
    raise SystemExit("No LLM key found. Set GEMINI_API_KEY (free) or GROQ_API_KEY (free) or ANTHROPIC_API_KEY in .env")


def word_count(text: str) -> int:
    """Conservative count: the stricter of whitespace-split and regex-word counts must be in range."""
    return max(len(text.split()), len(re.findall(r"[\w'’]+", text)))


def _min_word_count(text: str) -> int:
    return min(len(text.split()), len(re.findall(r"[\w'’]+", text)))


def validate(msg: dict) -> list[str]:
    problems = []
    for key in ("email_subject", "email_body", "instagram_dm"):
        if not isinstance(msg.get(key), str) or not msg[key].strip():
            problems.append(f"missing field {key}")
    if problems:
        return problems
    lo, hi = EMAIL_RANGE
    if not (lo <= _min_word_count(msg["email_body"]) and word_count(msg["email_body"]) <= hi):
        problems.append(f"email_body is {word_count(msg['email_body'])} words; must be {lo}-{hi}")
    lo, hi = DM_RANGE
    if not (lo <= _min_word_count(msg["instagram_dm"]) and word_count(msg["instagram_dm"]) <= hi):
        problems.append(f"instagram_dm is {word_count(msg['instagram_dm'])} words; must be {lo}-{hi}")
    if re.search(r"https?://", msg["instagram_dm"]):
        problems.append("instagram_dm must not contain links")
    return problems


def parse_json(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    start, end = raw.find("{"), raw.rfind("}")
    return json.loads(raw[start:end + 1])


def build_profile_prompt(inf: dict, enr: dict | None, campaign: dict) -> str:
    vids = [v["title"] for v in (inf.get("recent_videos") or [])[:4]]
    profile = {
        "channel_name": inf["name"], "platform": inf["platform"], "followers": inf["followers"],
        "engagement_rate_pct": inf["engagement_rate"], "country": inf.get("country") or "unknown",
        "content_themes": inf.get("themes"), "recent_video_titles": vids,
        "about_text_excerpt": re.sub(r"\s+", " ", inf.get("description", ""))[:350],
    }
    return (f"CAMPAIGN\nBrand: {campaign['brand_name']} - {campaign['brand_description']}\n"
            f"Collaboration type: {campaign['collaboration_type']}\nOffer: {campaign['offer']}\n"
            f"Sender: {campaign['sender_name']}, {campaign['sender_role']}\n\n"
            f"CREATOR PROFILE (only facts you may use)\n{json.dumps(profile, ensure_ascii=False, indent=2)}")


def generate_for(inf: dict, enr: dict | None, campaign: dict, llm: LLM, max_attempts: int = 3) -> dict:
    prompt = build_profile_prompt(inf, enr, campaign)
    feedback = ""
    for attempt in range(1, max_attempts + 1):
        raw = llm.generate(SYSTEM_PROMPT, prompt + feedback)
        try:
            msg = parse_json(raw)
        except (ValueError, json.JSONDecodeError):
            feedback = "\n\nYour previous reply was not valid JSON. Return ONLY the JSON object."
            continue
        problems = validate(msg)
        if not problems:
            return msg
        log.info("%s attempt %d rejected: %s", inf["name"], attempt, problems)
        feedback = "\n\nYour previous attempt was rejected: " + "; ".join(problems) + ". Rewrite fixing this."
    raise ValueError(f"no valid message after {max_attempts} attempts")


def run_personalization(db: Database, cfg: dict, llm: LLM, limit: int | None = None, force: bool = False) -> tuple[int, int]:
    done = {r["channel_id"] for r in db.fetch("SELECT channel_id FROM messages")}
    ok = bad = 0
    for inf in db.shortlisted():
        if limit is not None and ok >= limit:
            break
        if inf["channel_id"] in done and not force:
            continue
        enr = db.one("SELECT * FROM enrichment WHERE channel_id=?", (inf["channel_id"],))
        try:
            msg = generate_for(inf, enr, cfg["campaign"], llm)
        except Exception as exc:
            log.warning("Personalization failed for %s: %s", inf["name"], exc)
            bad += 1
            continue
        db.upsert("messages", {"channel_id": inf["channel_id"], "email_subject": msg["email_subject"].strip(),
                               "email_body": msg["email_body"].strip(), "instagram_dm": msg["instagram_dm"].strip(),
                               "signals_used": msg.get("signals_used", []), "model": llm.model,
                               "status": "draft", "generated_at": now_iso()})
        ok += 1
    return ok, bad