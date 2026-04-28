"""Reddit mention monitor.

Fetches posts and comments matching the configured brand and competitor
keywords, scores sentiment via a hybrid keyword + LLM analyzer, and returns
a pandas DataFrame ready for trendline plotting.

The keyword scorer handles obvious complaint and praise patterns ("never
buy", "do not buy", "avoid", "is it legit", "genuine", "recommend"). The LLM
analyzer (via `claude -p`) takes over for entity-aware sentiment, so a post
trashing a competitor while switching to the brand is correctly scored
brand-positive even though the text is full of negative words.

All brand-specific configuration (brand name, search keywords, competitors,
subreddit filter, voice) lives in config.json — see config.py.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests

from config import build_campaigns, load_config, primary_brand_keyword

REDDIT_SEARCH_URL = "https://www.reddit.com/search.json"
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# Loaded once at import. Restart the app after editing config.json so these
# pick up the change (the Setup page does this for you via st.rerun()).
_CFG = load_config()
USER_AGENT = (_CFG.get("app") or {}).get("user_agent", "reddit-monitor/0.4 (by u/anonymous)")

ANALYSIS_CACHE_VERSION = "v6-action-type"
ANALYSIS_CACHE_PATH = DATA_DIR / "analysis_cache.json"
LLM_ANALYZE_CHUNK = 15  # mentions per `claude -p` call
LLM_TIMEOUT_S = 120


# ---------------------------------------------------------------------------
# Subreddit filter — driven by config.subreddit_filter
# ---------------------------------------------------------------------------
# mode "any":       no filter (default for the OSS template).
# mode "allowlist": keep only subreddits whose name is in the allowlist OR
#                   contains one of the configured name_substrings, plus
#                   user-profile pages (u_*) when include_user_pages is true.


def _passes_subreddit_filter(subreddit: str, cfg: dict | None = None) -> bool:
    cfg = cfg if cfg is not None else _CFG
    flt = cfg.get("subreddit_filter") or {}
    mode = (flt.get("mode") or "any").lower()
    if mode == "any":
        return True
    if not subreddit:
        return False
    s = subreddit.lower()
    if flt.get("include_user_pages", True) and s.startswith("u_"):
        return True
    allowlist = {a.lower() for a in (flt.get("allowlist") or [])}
    if s in allowlist:
        return True
    for sub in (flt.get("name_substrings") or []):
        if sub and sub.lower() in s:
            return True
    return False


# ---------------------------------------------------------------------------
# Campaigns — built from config.json
# ---------------------------------------------------------------------------

CAMPAIGNS: dict[str, dict] = build_campaigns(_CFG)


def get_campaign(name: str) -> dict:
    if name not in CAMPAIGNS:
        raise ValueError(f"Unknown campaign: {name}. Known: {list(CAMPAIGNS)}")
    return CAMPAIGNS[name]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Mention:
    id: str
    kind: str  # "post" or "comment"
    subreddit: str
    author: str
    created_utc: float
    title: str
    body: str
    url: str
    score: int
    num_comments: int = 0
    parent_id: str = ""  # for comments: the parent post's Reddit id (no t3_ prefix)
    parent_title: str = ""  # for comments: the parent post's title
    parent_url: str = ""  # for comments: the parent post's URL
    parent_sentiment_label: str = ""  # for comments: parent post's sentiment, if known
    parent_sentiment: float = 0.0  # for comments: parent post's numeric sentiment
    parent_in_posts: bool = False  # True if the parent post is in our Posts table
    # source provenance:
    #   "post-search"          → posts directly matching the keyword (Reddit search)
    #   "within-post-comment"  → comments inside a matched post that mention the keyword
    #   "independent-comment"  → comments found via PullPush keyword search (parent may or may not be in Posts)
    source: str = ""
    # Analyzer fields, filled in later.
    sentiment: float = 0.0  # -1.0 negative, 0 neutral, +1.0 positive (soft numeric)
    sentiment_label: str = "neutral"
    issue_summary: str = ""
    action_type: str = ""  # off_topic | recommendation_request | complaint | praise | deal_share | general_discussion

    @property
    def text(self) -> str:
        return f"{self.title}\n\n{self.body}".strip()

    @property
    def created_dt(self) -> datetime:
        return datetime.fromtimestamp(self.created_utc, tz=timezone.utc)


@dataclass
class FetchResult:
    mentions: list[Mention] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Reddit fetch
# ---------------------------------------------------------------------------


# Module-level circuit breaker — if Reddit throttles us, stop hammering it.
_REDDIT_THROTTLED = False


def _search_one(query: str, sort: str, limit: int, time_filter: str) -> list[dict]:
    """Call Reddit's public search JSON endpoint and return raw children.

    Bounded retries on 429 with short exponential backoff. Once we trip the
    module-level circuit breaker, every subsequent call returns [] immediately
    so the dashboard doesn't sit waiting for a recovery that won't come on
    this run. The store-augment fallback kicks in instead.
    """
    global _REDDIT_THROTTLED
    if _REDDIT_THROTTLED:
        return []
    results: list[dict] = []
    after: str | None = None
    remaining = limit
    while remaining > 0:
        page_size = min(100, remaining)
        params = {
            "q": query,
            "sort": sort,
            "t": time_filter,
            "limit": page_size,
            "restrict_sr": "false",
            "type": "link",
        }
        if after:
            params["after"] = after
        # Single attempt, no 429 backoff — if Reddit is throttling, we trip
        # the circuit breaker immediately and let the store-augment fallback
        # populate the view. Faster fail = better UX than minutes of backoff.
        try:
            resp = requests.get(
                REDDIT_SEARCH_URL,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=20,
            )
        except requests.RequestException as e:
            print(f"[reddit] request error on '{query}': {e}; tripping breaker")
            _REDDIT_THROTTLED = True
            break
        if resp.status_code == 429:
            print(
                f"[reddit] 429 on '{query}'; tripping circuit breaker "
                "— store fallback will populate the view"
            )
            _REDDIT_THROTTLED = True
            break
        resp.raise_for_status()
        data = resp.json().get("data", {})
        children = data.get("children", [])
        if not children:
            break
        results.extend(children)
        after = data.get("after")
        remaining -= len(children)
        if not after:
            break
        time.sleep(1.0)
    return results


def reset_throttle() -> None:
    """Reset the Reddit-throttled flag. Call after a long idle period."""
    global _REDDIT_THROTTLED
    _REDDIT_THROTTLED = False


def _fetch_comments_for_post(permalink: str, limit: int = 50) -> list[dict]:
    url = f"https://www.reddit.com{permalink}.json"
    resp = requests.get(
        url,
        params={"limit": limit, "sort": "new"},
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    if resp.status_code != 200:
        return []
    listings = resp.json()
    if not isinstance(listings, list) or len(listings) < 2:
        return []
    comment_listing = listings[1].get("data", {}).get("children", [])
    return [c for c in comment_listing if c.get("kind") == "t1"]


def _post_to_mention(child: dict) -> Mention:
    d = child["data"]
    return Mention(
        id=d.get("id", ""),
        kind="post",
        subreddit=d.get("subreddit", ""),
        author=d.get("author", "") or "[deleted]",
        created_utc=float(d.get("created_utc", 0)),
        title=d.get("title", "") or "",
        body=d.get("selftext", "") or "",
        url=f"https://www.reddit.com{d.get('permalink', '')}",
        score=int(d.get("score", 0)),
        num_comments=int(d.get("num_comments", 0)),
    )


def _comment_to_mention(
    child: dict,
    parent_post: "Mention | None" = None,
) -> Mention:
    d = child["data"]
    body = d.get("body", "") or ""
    link_id = d.get("link_id", "") or ""
    parent_reddit_id = link_id.removeprefix("t3_") if link_id else ""
    parent_title = parent_post.title if parent_post else ""
    parent_url = parent_post.url if parent_post else ""
    return Mention(
        id=d.get("id", ""),
        kind="comment",
        subreddit=d.get("subreddit", ""),
        author=d.get("author", "") or "[deleted]",
        created_utc=float(d.get("created_utc", 0)),
        title=f"re: {parent_title}" if parent_title else "",
        body=body,
        url=f"https://www.reddit.com{d.get('permalink', '')}"
        if d.get("permalink")
        else "",
        score=int(d.get("score", 0)),
        parent_id=parent_reddit_id or (parent_post.id if parent_post else ""),
        parent_title=parent_title,
        parent_url=parent_url,
    )


def _contains_query(text: str, queries: Iterable[str]) -> bool:
    """True if any query matches the text on a word boundary.

    Word-boundary matching (vs substring) avoids false positives like a
    short keyword matching every URL with that string in a path or username.
    For multi-word queries (e.g. a brand whose name is two words), we require
    the phrase as a contiguous string with surrounding word boundaries.
    """
    lowered = text.lower()
    for q in queries:
        needle = q.strip('"').lower().strip()
        if not needle:
            continue
        # Build a regex that anchors with word boundaries on both sides.
        # `re.escape` handles spaces in multi-word phrases correctly.
        pattern = r"(?<![A-Za-z0-9_])" + re.escape(needle) + r"(?![A-Za-z0-9_])"
        if re.search(pattern, lowered):
            return True
    return False


# ---------------------------------------------------------------------------
# Pullpush — independent comment search across all of Reddit
# ---------------------------------------------------------------------------
#
# Reddit's public search.json doesn't return comments (type=comment is silently
# treated as a post search). Pushshift is dead. PullPush is the standard mirror
# that still serves the comment endpoint.

PULLPUSH_COMMENT_URL = "https://api.pullpush.io/reddit/search/comment/"

_TIME_FILTER_SECONDS = {
    "hour": 3600,
    "day": 86400,
    "week": 7 * 86400,
    "month": 30 * 86400,
    "year": 365 * 86400,
    "all": None,
}


def _search_comments_pullpush(
    query: str, limit: int, time_filter: str
) -> list[dict]:
    """Search Reddit comments containing the query via PullPush.

    Returns raw PullPush comment objects. Caps page size at 100 (PullPush limit).
    """
    seconds = _TIME_FILTER_SECONDS.get(time_filter)
    after_ts = None
    if seconds is not None:
        after_ts = int(time.time()) - seconds

    results: list[dict] = []
    remaining = limit
    before: int | None = None
    # PullPush paginates via `before` (newest first). We loop with shrinking before.
    while remaining > 0:
        page_size = min(100, remaining)
        params = {"q": query.strip('"'), "size": page_size}
        if after_ts is not None:
            params["after"] = after_ts
        if before is not None:
            params["before"] = before
        try:
            resp = requests.get(
                PULLPUSH_COMMENT_URL,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=30,
            )
        except requests.RequestException as e:
            print(f"[pullpush] request failed: {e}")
            break
        if resp.status_code != 200:
            print(f"[pullpush] status {resp.status_code}; aborting page loop")
            break
        try:
            payload = resp.json()
        except ValueError:
            break
        page = payload.get("data", []) or []
        if not page:
            break
        results.extend(page)
        remaining -= len(page)
        # Continue from the oldest comment on this page.
        try:
            before = int(page[-1].get("created_utc", 0))
            if not before:
                break
        except (TypeError, ValueError):
            break
        if len(page) < page_size:
            break  # no more
        time.sleep(0.5)
    return results


def _pullpush_comment_to_mention(d: dict) -> Mention:
    """Convert a PullPush comment dict to a Mention. Parent is hydrated later."""
    link_id = d.get("link_id", "") or ""
    parent_reddit_id = link_id.removeprefix("t3_") if link_id else ""
    permalink = d.get("permalink", "") or ""
    return Mention(
        id=d.get("id", "") or "",
        kind="comment",
        subreddit=d.get("subreddit", "") or "",
        author=d.get("author", "") or "[deleted]",
        created_utc=float(d.get("created_utc", 0) or 0),
        title="",  # filled in by hydration below
        body=d.get("body", "") or "",
        url=f"https://www.reddit.com{permalink}" if permalink else "",
        score=int(d.get("score", 0) or 0),
        parent_id=parent_reddit_id,
    )


# ---------------------------------------------------------------------------
# Parent post hydration (for orphan comments — parent not in our Posts list)
# ---------------------------------------------------------------------------


def _hydrate_parent_posts(parent_ids: list[str]) -> dict[str, Mention]:
    """Look up parent post details by id via Reddit's /by_id/ endpoint.

    Returns a dict {post_id: Mention(kind="post")}. Used to provide title/URL
    context for comments whose parent isn't in our keyword-matched Posts list.
    These hydrated parents are NOT added to the Posts table — they exist only
    to populate parent_title/parent_url on comments.
    """
    if not parent_ids:
        return {}
    out: dict[str, Mention] = {}
    # /by_id/ accepts up to 100 fullnames per call.
    for i in range(0, len(parent_ids), 100):
        batch = parent_ids[i : i + 100]
        names = ",".join(f"t3_{pid}" for pid in batch if pid)
        if not names:
            continue
        try:
            resp = requests.get(
                f"https://www.reddit.com/by_id/{names}.json",
                headers={"User-Agent": USER_AGENT},
                timeout=20,
            )
        except requests.RequestException as e:
            print(f"[hydrate] request failed: {e}")
            continue
        if resp.status_code != 200:
            print(f"[hydrate] status {resp.status_code}; skipping batch")
            continue
        try:
            data = resp.json()
        except ValueError:
            continue
        children = data.get("data", {}).get("children", []) if isinstance(data, dict) else []
        for c in children:
            if c.get("kind") != "t3":
                continue
            m = _post_to_mention(c)
            if m.id:
                out[m.id] = m
        time.sleep(0.5)
    return out


# ---------------------------------------------------------------------------
# Keyword-based analyzer
# ---------------------------------------------------------------------------
#
# Weighted phrase lists. Each phrase is matched as a regex with word boundaries
# where the phrase is a single word. Weight 2 = strong signal, 1 = mild.
#
# Tuned for common patterns in brand-monitoring text:
#   - "Never buy from <brand>" → negative
#   - "Do NOT buy from <brand>" where another brand is the bad seller → negative
#   - "Is <brand> legit?" → neutral (question; no verdict)
#   - "<brand> deal: WHEY PROTEIN down to $24" → neutral/positive
#
# Phrases are lowercased; we lowercase the text before scanning.

_NEG_PHRASES: list[tuple[str, int]] = [
    # Strong — direct warnings
    (r"\bnever buy\b", 2),
    (r"\bdo not buy\b", 2),
    (r"\bdon'?t buy\b", 2),
    (r"\bstay away\b", 2),
    (r"\bavoid\b", 2),
    (r"\bscam\b", 2),
    (r"\bfraud\b", 2),
    (r"\bfake\b", 2),
    (r"\bcounterfeit\b", 2),
    (r"\bripoff\b", 2),
    (r"\brip[- ]off\b", 2),
    (r"\bwaste of money\b", 2),
    (r"\bworst\b", 2),
    (r"\bterrible\b", 2),
    (r"\bhorrible\b", 2),
    (r"\bregret\b", 2),
    (r"\bwouldn'?t recommend\b", 2),
    (r"\bdon'?t recommend\b", 2),
    (r"\bmistake\b", 1),
    (r"\bcheated\b", 2),
    (r"\bmisled\b", 2),
    # Mild — specific complaints common to e-commerce
    (r"\brefund (?:not|never|pending)\b", 2),
    (r"\brefund\b", 1),
    (r"\breturn\b", 1),
    (r"\bstuck\b", 1),
    (r"\bdelayed?\b", 1),
    (r"\bdelay\b", 1),
    (r"\bissue\b", 1),
    (r"\bproblem\b", 1),
    (r"\bcomplaint\b", 1),
    (r"\bdisappointed\b", 2),
    (r"\bexpired\b", 2),
    (r"\bspoiled\b", 2),
    (r"\bspoilt\b", 2),
    (r"\bnot genuine\b", 2),
    (r"\bmissing\b", 1),
    (r"\bbroken seal\b", 2),
    (r"\bcustomer (?:care|support|service)\b", 1),  # usually mentioned in complaints
    (r"\bno response\b", 1),
    (r"\bunresponsive\b", 2),
    (r"\bstupid\b", 1),
    (r"\btrash\b", 1),
    (r"\bbad\b", 1),
    (r"\bworse\b", 1),
]

_POS_PHRASES: list[tuple[str, int]] = [
    # Strong
    (r"\bhighly recommend\b", 2),
    (r"\blegit\b", 1),  # ambiguous (questions ask this too); light weight
    (r"\bgenuine\b", 2),
    (r"\bauthentic\b", 2),
    (r"\blove (?:it|them)\b", 2),
    (r"\bgreat (?:deal|price|service)\b", 2),
    (r"\bbest price\b", 2),
    (r"\bworth it\b", 2),
    (r"\bsatisf(?:ied|action)\b", 2),
    (r"\bhappy with\b", 2),
    # Mild
    (r"\brecommend\b", 1),
    (r"\bgood\b", 1),
    (r"\bgreat\b", 1),
    (r"\bnice\b", 1),
    (r"\bdecent\b", 1),
    (r"\baffordable\b", 1),
    (r"\bcheaper\b", 1),
    (r"\bdiscount\b", 1),
    (r"\bdeal\b", 1),
    (r"\boff on\b", 1),  # "10% off on whey..."
    (r"\brewards?\b", 1),
]

# Question markers — if the text is predominantly a question, downweight the
# sentiment score (we don't want "is <brand> legit?" labeled positive just
# because it contains "legit").
def _brand_keyword_alt(cfg: dict | None = None) -> str:
    """Regex alternation of the brand's search keywords, e.g. (?:acme|acmeprotein)."""
    cfg = cfg if cfg is not None else _CFG
    kws = (cfg.get("brand") or {}).get("search_keywords") or []
    escaped = [re.escape(k.strip()) for k in kws if k and k.strip()]
    if not escaped:
        return r"(?:brand)"
    return "(?:" + "|".join(escaped) + ")"


