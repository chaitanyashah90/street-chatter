"""Persistent SQLite store for Reddit mentions across campaigns.

A `mention` is uniquely identified by (reddit_id, kind). The same mention can
belong to multiple campaigns (e.g. a comment that mentions both the brand
and a competitor). Tagging is via `campaign_mentions`.

On each fetch, we:
  1. Look up `last_fetched_at` per campaign to know how far back to scrape.
  2. Upsert every fetched mention into `mentions` (re-scoring labels if changed).
  3. Tag the campaign in `campaign_mentions`.

The dashboard reads from the store, not just the latest fetch — so we render
historical data even if the user shrinks the time window.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, Iterator

DB_PATH = Path(__file__).parent / "data" / "monitor.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# All Mention dataclass fields we persist. Keep in sync with Mention.
MENTION_COLS = (
    "reddit_id",
    "kind",
    "fullname",
    "subreddit",
    "author",
    "created_utc",
    "title",
    "body",
    "url",
    "score",
    "num_comments",
    "parent_id",
    "parent_title",
    "parent_url",
    "parent_in_posts",
    "parent_sentiment_label",
    "parent_sentiment",
    "source",
    "sentiment_label",
    "sentiment",
    "issue_summary",
    "action_type",
    "first_seen_utc",
    "last_seen_utc",
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS mentions (
    reddit_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    fullname TEXT NOT NULL,
    subreddit TEXT,
    author TEXT,
    created_utc REAL NOT NULL,
    title TEXT,
    body TEXT,
    url TEXT,
    score INTEGER,
    num_comments INTEGER,
    parent_id TEXT,
    parent_title TEXT,
    parent_url TEXT,
    parent_in_posts INTEGER DEFAULT 0,
    parent_sentiment_label TEXT,
    parent_sentiment REAL,
    source TEXT,
    sentiment_label TEXT,
    sentiment REAL,
    issue_summary TEXT,
    action_type TEXT,
    first_seen_utc REAL NOT NULL,
    last_seen_utc REAL NOT NULL,
    PRIMARY KEY (reddit_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_mentions_created ON mentions(created_utc DESC);
CREATE INDEX IF NOT EXISTS idx_mentions_kind ON mentions(kind);

CREATE TABLE IF NOT EXISTS campaign_mentions (
    campaign TEXT NOT NULL,
    reddit_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    matched_query TEXT,
    first_tagged_utc REAL NOT NULL,
    PRIMARY KEY (campaign, reddit_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_campaign_mentions_campaign ON campaign_mentions(campaign);

CREATE TABLE IF NOT EXISTS campaign_runs (
    campaign TEXT PRIMARY KEY,
    last_fetched_at_utc REAL NOT NULL,
    last_fetched_count INTEGER NOT NULL,
    last_new_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS post_actions (
    campaign TEXT NOT NULL,
    post_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | posted | discarded
    draft_text TEXT,
    draft_tone TEXT,
    draft_mentions TEXT,
    posted_at REAL,
    discarded_at REAL,
    updated_at REAL NOT NULL,
    last_post_url TEXT,
    last_error TEXT,
    PRIMARY KEY (campaign, post_id)
);

CREATE INDEX IF NOT EXISTS idx_post_actions_status ON post_actions(campaign, status);
"""


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _to_row(m, now_utc: float) -> dict:
    """Convert a Mention dataclass instance to a row dict."""
    fullname = ("t3_" if m.kind == "post" else "t1_") + m.id
    return {
        "reddit_id": m.id,
        "kind": m.kind,
        "fullname": fullname,
        "subreddit": m.subreddit,
        "author": m.author,
        "created_utc": m.created_utc,
        "title": m.title,
        "body": m.body,
        "url": m.url,
        "score": m.score,
        "num_comments": m.num_comments,
        "parent_id": m.parent_id,
        "parent_title": m.parent_title,
        "parent_url": m.parent_url,
        "parent_in_posts": 1 if m.parent_in_posts else 0,
        "parent_sentiment_label": m.parent_sentiment_label,
        "parent_sentiment": m.parent_sentiment,
        "source": m.source,
        "sentiment_label": m.sentiment_label,
        "sentiment": m.sentiment,
        "issue_summary": m.issue_summary,
        "action_type": m.action_type,
        "first_seen_utc": now_utc,
        "last_seen_utc": now_utc,
    }


def upsert_mentions(campaign: str, mentions: list, matched_queries: dict | None = None) -> tuple[int, int]:
    """Insert or update mentions and tag them with the campaign.

    Returns (new_count, updated_count).
    """
    if not mentions:
        return (0, 0)
    matched_queries = matched_queries or {}
    now = time.time()
    new_count = 0
    updated_count = 0

    cols = ",".join(MENTION_COLS)
    placeholders = ",".join("?" * len(MENTION_COLS))
    # Update everything except first_seen_utc on conflict.
    update_cols = [c for c in MENTION_COLS if c != "first_seen_utc"]
    update_clause = ",".join(f"{c}=excluded.{c}" for c in update_cols)
    sql = (
        f"INSERT INTO mentions ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(reddit_id, kind) DO UPDATE SET {update_clause}"
    )

    with _conn() as conn:
        for m in mentions:
            if not m.id:
                continue
            row = _to_row(m, now_utc=now)
            # Probe whether it already exists, to count new vs updated.
            existing = conn.execute(
                "SELECT 1 FROM mentions WHERE reddit_id=? AND kind=?",
                (m.id, m.kind),
            ).fetchone()
            if existing is None:
                new_count += 1
            else:
                updated_count += 1
            conn.execute(sql, [row[c] for c in MENTION_COLS])
            conn.execute(
                """
                INSERT INTO campaign_mentions (campaign, reddit_id, kind, matched_query, first_tagged_utc)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(campaign, reddit_id, kind) DO NOTHING
                """,
                (
                    campaign,
                    m.id,
                    m.kind,
                    matched_queries.get((m.id, m.kind), ""),
                    now,
                ),
            )
        # Bump campaign_runs.
        conn.execute(
            """
            INSERT INTO campaign_runs (campaign, last_fetched_at_utc, last_fetched_count, last_new_count)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(campaign) DO UPDATE SET
                last_fetched_at_utc=excluded.last_fetched_at_utc,
                last_fetched_count=excluded.last_fetched_count,
                last_new_count=excluded.last_new_count
            """,
            (campaign, now, len(mentions), new_count),
        )
    return new_count, updated_count


