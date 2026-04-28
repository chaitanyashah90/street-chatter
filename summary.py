"""Per-campaign sentiment summary via `claude -p`.

Given a DataFrame of mentions (posts + comments) for one campaign, produces a
short markdown analysis covering: overview, key positives, key negatives,
opportunities, risks, and sentiment movement over time.

Cached on disk by (campaign, mention_count, latest_created_utc) so it doesn't
re-run on every page load.
"""
from __future__ import annotations

import json
import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

CLAUDE_CMD = "claude"
SUMMARY_TIMEOUT_S = 180
SUMMARY_CACHE_PATH = Path(__file__).parent / "data" / "summary_cache.json"
SUMMARY_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)


SYSTEM_TMPL = """You are a brand-monitoring analyst writing an executive summary for {subject_label} based on Reddit mentions.

Subject context:
{subject_description}

You will receive a list of mentions (posts + comments) with their sentiment labels and dates. Produce a SHORT, scannable markdown summary in EXACTLY this structure:

## Overview
2-3 sentences on what people are predominantly talking about. Cite the dominant themes (e.g. delivery experience, product quality, deals, brand-vs-brand).

## Key positives
3-5 short bullets. Each bullet is a specific theme with implicit volume (e.g. "Multiple users praise creatine mixability"). Cite a representative phrase from the data when useful.

## Key negatives
3-5 short bullets, same shape. Be specific about WHAT users are unhappy with — "delivery delays via Delhivery courier", not "shipping issues".

## Opportunities
2-4 bullets. What is the brand doing well that it can amplify? What gaps in competitors does the data expose? What demographic is engaging that could be deepened? Be tactical.

## Risks
2-4 bullets. Patterns that, if unaddressed, will compound. Be specific (e.g. "repeated complaints about expired stock from a single warehouse" not "quality problems").

## Sentiment movement
1-2 sentences comparing recent ~30 days vs the prior period. Call out direction (improving / stable / worsening) with the strongest signal driving it. If the dataset is too small for movement analysis, say so.

RULES:
- Use markdown headings exactly as specified.
- Total length: 350-700 words.
- Bullets short and tight. No filler. No "It's important to note that…".
- Quote at most one short phrase per bullet, in italics.
- Do not invent numbers. If you cite "many users", make sure the data supports it.
- Output the markdown directly. No preamble, no JSON wrapper, no code fence.
"""


def _build_user_payload(df: pd.DataFrame, max_items: int = 250) -> str:
    """Serialize the mentions for the LLM. Truncate to max_items if huge."""
    if df.empty:
        return "No mentions in this window."
    df = df.sort_values("created_at", ascending=False).head(max_items).copy()
    lines = [
        f"Total mentions: {len(df)} (showing the {min(len(df), max_items)} most recent below).",
        f"Posts: {(df['kind']=='post').sum()}, Comments: {(df['kind']=='comment').sum()}.",
        "",
        "Sentiment counts:",
        f"  positive: {(df['sentiment_label']=='positive').sum()}",
        f"  neutral:  {(df['sentiment_label']=='neutral').sum()}",
        f"  negative: {(df['sentiment_label']=='negative').sum()}",
        "",
        "Mentions (newest first; format: [date] [kind] r/sub | sentiment | issue_summary | excerpt):",
    ]
    for _, m in df.iterrows():
        date_str = m["created_at"].strftime("%Y-%m-%d") if hasattr(m["created_at"], "strftime") else str(m["created_at"])[:10]
        title = (m.get("title") or "").strip().replace("\n", " ")
        body = (m.get("body") or "").strip().replace("\n", " ")
        excerpt = (title + " · " + body)[:280]
        lines.append(
            f"- [{date_str}] [{m['kind']}] r/{m.get('subreddit','')} | "
            f"{m.get('sentiment_label','neutral')} | "
            f"{m.get('issue_summary','') or '—'} | {excerpt}"
        )
    return "\n".join(lines)


def _cache_load() -> dict:
    if not SUMMARY_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(SUMMARY_CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _cache_save(d: dict) -> None:
    tmp = SUMMARY_CACHE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d))
    tmp.replace(SUMMARY_CACHE_PATH)


def _cache_key(campaign: str, df: pd.DataFrame) -> str:
    if df.empty:
        return f"{campaign}::empty"
    n = len(df)
    latest = df["created_at"].max()
    if hasattr(latest, "isoformat"):
        latest_s = latest.isoformat()
    else:
        latest_s = str(latest)
    return f"{campaign}::{n}::{latest_s}"


def summarize(
    campaign: str,
    df: pd.DataFrame,
    subject_label: str,
    subject_description: str,
    use_cache: bool = True,
    cmd: str = CLAUDE_CMD,
    timeout: int = SUMMARY_TIMEOUT_S,
) -> tuple[str, str]:
    """Generate the campaign summary. Returns (markdown, cache_status).

    cache_status is one of "fresh", "cached".
    """
    cache = _cache_load() if use_cache else {}
    key = _cache_key(campaign, df)
    if use_cache and key in cache:
        return cache[key]["markdown"], "cached"

    if df.empty:
        md = "_No mentions in the current window._"
        return md, "fresh"

    system = SYSTEM_TMPL.format(
        subject_label=subject_label, subject_description=subject_description
    )
    user_payload = _build_user_payload(df)
    full_prompt = system + "\n\n---\n\nDATA:\n" + user_payload

    proc = subprocess.run(
        [cmd, "-p", full_prompt, "--output-format", "text"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        return (
            f"_Summary generation failed (rc={proc.returncode}): "
            f"{(proc.stderr or proc.stdout)[:300]}_",
            "fresh",
        )
    md = proc.stdout.strip()
    # Strip optional markdown fence.
    if md.startswith("```"):
        md = md.strip("`").strip()
        if md.lower().startswith("markdown"):
            md = md[8:].strip()

    if use_cache:
        cache[key] = {
            "markdown": md,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        _cache_save(cache)
    return md, "fresh"
