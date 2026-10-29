# Automated Micro-Influencer Outreach System

End-to-end pipeline: **Discovery → Data Collection → Filtering → Enrichment → AI Personalization → Review → Sending → Tracking**.
Default niche: **Fashion & Beauty** on **YouTube** (fully configurable in `config.yaml`).

> Data integrity: every influencer, metric and email comes from live public sources. Nothing is generated or guessed.
> Missing data is written as `Not Found` / `Not Available` / `Hidden`. The unit-test fixtures are synthetic and are never written to the dataset.

## Architecture
```
YouTube Data API ─► discovery.py ─► SQLite (data/outreach.db)
                                        │
          filtering.py (pass/fail + reasons per check)
                                        │ shortlisted only
          enrichment.py (email / links / audience fields)
                                        │
          personalization.py (Claude → email + DM, validated)
                                        │
          review (human approve/reject)  ◄── gate: only "approved" is ever sent
                                        │
          sending.py: SMTP email (dry-run default) + manual Instagram DM queue
                                        │
          export.py → CSV deliverables / outreach tracker
```

## Tech stack / tools
Python 3.10+, `requests`, `BeautifulSoup4`, `PyYAML`, SQLite, **YouTube Data API v3**, **Anthropic API (Claude)**, SMTP (Gmail app password), pytest, GitHub Actions.

## Setup
```bash
git clone <your-repo-url> && cd influencer-outreach
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in YOUTUBE_API_KEY and ANTHROPIC_API_KEY (SMTP only for live sending)
# edit config.yaml -> campaign: (brand, offer, sender)
pytest -q                     # 19 tests
```
YouTube key: Google Cloud Console → enable *YouTube Data API v3* → Credentials → API key (free quota 10,000 units/day).

## Run
```bash
python -m outreach run                    # discover → filter → enrich → personalize (drafts)
python -m outreach stats
python -m outreach review --show          # read every draft
python -m outreach review --approve-all   # or --approve ID... / --reject ID...
python -m outreach send                   # DRY RUN: records "simulated", nothing leaves your machine
python -m outreach send --live --limit 5  # real SMTP sending (needs SMTP_* env vars)
python -m outreach mark-dm CHANNEL_ID     # after you manually send an Instagram DM
python -m outreach suppress someone@x.com # opt-out list
python -m outreach export                 # writes CSVs to data/exports/
```
Each stage is also runnable alone (`discover`, `filter`, `enrich`, `personalize`) and is resumable/idempotent.

## Discovery methodology
For each keyword in `categories.<niche>.discovery_keywords` the system runs YouTube **channel search** and **recent-video search** (last 90 days; video search surfaces smaller, active creators that channel search buries). Channel IDs are de-duplicated, then `channels.list` returns subscriber count, country, description. For channels inside the 5k–100k range, the last 10 uploads are fetched (`playlistItems` + `videos`) to compute real engagement. Out-of-range channels are still stored (and reported as rejected with the reason) but skip the video-metric calls to save quota.

## Filtering logic (`filtering.py`)
Every channel gets a per-check verdict stored in the DB and exported:
| Check | Rule (configurable) |
|---|---|
| follower_range | 5,000–100,000; hidden count = fail |
| engagement_rate | mean of (likes+comments)/views over last ≤10 videos ≥ 2.0% (videos with hidden likes/0 views excluded) |
| activity | ≥5 recent videos, last upload ≤60 days ago |
| niche_relevance | ≥3 distinct niche keywords in channel name/description/recent titles/tags |
| brand_safety | no blocklisted terms (gambling, adult, …) |
| geography | optional allow-list; unknown country passes unless `require_known_country` |

New niches: add a block under `categories:` and change `discovery.category`.

## Enrichment process
Only shortlisted profiles. Email is **extracted, never generated**: (1) public channel description, (2) websites/link-in-bio pages linked from it (`/contact`, `/about`; `robots.txt` respected, max 3 pages, identifying User-Agent). Also extracts Instagram/TikTok handles and website. Audience age/gender are `Not Available` (not exposed by public APIs); geography = channel country when set. YouTube's "View email address" button is behind a CAPTCHA and is deliberately **not** bypassed, so expect many `Not Found` emails.

## AI model & prompt
Claude (`LLM_MODEL`, default `claude-sonnet-5-5`) with the system prompt in `personalization.py`. Input = campaign config + a fact sheet per creator (name, followers, engagement, themes, recent video titles, about-text excerpt). The model must use only those facts and return JSON `{email_subject, email_body, instagram_dm, signals_used}`.

## Personalization logic
Messages are generated per influencer (no fixed template). Programmatic validation enforces email 60–90 words (strict of two counting methods), DM 15–30 words, no links in DM; on failure the model is re-prompted with the exact problem (3 attempts), otherwise the influencer is marked failed rather than given a bad message. `signals_used` is stored for the reviewer to audit.

## Sending mechanism
* **Email:** SMTP. Selects shortlisted + `approved` + valid email + not suppressed + not already `sent`; appends opt-out line; logs status; per-run cap and delay. **Dry-run by default**.
* **Duplicate prevention:** `outreach` table keyed on (influencer, channel); `sent` rows are never re-sent.
* **Instagram DM:** not auto-sent (Meta APIs don't permit cold DMs; no bypassing). Approved DMs go to a manual queue (`manual_pending`) with the profile link; mark done with `mark-dm`.
* **Log/tracker:** `outreach` table → `data/exports/outreach_tracker.csv`.

## Deliverables (generated by `python -m outreach export`)
`influencer_dataset.csv` (all discovered, with pass/fail reasons) · `shortlisted_enriched_profiles.csv` · `personalized_messages.csv/.md` · `outreach_tracker.csv`

## Scalability
Batch API calls (50 ids/request), resumable state in SQLite (swap for Postgres by replacing `storage.py`), quota-aware early exit, idempotent stages. To reach 500+: raise `max_candidates`/`pages_per_query`, add keywords/regions; quota is the limiter (~102 units per 50 search results).

## Limitations
* YouTube only (Instagram/TikTok scraping violates their ToS; they appear as enrichment links only).
* Many creators publish no email → `Not Found`. Audience demographics unavailable.
* Engagement uses recent public likes/comments; channels hiding likes can't be scored.
* Niche relevance is keyword-based (an embedding/LLM classifier is the natural upgrade).
* Comply with CAN-SPAM/GDPR/local law: honor opt-outs (`suppress`), send only to business contacts creators publish.
