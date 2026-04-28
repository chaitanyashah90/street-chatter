"""User-facing configuration for the Reddit monitor.

A single `config.json` in the repo root holds all brand-specific data: the
marketer's brand identity, voice, competitors, search keywords, subreddit
filter, and campaign caps.

`config.example.json` is committed to the repo as a working template (with a
fake "Acme Protein" brand). Runtime `config.json` is git-ignored — that's
where the marketer's actual brand config lives.

Public helpers:
  - load_config()                → dict (creates config.json from example on first run)
  - save_config(cfg)             → writes back to disk
  - is_complete(cfg)             → True iff the config has the minimum the app needs
  - build_campaigns(cfg)         → dict[campaign_key, campaign_dict] in the same shape
                                   the rest of the codebase already consumes
                                   (label, queries, subject_label, subject_description,
                                   max_posts, max_total_mentions)
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
EXAMPLE_PATH = ROOT / "config.example.json"

CAMPAIGN_KEYS = ("brand", "primary_competitor", "secondary_competitor", "generic_search")


def primary_brand_keyword(cfg: dict[str, Any]) -> str:
    """The canonical lowercase keyword used in the JSON `mentions` field.

    Defaults to the first search keyword on the brand. Falls back to the
    lowercased brand name.
    """
    brand = cfg.get("brand") or {}
    kws = [k.strip() for k in (brand.get("search_keywords") or []) if k and k.strip()]
    if kws:
        return kws[0].lower()
    return (brand.get("name") or "brand").lower().strip()


_DEFAULT_CAMPAIGN_GUIDANCE_FALLBACK = "(no campaign-specific guidance configured)"


def campaign_guidance_for(cfg: dict[str, Any], campaign: str) -> str:
    """Per-campaign prompt guidance. Falls back to a placeholder if missing."""
    cg = cfg.get("campaign_guidance") or {}
    return (cg.get(campaign) or "").strip() or _DEFAULT_CAMPAIGN_GUIDANCE_FALLBACK


def template_vars(cfg: dict[str, Any], campaign: str | None = None) -> dict[str, str]:
    """Flat dict of ${...} substitutions for the prompt templates.

    If `campaign` is given, also includes a `campaign_specific_guidance` key
    pulled from cfg.campaign_guidance[campaign].
    """
    brand = cfg.get("brand") or {}
    voice = brand.get("voice") or {}
    competitors = cfg.get("competitors") or {}
    generic = cfg.get("generic_search") or {}

    primary_names = [c["name"] for c in competitors.get("primary", []) if c.get("name")]
    secondary_names = [c["name"] for c in competitors.get("secondary", []) if c.get("name")]
    all_competitors = primary_names + secondary_names

    tone_rules = voice.get("tone_rules") or []
    examples = voice.get("examples") or []
    hooks = generic.get("category_hooks") or []

    out = {
        "brand_name": brand.get("name", "the brand"),
        "brand_keyword": primary_brand_keyword(cfg),
        "brand_url": brand.get("url", ""),
        "brand_description": brand.get("description", "").strip()
            or f"{brand.get('name', 'The brand')} is the marketer's brand.",
        "what_we_stand_for": brand.get("what_we_stand_for", "").strip()
            or "Authentic value to customers.",
        "persona": voice.get("persona", "Authentic everyday user, not a marketer."),
        "tone_rules": "\n".join(f"- {r}" for r in tone_rules) or "- Be authentic. No marketing speak.",
        "voice_examples": "\n".join(f"> {e}" for e in examples) or "> (no voice samples configured)",
        "competitor_list": ", ".join(all_competitors) or "the competitor",
        "primary_competitor_list": ", ".join(primary_names) or "the competitor",
        "secondary_competitor_list": ", ".join(secondary_names) or "the competitor",
        "category_hooks": "\n".join(f"- {h}" for h in hooks) or "- (no category hooks configured)",
    }
    if campaign is not None:
        out["campaign_specific_guidance"] = campaign_guidance_for(cfg, campaign)
    return out


def load_config() -> dict[str, Any]:
    """Load config.json. If missing, seed it from config.example.json."""
    if not CONFIG_PATH.exists():
        if not EXAMPLE_PATH.exists():
            raise FileNotFoundError(
                f"Neither {CONFIG_PATH} nor {EXAMPLE_PATH} exists. "
                "Restore config.example.json from the repo."
            )
        shutil.copyfile(EXAMPLE_PATH, CONFIG_PATH)
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: dict[str, Any]) -> None:
    """Atomically write the config back to disk."""
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(CONFIG_PATH)


def is_complete(cfg: dict[str, Any]) -> tuple[bool, list[str]]:
    """Validate the config has the minimum needed to run.

    Returns (ok, missing_fields). Used by app.py to gate the dashboard behind
    the Setup page on first run.
    """
    missing: list[str] = []
    brand = cfg.get("brand") or {}
    if not (brand.get("name") or "").strip():
        missing.append("brand.name")
    if not [k for k in (brand.get("search_keywords") or []) if k.strip()]:
        missing.append("brand.search_keywords")
    return (len(missing) == 0, missing)


def _quote_keywords(keywords: list[str]) -> list[str]:
    """Wrap each keyword in double-quotes for Reddit's exact-phrase search."""
    return [f'"{kw.strip()}"' for kw in keywords if kw and kw.strip()]


