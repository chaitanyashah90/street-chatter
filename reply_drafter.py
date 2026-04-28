"""Draft Reddit replies for actionable mentions, per campaign.

Calls the local `claude` CLI in headless mode (`claude -p`) so the user's
Claude Pro or Max plan auth is reused — no API key needed.

Each campaign has its own guidance template in prompts/templates/. The
template contains $-style placeholders (brand_name, brand_keyword, persona,
etc.) that are filled from config.json on every draft. The rendered version
is also written to prompts/rendered/{campaign}_reply_guidance.md so the
marketer can inspect what's being sent to the model.
"""
from __future__ import annotations

import json
import string
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from config import load_config, primary_brand_keyword, template_vars

CLAUDE_CMD = "claude"
DRAFT_TIMEOUT_S = 120
PROMPTS_DIR = Path(__file__).parent / "prompts"
TEMPLATES_DIR = PROMPTS_DIR / "templates"
RENDERED_DIR = PROMPTS_DIR / "rendered"

GUIDANCE_FILES = {
    "brand": "brand_reply_guidance.md",
    "primary_competitor": "primary_competitor_reply_guidance.md",
    "secondary_competitor": "secondary_competitor_reply_guidance.md",
    "generic_search": "generic_search_reply_guidance.md",
}


@dataclass
class ReplyVariant:
    reply: str
    tone: str = ""
    mentions: str = ""  # the brand keyword if mentioned, else "none"


def _build_mode_descriptions(cfg: dict) -> dict[str, str]:
    """Build the per-mode 'what to do' instructions injected into the user prompt."""
    brand_name = cfg["brand"].get("name", "the brand")
    brand_kw = primary_brand_keyword(cfg)
    primary = cfg.get("competitors", {}).get("primary", []) or []
    secondary = cfg.get("competitors", {}).get("secondary", []) or []
    all_competitor_names = [c["name"] for c in primary + secondary if c.get("name")]
    competitor_list_text = ", ".join(all_competitor_names) or "the competitor"

    return {
        "defend_brand_on_negative_post": (
            f"The OP is unhappy with {brand_name}. Acknowledge their issue, then offer "
            "a balanced personal counter-experience that nudges them and other "
            "readers toward a more neutral or positive view. Don't deny the OP's "
            "experience. Don't oversell. Suggest a concrete next step (contact "
            "support with order ID, share a screenshot, etc.) when relevant."
        ),
        "double_down_on_positive_post": (
            f"The OP is praising {brand_name}. Validate their experience with a short, "
            "specific personal echo (a product you also liked, a deal you got, a "
            "delivery that went smoothly). Don't be over-the-top. One genuine "
            "user-voice line is enough."
        ),
        "vouch_on_legit_question": (
            f"The OP is asking 'is {brand_name} legit?' or similar. Give a short "
            "personal vouch with one concrete detail. No marketing speak."
        ),
        "suggest_brand_as_alternative": (
            f"The OP is asking for a recommendation. Casually mention `{brand_kw}` "
            "as an option, phrased as personal experience, not PR copy. One line is enough."
        ),
        "suggest_alternative_to_competitor_complaint": (
            f"The OP is complaining about a competitor ({competitor_list_text}). "
            f"Acknowledge their issue, then casually mention you switched to `{brand_kw}` "
            "and how it's been. Mention it once."
        ),
        "softly_mention_alternative_to_positive_competitor_review": (
            "The OP is praising a competitor. Don't trash their pick. Just casually "
            f"mention you also rotate `{brand_kw}` alongside it, or you've found it "
            "works for X scenario. Soft, additive, not contrarian. Mention it once."
        ),
        "suggest_alternative_to_complaint": (
            "The OP is complaining about something they used. Acknowledge, then "
            f"mention `{brand_kw}` as an alternative. Mention it once."
        ),
        "validate_and_optionally_mention_alternative": (
            f"The OP is happy. Validate, and only mention `{brand_kw}` if the natural "
            "conversation leads there. Otherwise, no plug."
        ),
        "expert_answer": (
            "Be a helpful expert. Answer the OP's actual question with real "
            f"domain knowledge. Mention `{brand_kw}` only if it genuinely fits — "
            "credibility compounds when you're useful first."
        ),
    }


def _load_guidance(campaign: str, cfg: dict | None = None) -> str:
    """Render the guidance template for `campaign` from config and return it.

    Side effect: writes the rendered output to prompts/rendered/{campaign}_reply_guidance.md
    so the marketer can inspect what's being sent to the model.
    """
    if cfg is None:
        cfg = load_config()
    fname = GUIDANCE_FILES.get(campaign, GUIDANCE_FILES["brand"])
    template_path = TEMPLATES_DIR / fname
    if not template_path.exists():
        raise FileNotFoundError(
            f"Reply guidance template not found: {template_path}. "
            f"Restore it from prompts/templates/ in the repo."
        )
    template = string.Template(template_path.read_text(encoding="utf-8"))
    rendered = template.safe_substitute(template_vars(cfg, campaign=campaign))

    RENDERED_DIR.mkdir(parents=True, exist_ok=True)
    (RENDERED_DIR / fname).write_text(rendered, encoding="utf-8")
    return rendered


# ---------------------------------------------------------------------------
# "Actionable" rules — different per campaign
# ---------------------------------------------------------------------------


