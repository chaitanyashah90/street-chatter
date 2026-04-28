"""Streamlit setup page for the Reddit monitor.

Renders an editable form bound to config.json. Marketers fill this in once on
first run; they can return to it any time via the Setup tab to tune the
brand, competitors, voice, or subreddit filter.

Public:
  render_setup_page(initial: bool) — render the form. When `initial` is True,
  the page renders standalone (first-run gate). Otherwise it renders inside
  the existing tab layout.
"""
from __future__ import annotations

import streamlit as st

from config import is_complete, load_config, save_config


def _split_lines(s: str) -> list[str]:
    return [line.strip() for line in (s or "").splitlines() if line.strip()]


def _parse_competitor_block(s: str) -> list[dict]:
    """Each non-empty line is `Name | keyword1, keyword2, ...`."""
    out: list[dict] = []
    for line in _split_lines(s):
        if "|" in line:
            name, kws = line.split("|", 1)
        else:
            name, kws = line, line
        name = name.strip()
        keywords = [k.strip() for k in kws.split(",") if k.strip()]
        if name:
            out.append({"name": name, "search_keywords": keywords or [name.lower()]})
    return out


def _format_competitors(comps: list[dict]) -> str:
    lines = []
    for c in comps or []:
        kws = ", ".join(c.get("search_keywords") or [])
        lines.append(f"{c.get('name', '')} | {kws}" if kws else c.get("name", ""))
    return "\n".join(lines)