_BRAND_ALT = _brand_keyword_alt()

_QUESTION_MARKERS = [
    rf"\bis {_BRAND_ALT} legit\b",
    r"\bany (?:coupon|promo|discount) code\b",
    r"\bhas anyone\b",
    r"\bdoes anyone\b",
    r"\bwhy is it\b",
    r"\banyone (?:used|tried|ordered)\b",
]


def _score_text(text: str) -> tuple[int, int, bool]:
    """Return (positive_score, negative_score, is_question)."""
    t = text.lower()
    is_question = any(re.search(p, t) for p in _QUESTION_MARKERS) or t.strip().endswith("?")
    pos = sum(w for pat, w in _POS_PHRASES if re.search(pat, t))
    neg = sum(w for pat, w in _NEG_PHRASES if re.search(pat, t))
    return pos, neg, is_question


_POSITIVE_OVERRIDES = [
    # "never had [an/any] issue/problem/complaint" → reassurance, positive
    r"\bnever had (?:an?|any)? ?(?:issue|problem|complaint|trouble)\b",
    r"\bno (?:issues?|problems?|complaints?)\b",
    r"\bnever faced (?:an?|any)? ?(?:issue|problem)\b",
    r"\balways (?:on time|fast|fresh|genuine)\b",
    rf"\brecommend {_BRAND_ALT}\b",
    rf"\bsub recommends {_BRAND_ALT}\b",
    rf"\b{_BRAND_ALT} is legit\b",
    rf"\b{_BRAND_ALT} is genuine\b",
]