def last_fetched_at(campaign: str) -> float | None:
    """When did we last successfully fetch this campaign? Unix timestamp or None."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT last_fetched_at_utc FROM campaign_runs WHERE campaign=?",
            (campaign,),
        ).fetchone()
        return float(row[0]) if row else None


def latest_created_utc(campaign: str) -> float | None:
    """Most recent mention's created_utc for this campaign. Used for incremental hint."""
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT MAX(m.created_utc) FROM mentions m
            JOIN campaign_mentions cm ON m.reddit_id=cm.reddit_id AND m.kind=cm.kind
            WHERE cm.campaign=?
            """,
            (campaign,),
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None


def get_campaign_mentions(
    campaign: str,
    since_utc: float | None = None,
    until_utc: float | None = None,
) -> list[dict]:
    """Read all stored mentions for a campaign in [since_utc, until_utc]."""
    sql = (
        """
        SELECT m.* FROM mentions m
        JOIN campaign_mentions cm ON m.reddit_id=cm.reddit_id AND m.kind=cm.kind
        WHERE cm.campaign=?
        """
    )
    params: list = [campaign]
    if since_utc is not None:
        sql += " AND m.created_utc >= ?"
        params.append(since_utc)
    if until_utc is not None:
        sql += " AND m.created_utc <= ?"
        params.append(until_utc)
    sql += " ORDER BY m.created_utc DESC"
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_known_ids(campaign: str) -> set[tuple[str, str]]:
    """Return set of (reddit_id, kind) already known for this campaign — useful for dedup."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT reddit_id, kind FROM campaign_mentions WHERE campaign=?",
            (campaign,),
        ).fetchall()
        return {(r["reddit_id"], r["kind"]) for r in rows}


def get_action(campaign: str, post_id: str) -> dict | None:
    """Return the saved action row for a (campaign, post_id), or None."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM post_actions WHERE campaign=? AND post_id=?",
            (campaign, post_id),
        ).fetchone()
        return dict(row) if row else None


def list_actions(campaign: str, status: str | None = None) -> list[dict]:
    """List all action rows for a campaign, optionally filtered by status."""
    sql = "SELECT * FROM post_actions WHERE campaign=?"
    params: list = [campaign]
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY updated_at DESC"
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def upsert_action(
    campaign: str,
    post_id: str,
    *,
    status: str | None = None,
    draft_text: str | None = None,
    draft_tone: str | None = None,
    draft_mentions: str | None = None,
    last_post_url: str | None = None,
    last_error: str | None = None,
) -> None:
    """Upsert a post_actions row. Only provided fields are updated; the rest
    keep their existing values (or take their schema defaults on insert).
    """
    now = time.time()
    existing = get_action(campaign, post_id) or {}

    new_status = status if status is not None else existing.get("status", "pending")
    new_draft_text = draft_text if draft_text is not None else existing.get("draft_text")
    new_draft_tone = draft_tone if draft_tone is not None else existing.get("draft_tone")
    new_draft_mentions = (
        draft_mentions if draft_mentions is not None else existing.get("draft_mentions")
    )
    new_last_url = last_post_url if last_post_url is not None else existing.get("last_post_url")
    new_last_err = last_error if last_error is not None else existing.get("last_error")

    posted_at = existing.get("posted_at")
    discarded_at = existing.get("discarded_at")
    if status == "posted" and not posted_at:
        posted_at = now
    if status == "discarded" and not discarded_at:
        discarded_at = now
    if status == "pending":
        # Re-opening (un-discard / un-post) clears completion timestamps.
        posted_at = None
        discarded_at = None

    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO post_actions
                (campaign, post_id, status, draft_text, draft_tone, draft_mentions,
                 posted_at, discarded_at, updated_at, last_post_url, last_error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(campaign, post_id) DO UPDATE SET
                status=excluded.status,
                draft_text=excluded.draft_text,
                draft_tone=excluded.draft_tone,
                draft_mentions=excluded.draft_mentions,
                posted_at=excluded.posted_at,
                discarded_at=excluded.discarded_at,
                updated_at=excluded.updated_at,
                last_post_url=excluded.last_post_url,
                last_error=excluded.last_error
            """,
            (
                campaign,
                post_id,
                new_status,
                new_draft_text,
                new_draft_tone,
                new_draft_mentions,
                posted_at,
                discarded_at,
                now,
                new_last_url,
                new_last_err,
            ),
        )


def stats() -> dict:
    """Quick stats for diagnostics / dashboard footer."""
    with _conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM mentions").fetchone()[0]
        per_camp = conn.execute(
            "SELECT campaign, COUNT(*) FROM campaign_mentions GROUP BY campaign"
        ).fetchall()
        latest = conn.execute(
            "SELECT MAX(last_seen_utc) FROM mentions"
        ).fetchone()[0]
    return {
        "total_mentions": total,
        "per_campaign": {r[0]: r[1] for r in per_camp},
        "last_seen_utc": latest,
    }