def is_actionable(post_row: pd.Series, related_comments: pd.DataFrame, campaign: str) -> bool:
    """Decide whether a post is worth drafting a reply for.

    Always: skip off_topic posts.

    Brand:
      - negative complaint → defend / nudge to neutral
      - positive praise → double down with validating reply

    Primary / Secondary competitor:
      - recommendation_request → suggest the brand
      - complaint about competitor → suggest alternative
      - praise of competitor → soft mention of alternative

    Generic search:
      - recommendation_request / complaint / praise / general_discussion
        with low engagement → expert reply (may or may not plug)
    """
    action = str(post_row.get("action_type") or "").lower()
    label = str(post_row.get("sentiment_label") or "neutral").lower()

    if action == "off_topic":
        return False

    if campaign == "brand":
        if action == "complaint" or label == "negative":
            return True
        if action == "praise" or label == "positive":
            return True
        if action == "recommendation_request":
            return True
        return False

    if campaign in ("primary_competitor", "secondary_competitor"):
        if action in ("recommendation_request", "complaint", "praise"):
            return True
        return False

    if campaign == "generic_search":
        if action in ("recommendation_request", "complaint", "praise", "general_discussion"):
            # Cap by engagement to focus on posts where a reply still has reach.
            n_comments = int(post_row.get("num_comments") or 0)
            return n_comments < 12
        return False

    return False


def reply_mode_for(post_row: pd.Series, campaign: str) -> str:
    """Return a label describing what this draft should actually do."""
    action = str(post_row.get("action_type") or "").lower()
    label = str(post_row.get("sentiment_label") or "neutral").lower()

    if campaign == "brand":
        if action == "complaint" or label == "negative":
            return "defend_brand_on_negative_post"
        if action == "praise" or label == "positive":
            return "double_down_on_positive_post"
        if action == "recommendation_request":
            return "vouch_on_legit_question"
        return "expert_answer"

    if campaign in ("primary_competitor", "secondary_competitor"):
        if action == "recommendation_request":
            return "suggest_brand_as_alternative"
        if action == "complaint":
            return "suggest_alternative_to_competitor_complaint"
        if action == "praise":
            return "softly_mention_alternative_to_positive_competitor_review"
        return "expert_answer"

    if campaign == "generic_search":
        if action == "recommendation_request":
            return "suggest_brand_as_alternative"
        if action == "complaint":
            return "suggest_alternative_to_complaint"
        if action == "praise":
            return "validate_and_optionally_mention_alternative"
        return "expert_answer"

    return "expert_answer"


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def _build_user_prompt(
    post_row: pd.Series,
    related_comments: pd.DataFrame,
    campaign: str,
    cfg: dict,
) -> str:
    title = str(post_row.get("title") or "").strip()
    body = str(post_row.get("body") or "").strip()
    subreddit = str(post_row.get("subreddit") or "")
    issue = str(post_row.get("issue_summary") or "")
    sentiment = str(post_row.get("sentiment_label") or "")
    action_type = str(post_row.get("action_type") or "")

    mode = reply_mode_for(post_row, campaign)
    mode_desc = _build_mode_descriptions(cfg).get(mode, "")
    brand_kw = primary_brand_keyword(cfg)

    chunks: list[str] = [
        f"REPLY MODE: {mode}",
        f"WHAT TO DO: {mode_desc}",
        "",
        f"SUBREDDIT: r/{subreddit}",
        f"POST TITLE: {title}",
        f"POST SENTIMENT (toward subject): {sentiment}",
        f"POST ACTION TYPE: {action_type}",
        f"POST BODY:\n{body or '(empty)'}",
    ]
    if issue:
        chunks.append(f"DETECTED ISSUE: {issue}")

    if related_comments is not None and not related_comments.empty:
        chunks.append("EXISTING COMMENTS ON THIS POST (top 5 by recency):")
        sample = related_comments.head(5)
        for _, c in sample.iterrows():
            comment_body = str(c.get("body") or "").strip().replace("\n", " ")
            if len(comment_body) > 240:
                comment_body = comment_body[:237] + "…"
            label = c.get("sentiment_label") or "neutral"
            author = c.get("author") or "[deleted]"
            chunks.append(f"  - [{label}] {author}: {comment_body}")

    chunks.append(
        "\nDraft ONE reply per the persona spec above and the REPLY MODE. "
        'Return JSON only, with this exact shape: '
        f'{{"reply": "...", "tone": "...", "mentions": "{brand_kw}|none"}}'
    )
    return "\n\n".join(chunks)


def draft_reply(
    post_row: pd.Series,
    related_comments: pd.DataFrame,
    campaign: str = "brand",
    cmd: str = CLAUDE_CMD,
    timeout: int = DRAFT_TIMEOUT_S,
) -> tuple[ReplyVariant, str]:
    """Generate ONE reply via `claude -p` for a single post.

    Returns (variant, raw_stdout). Raises subprocess.CalledProcessError on
    CLI failure or json.JSONDecodeError if the model output isn't parseable.
    """
    cfg = load_config()
    persona = _load_guidance(campaign, cfg=cfg)
    user_prompt = _build_user_prompt(post_row, related_comments, campaign, cfg)
    full_prompt = persona + "\n\n---\n\n" + user_prompt
    proc = subprocess.run(
        [cmd, "-p", full_prompt, "--output-format", "text"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    raw = proc.stdout.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    try:
        start = raw.index("{")
        end = raw.rindex("}") + 1
        payload = json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError) as e:
        raise json.JSONDecodeError(
            f"Couldn't parse JSON from claude output: {e}", raw, 0
        )

    if "reply" in payload:
        v = payload
    else:
        items = payload.get("variants") or []
        v = next((it for it in items if it.get("reply")), {})
        if not v:
            raise json.JSONDecodeError(
                "No reply found in model output (no 'reply' key, no non-empty variants)",
                raw,
                0,
            )

    variant = ReplyVariant(
        reply=str(v.get("reply", "")).strip(),
        tone=str(v.get("tone", "")),
        mentions=str(v.get("mentions", "")),
    )
    return variant, raw