def _classify(mention: Mention) -> tuple[str, float]:
    """Return (label, soft_score in [-1, +1])."""
    body_lower = (mention.body or "").lower()

    # Positive overrides beat everything else — these phrases are unambiguous
    # reassurance in the comment body.
    if any(re.search(p, body_lower) for p in _POSITIVE_OVERRIDES):
        return "positive", 0.75

    # Comments carry "re: <parent title>" as their `title` field, which poisons
    # the score with the parent post's keywords. Score the body only for comments.
    if mention.kind == "comment":
        pos, neg, is_question = _score_text(mention.body or "")
    else:
        t_pos, t_neg, t_q = _score_text(mention.title or "")
        b_pos, b_neg, b_q = _score_text(mention.body or "")
        # Title gets 2x weight (titles were the clearest signal in the VADER failures).
        pos = t_pos * 2 + b_pos
        neg = t_neg * 2 + b_neg
        is_question = t_q or b_q

    # Hard overrides for posts: strong-negative phrases in the title dominate.
    if mention.kind == "post":
        title_lower = (mention.title or "").lower()
        if any(
            re.search(p, title_lower)
            for p in [
                r"\bnever buy\b",
                r"\bdo not buy\b",
                r"\bdon'?t buy\b",
                r"\bstay away\b",
            ]
        ):
            return "negative", -0.95

    # Pure question with no strong signals → neutral, even if some keywords matched.
    if is_question and pos <= 2 and neg <= 2:
        return "neutral", 0.0

    if pos == 0 and neg == 0:
        return "neutral", 0.0

    total = pos + neg
    score = (pos - neg) / total  # in [-1, 1]
    if score >= 0.2:
        return "positive", round(score, 2)
    if score <= -0.2:
        return "negative", round(score, 2)
    return "neutral", round(score, 2)