def render_setup_page(initial: bool = False) -> None:
    cfg = load_config()
    brand = cfg.get("brand") or {}
    voice = brand.get("voice") or {}
    competitors = cfg.get("competitors") or {}
    generic = cfg.get("generic_search") or {}
    sub_filter = cfg.get("subreddit_filter") or {}
    app_cfg = cfg.get("app") or {}
    campaign_guidance = cfg.get("campaign_guidance") or {}

    if initial:
        st.title("Welcome — set up your brand monitor")
        st.markdown(
            "Fill in the basics below and click **Save & launch dashboard**. "
            "You can edit any of this later from the **Setup** tab."
        )
    else:
        st.markdown("### ⚙️ Setup")
        st.caption(
            "Edit your brand, voice, competitors, and search keywords here. "
            "Save and the dashboard re-renders with your new config."
        )

    ok, missing = is_complete(cfg)
    if not ok:
        st.warning(f"Missing required fields: {', '.join(missing)}")

    with st.form("setup_form", clear_on_submit=False):
        # ---- Brand ----
        st.markdown("#### 1. Your brand")
        col_a, col_b = st.columns(2)
        with col_a:
            brand_name = st.text_input(
                "Brand name", value=brand.get("name", ""),
                help="Display name as it appears in your marketing.",
            )
        with col_b:
            brand_url = st.text_input("Brand URL", value=brand.get("url", ""))
        brand_description = st.text_area(
            "Description (1-3 sentences)",
            value=brand.get("description", ""),
            help="What does the brand sell? Where? This goes into the LLM's brand context.",
        )
        brand_stand = st.text_area(
            "What the brand stands for",
            value=brand.get("what_we_stand_for", ""),
            help="Positioning / values. Shapes the reply persona.",
        )
        brand_keywords = st.text_area(
            "Brand search keywords (one per line)",
            value="\n".join(brand.get("search_keywords") or []),
            help=(
                "Every spelling, alias, and typo to search Reddit for. "
                "Include lowercase + spaced variants. The first keyword is used "
                "as the canonical mention in drafted replies."
            ),
            height=100,
        )

        # ---- Voice ----
        st.markdown("#### 2. Voice & persona")
        persona = st.text_area(
            "Persona — who is the reply author?",
            value=voice.get("persona", ""),
            help="One paragraph. Age, vibe, where they post, how they talk.",
        )
        tone_rules = st.text_area(
            "Tone rules (one per line)",
            value="\n".join(voice.get("tone_rules") or []),
            help="Hard rules the LLM must follow. Length limits, language, what to avoid.",
            height=140,
        )
        examples = st.text_area(
            "Voice samples (one per line, optional)",
            value="\n".join(voice.get("examples") or []),
            help="Real / paraphrased lines that capture the voice. Few-shot for the LLM.",
            height=120,
        )

        # ---- Competitors ----
        st.markdown("#### 3. Competitors")
        st.caption(
            "One per line, format: `Name | keyword1, keyword2, ...`. "
            "If you skip the keywords, the name is used (lowercased)."
        )
        primary_text = st.text_area(
            "Primary competitors (high-priority)",
            value=_format_competitors(competitors.get("primary", [])),
            help="Direct rivals you most want to plug your brand against.",
            height=100,
        )
        secondary_text = st.text_area(
            "Secondary competitors (lower-priority)",
            value=_format_competitors(competitors.get("secondary", [])),
            height=120,
        )

        # ---- Generic search ----
        st.markdown("#### 4. Generic category search")
        st.caption(
            "Broad category keywords where your brand could be plugged into a "
            "recommendation. Useful for capturing un-branded interest."
        )
        generic_keywords = st.text_area(
            "Category keywords (one per line)",
            value="\n".join(generic.get("search_keywords") or []),
            height=100,
        )
        category_hooks = st.text_area(
            "Category hooks (one per line, optional)",
            value="\n".join(generic.get("category_hooks") or []),
            help="Short notes per category telling the drafter when to plug.",
            height=100,
        )

        # ---- Campaign-specific reply guidance ----
        st.markdown("#### 5. Campaign-specific reply guidance")
        st.caption(
            "How should the reply differ by campaign type? Each block is "
            "appended to the prompt that drafts replies in that campaign. Be "
            "concrete — describe when to acknowledge vs. push back, when to "
            "plug the brand, when to stay silent."
        )
        guide_brand = st.text_area(
            "Brand replies (defending the brand on negatives, validating on positives)",
            value=campaign_guidance.get("brand", ""),
            help="When the post is about your brand — complaint, praise, or 'is it legit?' question.",
            height=110,
        )
        guide_primary = st.text_area(
            "Primary competitor replies",
            value=campaign_guidance.get("primary_competitor", ""),
            help="When the post is about your top competitors — how to plug your brand without trashing theirs.",
            height=110,
        )
        guide_secondary = st.text_area(
            "Secondary competitor replies",
            value=campaign_guidance.get("secondary_competitor", ""),
            help="Same as primary, with whatever softer / different rules apply.",
            height=110,
        )
        guide_generic = st.text_area(
            "Generic category replies (un-branded posts)",
            value=campaign_guidance.get("generic_search", ""),
            help="When the OP isn't naming a brand — expert reply with optional plug.",
            height=110,
        )

        # ---- Subreddit filter ----
        st.markdown("#### 6. Subreddit filter")
        col_m, col_u = st.columns(2)
        with col_m:
            mode = st.selectbox(
                "Filter mode",
                options=["any", "allowlist"],
                index=0 if (sub_filter.get("mode", "any")) == "any" else 1,
                help="`any` searches all of Reddit. `allowlist` restricts to subs you list.",
            )
        with col_u:
            include_user_pages = st.checkbox(
                "Include u_* user-profile pages", value=sub_filter.get("include_user_pages", True),
            )
        allowlist = st.text_area(
            "Allowlist subreddits (one per line, used when mode = allowlist)",
            value="\n".join(sub_filter.get("allowlist") or []),
            height=80,
        )
        substrings = st.text_area(
            "Name substrings — any subreddit containing one of these passes (e.g. `india`, `desi`)",
            value="\n".join(sub_filter.get("name_substrings") or []),
            height=60,
        )

        # ---- App ----
        st.markdown("#### 7. App settings")
        col_ua, col_nt = st.columns(2)
        with col_ua:
            user_agent = st.text_input(
                "User-Agent header", value=app_cfg.get("user_agent", "reddit-monitor/0.4 (by u/anonymous)"),
            )
        with col_nt:
            notif_title = st.text_input(
                "Notification title (daily alerts)",
                value=app_cfg.get("notification_title", "Reddit Monitor"),
            )

        # ---- Buttons ----
        st.markdown("---")
        bcol1, bcol2 = st.columns([1, 1])
        with bcol1:
            save_only = st.form_submit_button("💾 Save", use_container_width=True)
        with bcol2:
            save_launch = st.form_submit_button(
                "🚀 Save & launch dashboard",
                type="primary",
                use_container_width=True,
            )

    if save_only or save_launch:
        new_cfg = {
            "brand": {
                "name": brand_name.strip(),
                "url": brand_url.strip(),
                "description": brand_description.strip(),
                "what_we_stand_for": brand_stand.strip(),
                "search_keywords": _split_lines(brand_keywords),
                "voice": {
                    "persona": persona.strip(),
                    "tone_rules": _split_lines(tone_rules),
                    "examples": _split_lines(examples),
                },
            },
            "competitors": {
                "primary": _parse_competitor_block(primary_text),
                "secondary": _parse_competitor_block(secondary_text),
            },
            "generic_search": {
                "label": generic.get("label") or "Generic category search",
                "search_keywords": _split_lines(generic_keywords),
                "category_hooks": _split_lines(category_hooks),
            },
            "campaign_guidance": {
                "brand": guide_brand.strip(),
                "primary_competitor": guide_primary.strip(),
                "secondary_competitor": guide_secondary.strip(),
                "generic_search": guide_generic.strip(),
            },
            "subreddit_filter": {
                "mode": mode,
                "allowlist": _split_lines(allowlist),
                "name_substrings": _split_lines(substrings),
                "include_user_pages": include_user_pages,
            },
            "campaign_caps": cfg.get("campaign_caps") or {
                "brand": {"max_posts": None, "max_total_mentions": None},
                "primary_competitor": {"max_posts": 200, "max_total_mentions": 300},
                "secondary_competitor": {"max_posts": 100, "max_total_mentions": 150},
                "generic_search": {"max_posts": 100, "max_total_mentions": 150},
            },
            "app": {
                "user_agent": user_agent.strip(),
                "notification_title": notif_title.strip(),
            },
        }
        ok2, missing2 = is_complete(new_cfg)
        if not ok2:
            st.error(f"Cannot save — missing: {', '.join(missing2)}")
        else:
            save_config(new_cfg)
            st.success("Config saved.")
            if save_launch:
                st.rerun()