def _competitor_subject(group: list[dict]) -> tuple[str, str]:
    """Build a subject_label and description for a competitor campaign."""
    names = [c["name"] for c in group if c.get("name")]
    label = " / ".join(names) if names else "Competitor"
    desc = (
        f"{label} "
        + ("are competitors" if len(names) > 1 else "is a competitor")
        + " of the user's brand. Sentiment is about whichever competitor the "
        "mention is actually discussing. If multiple are mentioned, score the "
        "more prominently discussed one."
    )
    return label, desc


def build_campaigns(cfg: dict[str, Any]) -> dict[str, dict]:
    """Produce the CAMPAIGNS dict the rest of the codebase consumes.

    Same shape that reddit_monitor.py used to define inline:
        {label, queries, subject_label, subject_description, max_posts, max_total_mentions}
    """
    brand = cfg["brand"]
    brand_name = brand["name"]
    brand_kws = brand.get("search_keywords", [])

    primary = cfg.get("competitors", {}).get("primary", []) or []
    secondary = cfg.get("competitors", {}).get("secondary", []) or []

    generic = cfg.get("generic_search", {}) or {}
    caps = cfg.get("campaign_caps", {}) or {}

    def _cap(key: str, field: str, default):
        return (caps.get(key) or {}).get(field, default)

    primary_label, primary_desc = _competitor_subject(primary)
    secondary_label, secondary_desc = _competitor_subject(secondary)

    primary_kws = [kw for c in primary for kw in c.get("search_keywords", [])]
    secondary_kws = [kw for c in secondary for kw in c.get("search_keywords", [])]
    generic_kws = generic.get("search_keywords", [])

    brand_desc = (
        f"{brand_name} is a brand. "
        + (brand.get("description", "") or "").strip()
        + ((" " + (brand.get("what_we_stand_for", "") or "").strip())
           if brand.get("what_we_stand_for") else "")
    ).strip()

    generic_label = generic.get("label") or "Generic category search"
    generic_kw_csv = ", ".join(generic_kws) if generic_kws else "the category"
    generic_desc = (
        f"These are generic discussions about {generic_kw_csv}. Many mentions "
        f"do not name any specific brand. Score sentiment toward the user's "
        f"experience or stance on the category they discuss: positive if they "
        f"recommend a product or are happy, negative if they complain about "
        f"taste/quality/results/price, neutral if it is a question or factual. "
        f"The goal is to surface posts where the OP is open to a recommendation "
        f"so we can plug {brand_name} in a follow-up reply."
    )

    return {
        "brand": {
            "label": f"Brand ({brand_name})",
            "queries": _quote_keywords(brand_kws),
            "subject_label": brand_name,
            "subject_description": brand_desc or f"{brand_name} brand monitoring.",
            "max_posts": _cap("brand", "max_posts", None),
            "max_total_mentions": _cap("brand", "max_total_mentions", None),
        },
        "primary_competitor": {
            "label": f"Primary Competitor ({primary_label})" if primary else "Primary Competitor",
            "queries": _quote_keywords(primary_kws),
            "subject_label": primary_label,
            "subject_description": primary_desc,
            "max_posts": _cap("primary_competitor", "max_posts", 200),
            "max_total_mentions": _cap("primary_competitor", "max_total_mentions", 300),
        },
        "secondary_competitor": {
            "label": f"Secondary Competitor ({secondary_label})" if secondary else "Secondary Competitor",
            "queries": _quote_keywords(secondary_kws),
            "subject_label": secondary_label,
            "subject_description": secondary_desc,
            "max_posts": _cap("secondary_competitor", "max_posts", 100),
            "max_total_mentions": _cap("secondary_competitor", "max_total_mentions", 150),
        },
        "generic_search": {
            "label": generic_label,
            "queries": _quote_keywords(generic_kws),
            "subject_label": "category in this mention",
            "subject_description": generic_desc,
            "max_posts": _cap("generic_search", "max_posts", 100),
            "max_total_mentions": _cap("generic_search", "max_total_mentions", 150),
        },
    }