def _make_issue_summary(mention: Mention) -> str:
    """Short preview of the user's topic/issue. Not a true summary."""
    body = (mention.body or "").strip().replace("\n", " ").replace("\r", " ")
    body = re.sub(r"\s+", " ", body)
    # Take first sentence-ish chunk.
    cut = re.split(r"(?<=[.!?])\s", body, maxsplit=1)[0] if body else ""
    cut = cut.strip()
    if len(cut) > 160:
        cut = cut[:157].rstrip() + "…"
    return cut


def _load_analysis_cache() -> dict[str, dict]:
    if not ANALYSIS_CACHE_PATH.exists():
        return {}
    try:
        raw = json.loads(ANALYSIS_CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    if raw.get("version") != ANALYSIS_CACHE_VERSION:
        return {}
    return raw.get("entries", {})


def _save_analysis_cache(entries: dict[str, dict]) -> None:
    tmp = ANALYSIS_CACHE_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"version": ANALYSIS_CACHE_VERSION, "entries": entries})
    )
    tmp.replace(ANALYSIS_CACHE_PATH)


# ---------------------------------------------------------------------------
# LLM analyzer (entity-aware) via `claude -p` subprocess
# ---------------------------------------------------------------------------
#
# Why: keyword sentiment can't do entity-level analysis. A post titled
#   "<Competitor> Review — concerned about <Competitor>, switching to <Brand>"
# is brand-positive but contains every "concerned/regret/avoid" trigger.
# We need the model to see which brand each negative phrase refers to.
# `claude -p` uses the user's Pro or Max plan auth — no API key needed.

_LLM_SYSTEM_TMPL = """You score Reddit mentions for {subject_label} brand monitoring.

ABOUT THE SUBJECT:
{subject_description}

For each input mention, output ONE record. Output preserves input order. Use the exact `id` provided. Each record has FOUR fields: sentiment, confidence, issue_summary, action_type.

SENTIMENT (about {subject_label} SPECIFICALLY, not competitors, not the user's mood):
- If the user trashes a competitor and switches to {subject_label}, that is {subject_label}-positive even though the post is full of negative words.
- If the user complains about a delayed order from {subject_label}, that is {subject_label}-negative.
- If the user just asks "is {subject_label} legit?" with no opinion, that is neutral.
- For comments, sentiment reflects what the COMMENT AUTHOR says, not the parent post's stance. "never had any issue" is positive even on a complaint thread.

ISSUE_SUMMARY (4-12 words):
Describe what THIS mention is about {subject_label}. Complaint → the core complaint. Question → the question. Praise → what they like. False-positive keyword match → empty string.

ACTION_TYPE — pick exactly one of these labels for each mention:
- "off_topic" — post is a giveaway, news article, viral image post, sports/politics/relationships chatter, or just doesn't actually discuss the category or brand at all. Crucial: posts where a generic-phrase keyword (e.g. a multi-word brand whose name is also a common phrase) appears in a non-brand context are off_topic. Reply will be SKIPPED.
- "recommendation_request" — OP is asking what product, brand, or platform to buy / use / try. Comparing options. "Which one is best?", "is <subject> legit?", "<X> vs <Y>?".
- "complaint" — OP describes a bad experience with a product or platform (delivery delay, fake product, bad taste, refund issue, side effect).
- "praise" — OP describes a positive experience or actively recommends a product / platform. Includes glowing reviews and "X works great".
- "deal_share" — OP is sharing a discount, promo, coupon, or sale. May or may not be commentary; primarily informational.
- "general_discussion" — generic question or discussion about the category without a clear ask, complaint, or praise. E.g. dosage advice, ingredient questions, technical / scientific discussion.

For COMMENTS, action_type reflects what the comment is doing (not the parent post). A comment that recommends an alternative is "praise" (toward the alternative) or could be "complaint" (about the parent's choice).

Return ONLY valid JSON (no markdown fence, no prose). Schema:
{{"items":[{{"id":"<exact id>","sentiment":"positive|negative|neutral","confidence":0.0-1.0,"issue_summary":"...","action_type":"off_topic|recommendation_request|complaint|praise|deal_share|general_discussion"}}]}}"""


def _llm_system(subject_label: str, subject_description: str) -> str:
    return _LLM_SYSTEM_TMPL.format(
        subject_label=subject_label, subject_description=subject_description
    )


def _build_llm_user_prompt(chunk: list[Mention]) -> str:
    lines = [f"Score these {len(chunk)} Reddit mention(s). One record per id, same order."]
    for i, m in enumerate(chunk, start=1):
        body = (m.body or "").replace("\r", "").strip()
        if len(body) > 1500:
            body = body[:1500] + " …[truncated]"
        title = m.title or "(no title)"
        parent_bit = (
            f" | parent post: {m.parent_title!r}"
            if m.kind == "comment" and m.parent_title
            else ""
        )
        lines.append(
            f"\n[{i}] id={m.id} kind={m.kind} subreddit=r/{m.subreddit}{parent_bit}"
            f"\nTITLE: {title}"
            f"\nBODY: {body or '(empty)'}"
        )
    lines.append("\nReturn JSON only.")
    return "\n".join(lines)


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        # ```json\n...\n```
        s = s.strip("`").strip()
        if s.lower().startswith("json"):
            s = s[4:].strip()
    return s


def _parse_llm_json(raw: str) -> list[dict]:
    raw = _strip_fences(raw)
    try:
        start = raw.index("{")
        end = raw.rindex("}") + 1
        payload = json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError):
        return []
    return payload.get("items", []) or []


def _llm_score_chunk(
    chunk: list[Mention],
    subject_label: str,
    subject_description: str,
) -> dict[str, dict]:
    """Call `claude -p` for a chunk. Returns {id: {sentiment, confidence, issue_summary}}."""
    if not chunk:
        return {}
    full_prompt = (
        _llm_system(subject_label, subject_description)
        + "\n\n---\n\n"
        + _build_llm_user_prompt(chunk)
    )
    try:
        proc = subprocess.run(
            ["claude", "-p", full_prompt, "--output-format", "text"],
            capture_output=True,
            text=True,
            timeout=LLM_TIMEOUT_S,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"[llm] claude -p failed: rc={e.returncode} stderr={(e.stderr or '')[:300]}")
        return {}
    except subprocess.TimeoutExpired:
        print(f"[llm] claude -p timed out after {LLM_TIMEOUT_S}s")
        return {}
    items = _parse_llm_json(proc.stdout)
    out: dict[str, dict] = {}
    valid_actions = {
        "off_topic",
        "recommendation_request",
        "complaint",
        "praise",
        "deal_share",
        "general_discussion",
    }
    for it in items:
        rid = str(it.get("id", "")).strip()
        if not rid:
            continue
        label = str(it.get("sentiment", "neutral")).lower().strip()
        if label not in ("positive", "negative", "neutral"):
            label = "neutral"
        try:
            conf = float(it.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        conf = max(0.0, min(1.0, conf))
        summary = str(it.get("issue_summary", "")).strip()
        action_type = str(it.get("action_type", "")).lower().strip()
        if action_type not in valid_actions:
            action_type = "general_discussion"
        out[rid] = {
            "sentiment": label,
            "confidence": conf,
            "issue_summary": summary,
            "action_type": action_type,
        }
    return out


def _sentiment_to_float(label: str, confidence: float) -> float:
    if label == "positive":
        return round(confidence, 2)
    if label == "negative":
        return round(-confidence, 2)
    return 0.0


def _claude_cli_available() -> bool:
    return shutil.which("claude") is not None


def analyze_mentions(
    mentions: list[Mention],
    subject_label: str,
    subject_description: str | None = None,
    progress_cb=None,
) -> None:
    """Populate sentiment / sentiment_label / issue_summary on each mention.

    Primary path: LLM via `claude -p` (entity-aware). Falls back to keyword
    scorer if the CLI is unavailable or every batch fails.
    Cached on disk per (mention id, subject_label) — different subjects can
    score the same mention differently.
    """
    if not mentions:
        return
    if subject_description is None:
        subject_description = CAMPAIGNS["brand"]["subject_description"]

    cache_key_prefix = subject_label.lower().replace(" ", "_").replace("/", "-")
    cache = _load_analysis_cache()

    # Cache keys are now scoped per-subject so the same mention can have
    # a different label in different campaigns.
    def _ck(mid: str) -> str:
        return f"{cache_key_prefix}::{mid}"

    # Apply cached results; collect what still needs scoring.
    to_score: list[Mention] = []
    for m in mentions:
        ck = _ck(m.id) if m.id else None
        if ck and ck in cache:
            c = cache[ck]
            m.sentiment_label = c["sentiment_label"]
            m.sentiment = c["sentiment"]
            m.issue_summary = c.get("issue_summary", "")
            m.action_type = c.get("action_type", "")
        else:
            to_score.append(m)

    if not to_score:
        return

    scored_ids: set[str] = set()
    if _claude_cli_available():
        total_batches = (len(to_score) + LLM_ANALYZE_CHUNK - 1) // LLM_ANALYZE_CHUNK
        for i in range(0, len(to_score), LLM_ANALYZE_CHUNK):
            chunk = to_score[i : i + LLM_ANALYZE_CHUNK]
            batch_idx = i // LLM_ANALYZE_CHUNK + 1
            msg = f"[analyzer:{subject_label}] LLM batch {batch_idx}/{total_batches} ({len(chunk)} items)…"
            print(msg)
            if progress_cb:
                try:
                    progress_cb(f"Scoring sentiment: batch {batch_idx}/{total_batches} ({len(chunk)} items)")
                except Exception:
                    pass
            results = _llm_score_chunk(chunk, subject_label, subject_description)
            for m in chunk:
                r = results.get(m.id)
                if not r:
                    continue
                m.sentiment_label = r["sentiment"]
                m.sentiment = _sentiment_to_float(r["sentiment"], r["confidence"])
                m.issue_summary = r["issue_summary"]
                m.action_type = r.get("action_type", "general_discussion")
                cache[_ck(m.id)] = {
                    "sentiment_label": m.sentiment_label,
                    "sentiment": m.sentiment,
                    "confidence": r["confidence"],
                    "issue_summary": m.issue_summary,
                    "action_type": m.action_type,
                    "subject": subject_label,
                    "method": "claude-cli",
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
                scored_ids.add(m.id)
    else:
        print("[analyzer] `claude` CLI not on PATH — using keyword fallback for all items")

    unscored = [m for m in to_score if m.id and m.id not in scored_ids]
    if unscored and _claude_cli_available():
        print(f"[analyzer] LLM missed {len(unscored)} items; using keyword fallback for them")
    for m in unscored:
        label, score = _classify(m)
        m.sentiment_label = label
        m.sentiment = score
        m.issue_summary = _make_issue_summary(m)
        m.action_type = "general_discussion"  # safe default for keyword fallback
        if m.id:
            cache[_ck(m.id)] = {
                "sentiment_label": label,
                "sentiment": score,
                "issue_summary": m.issue_summary,
                "action_type": m.action_type,
                "subject": subject_label,
                "method": "keyword",
                "ts": datetime.now(timezone.utc).isoformat(),
            }

    _save_analysis_cache(cache)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def fetch_mentions(
    queries: list[str] | None = None,
    limit_per_query: int = 100,
    time_filter: str = "month",
    include_comments: bool = True,
    cache_ttl_seconds: int = 900,
    campaign: str = "brand",
    persist_to_store: bool = True,
    progress_cb=None,
) -> FetchResult:
    """Search Reddit for mentions and run the analyzer.

    time_filter: one of "hour", "day", "week", "month", "year", "all".
    campaign: key into CAMPAIGNS — selects subject_label/description for the
              entity-aware analyzer and the campaign tag in the SQLite store.
    persist_to_store: if True, upsert all fetched mentions into the SQLite store.
    """
    camp = CAMPAIGNS.get(campaign, CAMPAIGNS["brand"])
    # Default queries come from the campaign config, not the global brand list.
    queries = queries or camp["queries"]
    cache_key = f"{campaign}__{'-'.join(q.strip(chr(34)) for q in queries)}__{time_filter}__{limit_per_query}__{int(include_comments)}__{ANALYSIS_CACHE_VERSION}.json"
    cache_path = DATA_DIR / cache_key.replace(" ", "_")

    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < cache_ttl_seconds:
            raw = json.loads(cache_path.read_text())
            mentions = [Mention(**m) for m in raw["mentions"]]
            return FetchResult(
                mentions=mentions,
                fetched_at=datetime.fromisoformat(raw["fetched_at"]),
            )

    def _tick(msg: str) -> None:
        print(f"[{campaign}] {msg}")
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:
                pass

    seen: dict[str, Mention] = {}
    posts_by_id: dict[str, Mention] = {}
    max_posts = camp.get("max_posts")
    max_total_mentions = camp.get("max_total_mentions")

    # 1. Direct post search via Reddit's search.json
    _tick(f"Reddit post search ({len(queries)} keyword(s))")
    for q in queries:
        for sort in ("new", "relevance"):
            children = _search_one(q, sort, limit_per_query, time_filter)
            for c in children:
                if c.get("kind") != "t3":
                    continue
                m = _post_to_mention(c)
                m.source = "post-search"
                if m.id and m.id not in seen and _contains_query(m.text, queries):
                    seen[m.id] = m
                    posts_by_id[m.id] = m

    # 1b. Apply per-campaign post cap (keep newest first).
    post_mentions_all = sorted(
        [m for m in list(seen.values()) if m.kind == "post"],
        key=lambda m: m.created_utc,
        reverse=True,
    )
    if max_posts is not None and len(post_mentions_all) > max_posts:
        kept = post_mentions_all[:max_posts]
        kept_ids = {m.id for m in kept}
        # Drop posts beyond the cap from `seen` and `posts_by_id`.
        for m in post_mentions_all[max_posts:]:
            seen.pop(m.id, None)
            posts_by_id.pop(m.id, None)
        _tick(
            f"Capped to {max_posts} most-recent posts (had {len(post_mentions_all)})"
        )

    # 2. Comments inside the matched posts (Reddit JSON for each post)
    if include_comments:
        post_mentions = [m for m in list(seen.values()) if m.kind == "post"]
        post_cap = min(40, len(post_mentions))
        _tick(f"Within-post comment fetch ({post_cap} post(s))")
        for parent in post_mentions[:40]:  # cap to stay polite
            permalink = parent.url.replace("https://www.reddit.com", "")
            for child in _fetch_comments_for_post(permalink):
                cm = _comment_to_mention(child, parent_post=parent)
                cm.source = "within-post-comment"
                if cm.id and cm.id not in seen and _contains_query(cm.body, queries):
                    seen[cm.id] = cm
            time.sleep(0.5)

        # 3. Independent comment search across all of Reddit (PullPush)
        _tick(f"PullPush independent comment search ({len(queries)} keyword(s))")
        for q in queries:
            raw_comments = _search_comments_pullpush(q, limit_per_query, time_filter)
            for d in raw_comments:
                cm = _pullpush_comment_to_mention(d)
                if not cm.id:
                    continue
                if not _contains_query(cm.body, queries):
                    # PullPush sometimes returns near-matches; keep the filter.
                    continue
                if cm.id in seen:
                    # Already collected via within-post fetch — upgrade source label?
                    # Keep the earlier (more specific) source.
                    continue
                cm.source = "independent-comment"
                seen[cm.id] = cm

        # 4. Hydrate parents for any comment whose parent isn't in our Posts list.
        orphan_parent_ids: set[str] = set()
        for m in seen.values():
            if m.kind != "comment" or not m.parent_id:
                continue
            if m.parent_id not in posts_by_id:
                orphan_parent_ids.add(m.parent_id)
        if orphan_parent_ids:
            _tick(f"Hydrating {len(orphan_parent_ids)} parent post(s)")
            hydrated = _hydrate_parent_posts(sorted(orphan_parent_ids))
        else:
            hydrated = {}

        # 5. Stamp parent_title / parent_url / parent_in_posts on every comment.
        for m in seen.values():
            if m.kind != "comment":
                continue
            if m.parent_id and m.parent_id in posts_by_id:
                p = posts_by_id[m.parent_id]
                m.parent_title = p.title
                m.parent_url = p.url
                m.title = f"re: {p.title}"
                m.parent_in_posts = True
            elif m.parent_id and m.parent_id in hydrated:
                p = hydrated[m.parent_id]
                m.parent_title = p.title
                m.parent_url = p.url
                m.title = f"re: {p.title}"
                m.parent_in_posts = False

    # Subreddit filter — driven by config.subreddit_filter. With mode "any"
    # (the OSS default) this is a no-op. With mode "allowlist" it drops
    # mentions from subreddits not on the allowlist or matching one of the
    # configured name_substrings — useful for region-locking (e.g. India)
    # or category-locking the dashboard.
    flt = (_CFG.get("subreddit_filter") or {})
    if (flt.get("mode") or "any").lower() == "allowlist":
        before = len(seen)
        seen = {
            mid: m for mid, m in seen.items()
            if _passes_subreddit_filter(m.subreddit)
        }
        if before != len(seen):
            _tick(
                f"Subreddit filter: kept {len(seen)} of {before} mentions"
            )
            posts_by_id = {
                pid: p for pid, p in posts_by_id.items() if pid in seen
            }

    mentions = sorted(seen.values(), key=lambda m: m.created_utc, reverse=True)
    if max_total_mentions is not None and len(mentions) > max_total_mentions:
        _tick(
            f"Trimming to {max_total_mentions} most-recent mentions "
            f"(had {len(mentions)})"
        )
        mentions = mentions[:max_total_mentions]
    _tick(f"Analyzing {len(mentions)} mention(s) — calls Claude in batches of {LLM_ANALYZE_CHUNK}")
    analyze_mentions(
        mentions,
        subject_label=camp["subject_label"],
        subject_description=camp["subject_description"],
        progress_cb=progress_cb,
    )

    # 6. Now that posts have sentiment, propagate parent sentiment onto comments.
    post_sentiment_by_id: dict[str, tuple[str, float]] = {
        m.id: (m.sentiment_label, m.sentiment)
        for m in mentions
        if m.kind == "post"
    }
    for m in mentions:
        if m.kind != "comment" or not m.parent_id:
            continue
        if m.parent_id in post_sentiment_by_id:
            label, score = post_sentiment_by_id[m.parent_id]
            m.parent_sentiment_label = label
            m.parent_sentiment = score

    # 7. Persist to the SQLite store so subsequent runs accumulate history.
    if persist_to_store:
        try:
            from store import upsert_mentions  # local import to avoid a hard dep at module import time
            new_count, updated_count = upsert_mentions(campaign, mentions)
            print(
                f"[store:{campaign}] upserted {len(mentions)} mentions "
                f"({new_count} new, {updated_count} updated)"
            )
        except Exception as e:  # store failure shouldn't break the dashboard
            print(f"[store] persist failed: {type(e).__name__}: {e}")

    # 8. If Reddit was rate-limiting us heavily (or for any reason we got back
    # very little fresh data), augment from the store so the dashboard isn't
    # empty. Threshold: less than 1/3 of what's in store, OR fewer than 10.
    if persist_to_store and (_REDDIT_THROTTLED or len(mentions) < 10):
        try:
            from store import get_campaign_mentions
            stored_rows = get_campaign_mentions(campaign)
            if stored_rows:
                _tick(
                    f"Live fetch returned only {len(mentions)} — augmenting "
                    f"with {len(stored_rows)} stored mentions"
                )
                seen_ids = {(m.id, m.kind) for m in mentions}
                allowlist_mode = (flt.get("mode") or "any").lower() == "allowlist"
                for r in stored_rows:
                    key = (r["reddit_id"], r["kind"])
                    if key in seen_ids:
                        continue
                    if allowlist_mode and not _passes_subreddit_filter(r.get("subreddit") or ""):
                        continue
                    m = Mention(
                        id=r["reddit_id"],
                        kind=r["kind"],
                        subreddit=r.get("subreddit") or "",
                        author=r.get("author") or "",
                        created_utc=float(r["created_utc"]),
                        title=r.get("title") or "",
                        body=r.get("body") or "",
                        url=r.get("url") or "",
                        score=int(r.get("score") or 0),
                        num_comments=int(r.get("num_comments") or 0),
                        parent_id=r.get("parent_id") or "",
                        parent_title=r.get("parent_title") or "",
                        parent_url=r.get("parent_url") or "",
                        parent_in_posts=bool(r.get("parent_in_posts") or 0),
                        parent_sentiment_label=r.get("parent_sentiment_label") or "",
                        parent_sentiment=float(r.get("parent_sentiment") or 0.0),
                        source=r.get("source") or "",
                        sentiment_label=r.get("sentiment_label") or "neutral",
                        sentiment=float(r.get("sentiment") or 0.0),
                        issue_summary=r.get("issue_summary") or "",
                        action_type=r.get("action_type") or "",
                    )
                    mentions.append(m)
                    seen_ids.add(key)
                mentions.sort(key=lambda m: m.created_utc, reverse=True)
                # Re-apply caps after augmentation: max_posts on posts, max_total on the total.
                if max_posts is not None:
                    posts_only = [m for m in mentions if m.kind == "post"]
                    others = [m for m in mentions if m.kind != "post"]
                    if len(posts_only) > max_posts:
                        kept_posts = posts_only[:max_posts]
                        kept_post_ids = {m.id for m in kept_posts}
                        # Only keep comments whose parent is a kept post.
                        kept_comments = [
                            m for m in others
                            if not m.parent_id or m.parent_id in kept_post_ids
                        ]
                        mentions = sorted(
                            kept_posts + kept_comments,
                            key=lambda m: m.created_utc,
                            reverse=True,
                        )
                if max_total_mentions is not None and len(mentions) > max_total_mentions:
                    mentions = mentions[:max_total_mentions]
        except Exception as e:
            print(f"[store] augment failed: {type(e).__name__}: {e}")

    result = FetchResult(mentions=mentions)
    cache_path.write_text(
        json.dumps(
            {
                "fetched_at": result.fetched_at.isoformat(),
                "mentions": [m.__dict__ for m in mentions],
            },
            default=str,
        )
    )
    return result


def to_dataframe(result: FetchResult) -> pd.DataFrame:
    cols = [
        "id",
        "kind",
        "source",
        "subreddit",
        "author",
        "created_at",
        "title",
        "body",
        "url",
        "score",
        "num_comments",
        "parent_id",
        "parent_title",
        "parent_url",
        "parent_sentiment_label",
        "parent_sentiment",
        "parent_in_posts",
        "sentiment",
        "sentiment_label",
        "issue_summary",
        "action_type",
    ]
    if not result.mentions:
        return pd.DataFrame(columns=cols)
    rows = []
    for m in result.mentions:
        rows.append(
            {
                "id": m.id,
                "kind": m.kind,
                "source": m.source,
                "subreddit": m.subreddit,
                "author": m.author,
                "created_at": m.created_dt,
                "title": m.title,
                "body": m.body,
                "url": m.url,
                "score": m.score,
                "num_comments": m.num_comments,
                "parent_id": m.parent_id,
                "parent_title": m.parent_title,
                "parent_url": m.parent_url,
                "parent_sentiment_label": m.parent_sentiment_label,
                "parent_sentiment": m.parent_sentiment,
                "parent_in_posts": m.parent_in_posts,
                "sentiment": m.sentiment,
                "sentiment_label": m.sentiment_label,
                "issue_summary": m.issue_summary,
                "action_type": m.action_type,
            }
        )
    df = pd.DataFrame(rows)
    df = df.sort_values("created_at", ascending=False).reset_index(drop=True)
    return df


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fetch Reddit mentions for the configured brand campaign.")
    parser.add_argument(
        "--time", default="month", choices=["hour", "day", "week", "month", "year", "all"]
    )
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--no-comments", action="store_true")
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    result = fetch_mentions(
        limit_per_query=args.limit,
        time_filter=args.time,
        include_comments=not args.no_comments,
        cache_ttl_seconds=0 if args.no_cache else 900,
    )
    df = to_dataframe(result)
    print(f"Fetched {len(df)} mentions across {df['subreddit'].nunique()} subreddits.")
    if not df.empty:
        print(
            df[
                [
                    "created_at",
                    "kind",
                    "subreddit",
                    "sentiment_label",
                    "issue_summary",
                    "title",
                ]
            ]
            .head(25)
            .to_string()
        )
