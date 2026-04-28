"""Streamlit dashboard for Reddit brand & competitor monitoring.

Five tabs:
  - Brand
  - Primary Competitor
  - Secondary Competitor
  - Generic category search
  - Setup (edit your brand, voice, competitors, keywords)

All brand-specific configuration lives in config.json. On first run (or any
time the config is incomplete), the dashboard shows the Setup page as a gate.

Reply guidance templates live in prompts/templates/. The drafter renders them
fresh on every config change into prompts/rendered/. Data persists in
data/monitor.db (SQLite); each fetch upserts so history accumulates.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import store
import summary as summary_mod
from config import is_complete, load_config
from reddit_monitor import CAMPAIGNS, fetch_mentions, to_dataframe
from reply_drafter import ReplyVariant, draft_reply, is_actionable, reply_mode_for
from setup_page import render_setup_page

ROOT = Path(__file__).parent

_cfg = load_config()
_BRAND_NAME = (_cfg.get("brand") or {}).get("name", "Brand")

st.set_page_config(page_title=f"{_BRAND_NAME} Reddit Monitor", layout="wide")

# First-run gate: if the config isn't complete, render the Setup page
# standalone and stop. The user fills it, hits "Save & launch", and the rerun
# falls through to the dashboard.
_cfg_ok, _missing = is_complete(_cfg)
if not _cfg_ok:
    render_setup_page(initial=True)
    st.stop()

st.title(f"{_BRAND_NAME} Reddit Monitor")
st.caption(
    f"Brand monitoring + competitor intelligence for {_BRAND_NAME}. "
    "Sentiment & summaries via Claude (your Pro or Max plan). Posts via Reddit search; "
    "comments via PullPush. Persistent store in data/monitor.db."
)


# ---------------------------------------------------------------------------
# Sidebar — global controls + posting
# ---------------------------------------------------------------------------
with st.sidebar:
    # ===== TOP: editable keywords per campaign =====
    st.header("Search keywords")
    st.caption(
        "Edit per campaign and click **Apply & refresh**. Defaults are restored "
        "by clicking **Reset**. The override is held in this session only."
    )
    # Per-campaign refresh flags consumed by render_campaign_tab.
    if "force_refresh" not in st.session_state:
        st.session_state["force_refresh"] = {k: False for k in CAMPAIGNS}
    if "queries_override" not in st.session_state:
        st.session_state["queries_override"] = {}

    for camp_key, camp in CAMPAIGNS.items():
        with st.expander(camp["label"], expanded=False):
            current = st.session_state["queries_override"].get(
                camp_key, "\n".join(q.strip('"') for q in camp["queries"])
            )
            edited = st.text_area(
                "Keywords (one per line)",
                value=current,
                key=f"qta_{camp_key}",
                height=130,
            )
            cc1, cc2 = st.columns([1, 1])
            with cc1:
                if st.button("Apply & refresh", key=f"apply_{camp_key}"):
                    st.session_state["queries_override"][camp_key] = edited
                    st.session_state["force_refresh"][camp_key] = True
                    st.rerun()
            with cc2:
                if st.button("Reset", key=f"reset_{camp_key}"):
                    st.session_state["queries_override"].pop(camp_key, None)
                    st.session_state["force_refresh"][camp_key] = True
                    st.rerun()

    st.markdown("---")

    # ===== Controls =====
    st.header("Controls")
    time_filter = st.selectbox(
        "Time window",
        options=["day", "week", "month", "year", "all"],
        index=2,
    )
    limit = st.slider("Max results per query per sort", 25, 300, 100, step=25)
    include_comments = st.checkbox(
        "Search comments (PullPush + within-post)", value=True
    )
    cache_minutes = st.slider("Fetch cache freshness (minutes)", 0, 60, 15)
    refresh = st.button("Refresh all tabs now")
    if refresh:
        for k in CAMPAIGNS:
            st.session_state["force_refresh"][k] = True

    st.markdown("---")
    st.markdown("**Reddit posting**")
    if st.button("Login to Reddit (one-time)"):
        with st.spinner("Opening browser… log in there, then close it."):
            proc = subprocess.run(
                [sys.executable, str(ROOT / "reddit_poster.py"), "login"],
                capture_output=True,
                text=True,
                timeout=12 * 60,
            )
            try:
                result = json.loads(proc.stdout.strip().splitlines()[-1])
                if result.get("ok"):
                    st.success(result.get("msg", "Login flow complete."))
                else:
                    st.error(result.get("msg", "Login flow failed."))
            except Exception:
                st.error(f"Login subprocess output: {proc.stdout}\nstderr: {proc.stderr}")

    st.markdown("---")
    st.markdown("**Reply guidance**")
    st.caption(
        "Templates live in `prompts/templates/`. The drafter substitutes your "
        "brand and voice from config.json on every call and writes the rendered "
        "result to `prompts/rendered/` for inspection. Edit voice via the "
        "Setup tab; edit the templates directly to change structure."
    )
    for fname in [
        "brand_reply_guidance.md",
        "primary_competitor_reply_guidance.md",
        "secondary_competitor_reply_guidance.md",
        "generic_search_reply_guidance.md",
    ]:
        st.code(f"prompts/templates/{fname}", language="text")

    st.markdown("---")
    s = store.stats()
    st.markdown("**Store**")
    st.caption(
        f"Total mentions: {s['total_mentions']}\n\n"
        + "\n\n".join(f"{k}: {v}" for k, v in s["per_campaign"].items())
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_date(ts) -> str:
    """Format a Timestamp / datetime as '24 April 2026' (no time)."""
    if pd.isna(ts):
        return ""
    if isinstance(ts, str):
        return ts
    return ts.strftime("%d %B %Y")


def _decorate(df: pd.DataFrame) -> pd.DataFrame:
    """Add display columns: reddit_id, fullname, date_str, body_preview."""
    if df.empty:
        return df
    df = df.copy()
    df["reddit_id"] = df["id"]
    df["fullname"] = df.apply(
        lambda r: ("t3_" if r["kind"] == "post" else "t1_") + r["id"], axis=1
    )
    df["date"] = df["created_at"].apply(_format_date)
    df["body_preview"] = df["body"].fillna("").str.slice(0, 280)
    return df


# ---------------------------------------------------------------------------
# Per-campaign renderer
# ---------------------------------------------------------------------------


def render_campaign_tab(campaign_key: str, refresh: bool, time_filter: str, limit: int, include_comments: bool, cache_minutes: int) -> None:
    camp = CAMPAIGNS[campaign_key]
    # Use the user's edited keyword list if they applied an override, else default.
    override = st.session_state.get("queries_override", {}).get(campaign_key)
    if override:
        queries = ['"' + line.strip() + '"' for line in override.splitlines() if line.strip()]
    else:
        queries = camp["queries"]
    # Per-campaign force_refresh OR global "Refresh all tabs".
    force_now = (
        refresh
        or st.session_state.get("force_refresh", {}).get(campaign_key, False)
    )
    cache_ttl = 0 if force_now else cache_minutes * 60
    if force_now:
        # Consume the flag so subsequent re-renders don't keep refetching.
        st.session_state.setdefault("force_refresh", {})[campaign_key] = False

    with st.status(
        f"Loading {camp['label']}…", expanded=True, state="running"
    ) as status_box:
        progress_lines: list[str] = []

        def _progress(msg: str) -> None:
            progress_lines.append(msg)
            # Show the latest 6 progress lines so the user can see where the
            # analyzer is in long fetches (esp. competitor tabs).
            status_box.write("\n".join(f"• {line}" for line in progress_lines[-6:]))

        try:
            result = fetch_mentions(
                queries=queries,
                limit_per_query=limit,
                time_filter=time_filter,
                include_comments=include_comments,
                cache_ttl_seconds=cache_ttl,
                campaign=campaign_key,
                progress_cb=_progress,
            )
            status_box.update(state="complete", label=f"{camp['label']} loaded")
        except Exception as e:
            status_box.update(state="error", label=f"Failed: {type(e).__name__}: {e}")
            raise
    df = to_dataframe(result)

    if df.empty:
        st.warning(f"No mentions found for {camp['label']} in the current window.")
        return

    df = _decorate(df)
    st.caption(
        f"Subject: **{camp['subject_label']}** · "
        f"keywords: `{', '.join(q.strip(chr(34)) for q in queries)}` · "
        f"fetched at {result.fetched_at.strftime('%d %B %Y %H:%M UTC')} — "
        f"{len(df)} mentions"
    )

    posts_df = df[df["kind"] == "post"].copy().reset_index(drop=True)
    comments_df = df[df["kind"] == "comment"].copy().reset_index(drop=True)

    # --- Top metrics ---
    now = datetime.now(timezone.utc)
    last_24h = df[df["created_at"] >= now - timedelta(hours=24)]
    last_7d = df[df["created_at"] >= now - timedelta(days=7)]

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total", len(df))
    c2.metric("Posts", len(posts_df))
    c3.metric("Comments", len(comments_df))
    c4.metric("Last 24h", len(last_24h))
    c5.metric("Last 7d", len(last_7d))
    c6.metric("Subreddits", df["subreddit"].nunique())

    pos = (df["sentiment_label"] == "positive").sum()
    neg = (df["sentiment_label"] == "negative").sum()
    neu = (df["sentiment_label"] == "neutral").sum()
    st.write(f"**Sentiment mix:** 🟢 {pos} positive · ⚪ {neu} neutral · 🔴 {neg} negative")

    if not comments_df.empty:
        breakdown = comments_df["source"].value_counts().to_dict()
        independent = breakdown.get("independent-comment", 0)
        within = breakdown.get("within-post-comment", 0)
        st.caption(
            f"Comments by source: {within} from within matched posts · "
            f"{independent} independent (PullPush)"
        )

    # --- Summary analysis ---
    st.markdown("### 📊 Sentiment summary analysis")
    summary_state_key = f"summary_md_{campaign_key}"
    summary_status_key = f"summary_status_{campaign_key}"
    col_left, col_right = st.columns([3, 1])
    with col_right:
        run_summary = st.button(
            "Generate summary",
            key=f"sum_btn_{campaign_key}",
            help="Calls Claude with all mentions in the current window. ~10-30 sec.",
        )
        force_regen = st.checkbox(
            "Force regenerate (ignore cache)",
            key=f"sum_force_{campaign_key}",
            value=False,
        )
    if run_summary:
        with st.spinner("Generating summary via Claude…"):
            md, status = summary_mod.summarize(
                campaign=campaign_key,
                df=df,
                subject_label=camp["subject_label"],
                subject_description=camp["subject_description"],
                use_cache=not force_regen,
            )
            st.session_state[summary_state_key] = md
            st.session_state[summary_status_key] = status
    md = st.session_state.get(summary_state_key)
    if md:
        status = st.session_state.get(summary_status_key, "")
        with col_left:
            if status == "cached":
                st.caption("_From cache. Toggle 'Force regenerate' for a fresh run._")
            st.markdown(md)
    else:
        with col_left:
            st.info(
                "Click **Generate summary** to produce an executive overview "
                "(themes, positives, negatives, opportunities, risks, "
                "sentiment movement)."
            )

    # --- Trendlines ---
    st.markdown("### Trendlines")
    df_trend = df.copy()
    df_trend["date_floor"] = df_trend["created_at"].dt.tz_convert("UTC").dt.floor("D")
    vol = df_trend.groupby(["date_floor", "sentiment_label"]).size().reset_index(name="count")
    vol_total = df_trend.groupby("date_floor").size().reset_index(name="total")

    tab_vol, tab_sent, tab_subs = st.tabs(
        ["Mention volume", "Sentiment over time", "By subreddit"]
    )
    with tab_vol:
        fig = px.bar(
            vol,
            x="date_floor",
            y="count",
            color="sentiment_label",
            color_discrete_map={"positive": "#2ca02c", "neutral": "#9ca3af", "negative": "#d62728"},
            title="Mentions per day, stacked by sentiment",
        )
        fig.add_trace(
            go.Scatter(
                x=vol_total["date_floor"],
                y=vol_total["total"].rolling(7, min_periods=1).mean(),
                mode="lines",
                name="7-day avg",
                line=dict(color="#1f77b4", width=3),
            )
        )
        fig.update_layout(barmode="stack", xaxis_title="", yaxis_title="Mentions")
        st.plotly_chart(fig, use_container_width=True, key=f"vol_{campaign_key}")

    with tab_sent:
        daily_sent = df_trend.groupby("date_floor")["sentiment"].mean().reset_index()
        daily_sent["rolling"] = daily_sent["sentiment"].rolling(7, min_periods=1).mean()
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=daily_sent["date_floor"], y=daily_sent["sentiment"], mode="markers+lines", name="Daily avg", line=dict(color="#9ca3af")))
        fig.add_trace(go.Scatter(x=daily_sent["date_floor"], y=daily_sent["rolling"], mode="lines", name="7-day rolling", line=dict(color="#1f77b4", width=3)))
        fig.add_hline(y=0, line_dash="dot", line_color="#666")
        fig.update_layout(title="Average sentiment (−1 negative, +1 positive)", xaxis_title="", yaxis_title="Sentiment", yaxis=dict(range=[-1, 1]))
        st.plotly_chart(fig, use_container_width=True, key=f"sent_{campaign_key}")

    with tab_subs:
        top_subs = (
            df.groupby("subreddit")
            .agg(mentions=("id", "count"), avg_sentiment=("sentiment", "mean"))
            .sort_values("mentions", ascending=False)
            .head(15)
            .reset_index()
        )
        fig = px.bar(top_subs, x="mentions", y="subreddit", orientation="h", color="avg_sentiment", color_continuous_scale="RdYlGn", range_color=[-1, 1], title="Top subreddits by volume (color = avg sentiment)")
        fig.update_layout(yaxis=dict(autorange="reversed"))
        st.plotly_chart(fig, use_container_width=True, key=f"subs_{campaign_key}")

    # --- Filters ---
    st.markdown("### Mentions")
    label_filter = st.multiselect(
        "Filter by sentiment",
        options=["positive", "neutral", "negative"],
        default=["positive", "neutral", "negative"],
        key=f"label_{campaign_key}",
    )

    def _apply(frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty:
            return frame
        return frame[frame["sentiment_label"].isin(label_filter)].copy()

    # --- Posts ---
    st.markdown("#### 📄 Posts")
    posts_f = _apply(posts_df)
    if posts_f.empty:
        st.info("No posts match the current filter.")
    else:
        st.caption(f"{len(posts_f)} post(s)")
        st.dataframe(
            posts_f[
                [
                    "date",
                    "reddit_id",
                    "subreddit",
                    "author",
                    "sentiment_label",
                    "sentiment",
                    "issue_summary",
                    "title",
                    "score",
                    "num_comments",
                    "url",
                ]
            ].rename(columns={"sentiment": "score_num", "score": "upvotes", "issue_summary": "issue summary", "num_comments": "comments", "reddit_id": "post id"}),
            use_container_width=True,
            hide_index=True,
            column_config={
                "url": st.column_config.LinkColumn("link"),
                "score_num": st.column_config.NumberColumn(format="%+.2f"),
                "issue summary": st.column_config.TextColumn(width="large"),
                "title": st.column_config.TextColumn(width="large"),
            },
        )

    # --- Comment scraper ---
    st.markdown("#### 🔎 Comment scraper")
    st.caption(
        "All comments mentioning the keyword — from within matched posts and from "
        "PullPush independent search."
    )
    comments_all = _apply(comments_df)
    if comments_all.empty:
        st.info("No comments match the current filter.")
    else:
        st.caption(f"{len(comments_all)} comment(s)")
        comments_all["parent_label"] = comments_all["parent_title"].fillna("").apply(lambda t: t if t else "(parent unknown)")
        st.dataframe(
            comments_all[
                [
                    "date",
                    "reddit_id",
                    "parent_id",
                    "subreddit",
                    "source",
                    "author",
                    "sentiment_label",
                    "sentiment",
                    "body_preview",
                    "parent_label",
                    "parent_url",
                    "url",
                ]
            ].rename(columns={"sentiment": "score_num", "body_preview": "comment", "parent_label": "parent post", "parent_url": "parent link", "url": "comment link", "reddit_id": "comment id", "parent_id": "parent post id"}),
            use_container_width=True,
            hide_index=True,
            column_config={
                "comment link": st.column_config.LinkColumn("comment link"),
                "parent link": st.column_config.LinkColumn("parent link"),
                "score_num": st.column_config.NumberColumn(format="%+.2f"),
                "comment": st.column_config.TextColumn(width="large"),
                "parent post": st.column_config.TextColumn(width="medium"),
            },
        )

    # --- Comments within posts ---
    st.markdown("#### 🧵 Comments within posts (parent matched)")
    within = comments_all[comments_all["parent_in_posts"] == True].copy() if not comments_all.empty else comments_all
    if within.empty:
        st.info("No comments tied to a matched post in the current view.")
    else:
        st.caption(f"{len(within)} comment(s)")
        st.dataframe(
            within[
                [
                    "date",
                    "reddit_id",
                    "parent_id",
                    "subreddit",
                    "author",
                    "parent_sentiment_label",
                    "parent_sentiment",
                    "sentiment_label",
                    "sentiment",
                    "parent_title",
                    "body_preview",
                    "parent_url",
                    "url",
                ]
            ].rename(columns={"parent_sentiment_label": "post sentiment", "parent_sentiment": "post score", "sentiment_label": "comment sentiment", "sentiment": "comment score", "parent_title": "post title", "body_preview": "comment", "parent_url": "post link", "url": "comment link", "reddit_id": "comment id", "parent_id": "parent post id"}),
            use_container_width=True,
            hide_index=True,
            column_config={
                "post link": st.column_config.LinkColumn("post link"),
                "comment link": st.column_config.LinkColumn("comment link"),
                "post score": st.column_config.NumberColumn(format="%+.2f"),
                "comment score": st.column_config.NumberColumn(format="%+.2f"),
                "comment": st.column_config.TextColumn(width="large"),
                "post title": st.column_config.TextColumn(width="medium"),
            },
        )

    # --- Actionable + reply UI ---
    _render_actionable_panel(campaign_key, posts_df, comments_df)

    # --- Download ---
    with st.expander("Download raw data (CSV)"):
        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download CSV",
            csv,
            file_name=f"{campaign_key}_{datetime.utcnow():%Y%m%d_%H%M}.csv",
            mime="text/csv",
            key=f"dl_{campaign_key}",
        )


def _draft_one(campaign_key: str, post_id: str, row: pd.Series, rel: pd.DataFrame) -> tuple[bool, str]:
    """Generate a draft and persist it. Returns (ok, msg).

    On failure, persists the error to the store so it survives reruns and is
    visible the next time the row is rendered.
    """
    try:
        variant, _raw = draft_reply(row, rel, campaign=campaign_key)
    except subprocess.CalledProcessError as e:
        msg = f"`claude -p` failed (rc={e.returncode}): {(e.stderr or e.stdout or '')[:400]}"
        store.upsert_action(campaign_key, post_id, last_error=msg[:500])
        return False, msg
    except Exception as e:
        import traceback
        tb = traceback.format_exc().splitlines()[-3:]
        msg = f"{type(e).__name__}: {e}\n" + "\n".join(tb)
        store.upsert_action(campaign_key, post_id, last_error=msg[:500])
        return False, msg
    store.upsert_action(
        campaign_key,
        post_id,
        status="pending",
        draft_text=variant.reply,
        draft_tone=variant.tone,
        draft_mentions=variant.mentions,
        last_error="",  # clear any prior error
    )
    return True, "drafted"


def _submit_via_playwright(jobs: list[dict]) -> list[dict]:
    """Run reddit_poster.py bulk in a subprocess; return list of result dicts.

    Annotates each job's result with the post_id (echoed back) so the caller
    can correlate. On unparseable output, returns one error result per job
    with the captured stdout/stderr so the user sees the actual failure.
    """
    if not jobs:
        return []
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as jf:
        json.dump(jobs, jf)
        jobs_path = jf.name
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "reddit_poster.py"), "bulk", jobs_path],
            capture_output=True,
            text=True,
            timeout=60 + 90 * len(jobs),
        )
        # Try to parse the LAST line of stdout (where reddit_poster.py prints JSON).
        last_line = ""
        for line in reversed(proc.stdout.strip().splitlines()):
            if line.startswith("{"):
                last_line = line
                break
        try:
            payload = json.loads(last_line) if last_line else {}
            results = payload.get("results", [])
            if results:
                return results
        except Exception:
            pass
        # Fallback: return the same error for every job, with full diagnostics.
        diag = (
            f"poster subprocess rc={proc.returncode}; "
            f"stdout_tail={proc.stdout[-400:]!r}; "
            f"stderr_tail={proc.stderr[-400:]!r}"
        )
        return [
            {"post_id": j.get("post_id"), "ok": False, "msg": diag}
            for j in jobs
        ]
    except subprocess.TimeoutExpired as e:
        return [
            {"post_id": j.get("post_id"), "ok": False, "msg": f"poster timed out after {e.timeout}s"}
            for j in jobs
        ]
    finally:
        try:
            Path(jobs_path).unlink(missing_ok=True)
        except Exception:
            pass


def _render_actionable_panel(campaign_key: str, posts_df: pd.DataFrame, comments_df: pd.DataFrame) -> None:
    """Tabular CRM-style panel: Pending on top, Posted/Discarded below."""
    st.markdown("---")

    # CSS for the actionable table: vertical column dividers, prevent text wrap
    # on short labels, and consistent button widths. Scoped via a wrapping div
    # with class actionable-tbl so it doesn't bleed into trendlines/other tabs.
    st.markdown(
        """
        <style>
        .actionable-tbl [data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {
            border-right: 1px solid #e6e6e6;
            padding-right: 8px !important;
            padding-left: 8px !important;
        }
        .actionable-tbl [data-testid="stHorizontalBlock"] > div[data-testid="stColumn"]:last-child {
            border-right: none;
        }
        .actionable-tbl [data-testid="stHorizontalBlock"] {
            margin-bottom: 4px !important;
        }
        .actionable-tbl .stButton > button {
            width: 100%;
            min-height: 34px;
            padding: 4px 6px !important;
            font-size: 0.85rem !important;
            white-space: nowrap;
        }
        .actionable-tbl .stCheckbox label {
            display: none;
        }
        .actionable-tbl .stMarkdown p, .actionable-tbl .stCaption {
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .actionable-tbl-row [data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {
            display: flex;
            flex-direction: column;
            justify-content: flex-start;
        }
        </style>
        <div class="actionable-tbl">
        """,
        unsafe_allow_html=True,
    )

    st.markdown("### 🎯 Actionable")
    if campaign_key == "brand":
        st.caption(
            f"{_BRAND_NAME} posts: defend on negatives, double-down on positives, vouch on legit-questions. "
            "Off-topic posts excluded automatically."
        )
    elif campaign_key in ("primary_competitor", "secondary_competitor"):
        st.caption(
            f"Competitor posts: suggest {_BRAND_NAME} on recommendation requests, "
            "complaints, or praise. Off-topic posts excluded."
        )
    else:
        st.caption(
            f"Generic category posts: expert reply, soft plug for {_BRAND_NAME} only when natural. "
            "Off-topic posts excluded."
        )

    # Build the actionable list with related comments grouped by parent_id.
    comments_by_parent = (
        {pid: g for pid, g in comments_df.groupby("parent_id")}
        if not comments_df.empty
        else {}
    )
    actionable: list[tuple[pd.Series, pd.DataFrame]] = []
    for _, row in posts_df.iterrows():
        rel = comments_by_parent.get(row["id"], pd.DataFrame())
        if is_actionable(row, rel, campaign_key):
            actionable.append((row, rel))

    if not actionable:
        st.success("Nothing to draft right now.")
        return

    # Map post_id → (row, rel) for fast lookup.
    actionable_map = {row["id"]: (row, rel) for row, rel in actionable}

    # ---- Process any queued operations BEFORE rendering widgets ----
    # Streamlit forbids writing to session_state[K] after a widget with key K
    # has been instantiated this run. So actions that need to set a widget's
    # state (textareas, checkboxes) queue a request via a non-widget key, and
    # we drain the queue here at the top of the panel — before rows render.
    draft_q_key = f"_draft_queue_{campaign_key}"
    clear_sel_key = f"_clear_selection_{campaign_key}"

    queued_drafts = st.session_state.pop(draft_q_key, [])
    if queued_drafts:
        with st.status(
            f"Drafting {len(queued_drafts)} reply(ies)…", expanded=True
        ) as s:
            for i, pid in enumerate(queued_drafts, 1):
                if pid not in actionable_map:
                    continue
                row, rel = actionable_map[pid]
                s.write(
                    f"• {i}/{len(queued_drafts)}: r/{row['subreddit']} · "
                    f"{(row['title'] or '')[:60]}"
                )
                ok, msg = _draft_one(campaign_key, pid, row, rel)
                if ok:
                    fresh = store.get_action(campaign_key, pid) or {}
                    st.session_state[f"draft_text_{campaign_key}_{pid}"] = (
                        fresh.get("draft_text") or ""
                    )
                else:
                    s.write(f"   ⚠️ {msg}")
            s.update(state="complete", label="Drafts done")

    # Pull current statuses from the store (refreshed after any queued drafts).
    actions_by_id = {a["post_id"]: a for a in store.list_actions(campaign_key)}

    # Clear-selection queue: set each pending checkbox's state to False BEFORE
    # the checkboxes render this run.
    if st.session_state.pop(clear_sel_key, False):
        for pid, _ in actionable_map.items():
            ck = f"sel_{campaign_key}_{pid}"
            if ck in st.session_state:
                st.session_state[ck] = False

    pending_rows: list[tuple[pd.Series, pd.DataFrame, dict]] = []
    posted_rows: list[tuple[pd.Series, pd.DataFrame, dict]] = []
    discarded_rows: list[tuple[pd.Series, pd.DataFrame, dict]] = []
    for row, rel in actionable:
        action = actions_by_id.get(row["id"], {"status": "pending"})
        status = action.get("status", "pending")
        if status == "posted":
            posted_rows.append((row, rel, action))
        elif status == "discarded":
            discarded_rows.append((row, rel, action))
        else:
            pending_rows.append((row, rel, action))

    pending_ids = [r["id"] for r, _, _ in pending_rows]

    # Reserve the bulk-actions container near the top, but render its contents
    # AFTER the rows below — that way the live checkbox state is observable
    # when we compute "selected" counts and disabled states.
    bulk_box = st.container()

    # ===== Tabular Pending section =====
    st.markdown("#### 🟡 Pending")
    if not pending_rows:
        st.info("No pending posts.")
    else:
        h = st.columns(
            [0.35, 1.2, 1.2, 1.1, 0.9, 2.7, 3.8, 1.5],
            gap="small",
        )
        for col, label in zip(
            h,
            ["", "Date · Sub", "Action", "Sentiment", "Comments", "Title / issue", "Draft", "Actions"],
        ):
            col.markdown(f"**{label}**")
        st.markdown(
            "<hr style='margin:2px 0 4px 0;border:none;border-top:2px solid #cfcfcf;'>",
            unsafe_allow_html=True,
        )

        for row, rel, action in pending_rows:
            _render_pending_row(campaign_key, row, rel, action)
            st.markdown(
                "<hr style='margin:0;border:none;border-top:1px solid #ececec;'>",
                unsafe_allow_html=True,
            )

    # ===== Render bulk actions INTO the reserved container =====
    # Read the live selection from each checkbox's session_state (set during
    # the row-rendering pass above), so counts always reflect the current state.
    def _ck(pid: str) -> str:
        return f"sel_{campaign_key}_{pid}"

    selected_ids = {pid for pid in pending_ids if st.session_state.get(_ck(pid), False)}
    eligible_to_post = [
        pid for pid in selected_ids if (actions_by_id.get(pid, {}).get("draft_text") or "").strip()
    ]
    needs_draft = [
        pid for pid in selected_ids if not (actions_by_id.get(pid, {}).get("draft_text") or "").strip()
    ]

    with bulk_box:
        st.markdown("#### ⚡ Bulk actions")
        st.caption(
            f"{len(selected_ids)} selected of {len(pending_ids)} pending  ·  "
            f"{len(eligible_to_post)} ready to post  ·  "
            f"{len(needs_draft)} still need a draft"
        )
        bcol1, bcol2, bcol3, bcol4, bcol5 = st.columns([1.5, 1.7, 1.7, 1.5, 1.0])
        with bcol1:
            if st.button(
                f"☑ Select all ({len(pending_ids)})",
                key=f"bulk_selectall_{campaign_key}",
                disabled=not pending_ids,
                use_container_width=True,
            ):
                for pid in pending_ids:
                    st.session_state[_ck(pid)] = True
                st.rerun()
        with bcol2:
            if st.button(
                f"📝 Generate drafts ({len(needs_draft)})",
                key=f"bulk_draft_{campaign_key}",
                disabled=not needs_draft,
                use_container_width=True,
            ):
                # Queue all the post IDs. Processed at the top of the panel
                # on rerun, with full progress UI, before any widget renders.
                st.session_state[f"_draft_queue_{campaign_key}"] = list(needs_draft)
                st.rerun()
        with bcol3:
            if st.button(
                f"📤 Submit to Reddit ({len(eligible_to_post)})",
                key=f"bulk_submit_{campaign_key}",
                type="primary",
                disabled=not eligible_to_post,
                use_container_width=True,
            ):
                jobs = []
                for pid in eligible_to_post:
                    row, _ = actionable_map[pid]
                    jobs.append({
                        "post_id": pid,
                        "url": row["url"],
                        "text": actions_by_id[pid]["draft_text"],
                    })
                with st.status(
                    f"Posting {len(jobs)} reply(ies) via Playwright…",
                    expanded=True,
                ) as s:
                    results = _submit_via_playwright(jobs)
                    for r in results:
                        pid = r.get("post_id", "?")
                        if r.get("ok"):
                            s.write(f"✅ {pid}: {r.get('msg', 'posted')}")
                            store.upsert_action(
                                campaign_key, pid, status="posted",
                                last_post_url=r.get("post_url"),
                            )
                        else:
                            s.write(f"❌ {pid}: {r.get('msg')}")
                            store.upsert_action(
                                campaign_key, pid,
                                last_error=r.get("msg", "")[:500],
                            )
                    s.update(state="complete", label="Bulk submit done")
                # Posted rows leave the pending list automatically; no need
                # to touch checkbox session_state (which would fail anyway
                # since checkboxes have already instantiated this run).
                st.rerun()
        with bcol4:
            if st.button(
                f"🗑️ Discard ({len(selected_ids)})",
                key=f"bulk_discard_{campaign_key}",
                disabled=not selected_ids,
                use_container_width=True,
            ):
                for pid in selected_ids:
                    store.upsert_action(campaign_key, pid, status="discarded")
                # Discarded rows leave the pending list; no need to clear
                # checkbox session_state (would error on already-instantiated keys).
                st.rerun()
        with bcol5:
            if st.button(
                "Clear",
                key=f"bulk_clear_{campaign_key}",
                disabled=not selected_ids,
                use_container_width=True,
            ):
                # Queue the clear; processed at top of panel before checkboxes render.
                st.session_state[f"_clear_selection_{campaign_key}"] = True
                st.rerun()
        st.markdown(
            "<hr style='margin:6px 0 10px 0;border:none;border-top:2px solid #cfcfcf;'>",
            unsafe_allow_html=True,
        )

    # ===== Posted / Discarded sections =====
    if posted_rows or discarded_rows:
        st.markdown("---")

    if posted_rows:
        st.markdown(f"#### 🟢 Posted ({len(posted_rows)})")
        _render_archive_table(campaign_key, posted_rows, kind="posted")

    if discarded_rows:
        st.markdown(f"#### ⚪ Discarded ({len(discarded_rows)})")
        _render_archive_table(campaign_key, discarded_rows, kind="discarded")

    # Close the .actionable-tbl wrapper opened at the top.
    st.markdown("</div>", unsafe_allow_html=True)


def _render_pending_row(
    campaign_key: str,
    row: pd.Series,
    rel: pd.DataFrame,
    action: dict,
) -> None:
    post_id = row["id"]
    sentiment_label = row.get("sentiment_label", "neutral")
    action_type = row.get("action_type") or "—"
    title = (row["title"] or "")[:90]
    issue = (row.get("issue_summary") or "")[:120]
    n_total_comments = int(row.get("num_comments") or 0)
    sentiment_emoji = {"negative": "🔴", "neutral": "⚪", "positive": "🟢"}.get(
        sentiment_label, "⚪"
    )
    has_draft = bool((action.get("draft_text") or "").strip())

    c = st.columns([0.35, 1.2, 1.2, 1.1, 0.9, 2.7, 3.8, 1.5], gap="small")

    # Checkbox — its session_state key is the source of truth for bulk selection.
    with c[0]:
        st.checkbox(
            "select",
            key=f"sel_{campaign_key}_{post_id}",
            label_visibility="collapsed",
        )

    # Date · Sub
    with c[1]:
        st.markdown(f"`{_format_date(row['created_at'])}`")
        st.caption(f"r/{row['subreddit']}")

    # Action type
    with c[2]:
        st.markdown(f"`{action_type}`")

    # Sentiment (no wrap)
    with c[3]:
        st.markdown(f"{sentiment_emoji}&nbsp;{sentiment_label}", unsafe_allow_html=True)

    # Comments count (Reddit's reported count + keyword-matched indicator)
    with c[4]:
        kw_matched = len(rel)
        if kw_matched > 0:
            st.markdown(f"**{n_total_comments}**&nbsp;({kw_matched}🔑)", unsafe_allow_html=True)
        else:
            st.markdown(f"**{n_total_comments}**")

    # Title / issue / link
    with c[5]:
        st.markdown(f"[{title}]({row['url']})")
        if issue:
            st.caption(issue)
        last_err = (action.get("last_error") or "").strip()
        if last_err:
            st.caption(f"⚠️ {last_err[:160]}")

    # Draft textarea — persists to store on edit.
    # Seed session_state before the widget renders so we can avoid passing
    # both `value=` and `key=` (Streamlit warns when a widget has a default
    # AND its session_state slot is set — exactly the situation the bulk
    # drafter triggers when it writes the new draft text into session_state
    # at the top of the panel).
    with c[6]:
        textarea_key = f"draft_text_{campaign_key}_{post_id}"
        current = action.get("draft_text") or ""
        if textarea_key not in st.session_state:
            st.session_state[textarea_key] = current
        edited = st.text_area(
            "draft",
            key=textarea_key,
            height=110,
            label_visibility="collapsed",
            placeholder="Click 📝 Draft, or type your own.",
        )
        if edited != current:
            store.upsert_action(campaign_key, post_id, draft_text=edited)

    # Per-row actions — three buttons, equal-width via use_container_width
    with c[7]:
        if st.button(
            "📝 Draft",
            key=f"row_draft_{campaign_key}_{post_id}",
            help="Generate draft via Claude",
            use_container_width=True,
        ):
            # Queue the draft request. Processed at the top of the panel on
            # rerun, BEFORE textareas instantiate (Streamlit forbids writing
            # to a widget's session_state after the widget renders).
            q = st.session_state.get(f"_draft_queue_{campaign_key}", [])
            q.append(post_id)
            st.session_state[f"_draft_queue_{campaign_key}"] = q
            st.rerun()
        if st.button(
            "📤 Post",
            key=f"row_post_{campaign_key}_{post_id}",
            type="primary",
            disabled=not has_draft,
            help="Submit this single reply via Playwright",
            use_container_width=True,
        ):
            with st.spinner("Posting via Playwright…"):
                jobs = [{
                    "post_id": post_id,
                    "url": row["url"],
                    "text": action.get("draft_text", ""),
                }]
                results = _submit_via_playwright(jobs)
                r = results[0] if results else {"ok": False, "msg": "no result"}
            if r.get("ok"):
                store.upsert_action(
                    campaign_key, post_id, status="posted",
                    last_post_url=r.get("post_url"),
                    last_error="",
                )
                st.rerun()
            else:
                # Persist + display; don't rerun so the error is visible.
                store.upsert_action(
                    campaign_key, post_id,
                    last_error=(r.get("msg") or "")[:500],
                )
                st.error(r.get("msg", "post failed"))
        if st.button(
            "🗑️ Discard",
            key=f"row_disc_{campaign_key}_{post_id}",
            help="Mark as discarded",
            use_container_width=True,
        ):
            store.upsert_action(campaign_key, post_id, status="discarded")
            st.rerun()


def _render_archive_table(
    campaign_key: str,
    rows: list,
    kind: str,
) -> None:
    """Read-only archive view for posted/discarded items, with a 'Reopen' button."""
    h = st.columns([1.2, 1.2, 1.1, 3.8, 3.0, 1.0], gap="small")
    for col, label in zip(
        h,
        ["Date · Sub", "Action", "Sentiment", "Title", "Sent draft", "Reopen"],
    ):
        col.markdown(f"**{label}**")
    st.markdown(
        "<hr style='margin:0;border:none;border-top:1px solid #e0e0e0;'>",
        unsafe_allow_html=True,
    )
    for row, _rel, action in rows:
        post_id = row["id"]
        c = st.columns([1.2, 1.2, 1.1, 3.8, 3.0, 1.0], gap="small")
        with c[0]:
            st.markdown(f"`{_format_date(row['created_at'])}`")
            st.caption(f"r/{row['subreddit']}")
        with c[1]:
            st.markdown(f"`{row.get('action_type') or '—'}`")
        with c[2]:
            sentiment = row.get("sentiment_label", "neutral")
            emoji = {"negative": "🔴", "neutral": "⚪", "positive": "🟢"}.get(sentiment, "⚪")
            st.markdown(f"{emoji}&nbsp;{sentiment}", unsafe_allow_html=True)
        with c[3]:
            st.markdown(f"[{(row['title'] or '')[:90]}]({row['url']})")
            if row.get("issue_summary"):
                st.caption(row["issue_summary"][:120])
        with c[4]:
            sent_draft = (action.get("draft_text") or "")[:240]
            st.caption(sent_draft or "(no draft text)")
        with c[5]:
            if st.button(
                "↩",
                key=f"reopen_{kind}_{campaign_key}_{post_id}",
                help="Reopen as pending",
                use_container_width=True,
            ):
                store.upsert_action(campaign_key, post_id, status="pending")
                st.rerun()
        st.markdown(
            "<hr style='margin:0;border:none;border-top:1px solid #ececec;'>",
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

tab_brand, tab_primary, tab_secondary, tab_generic, tab_setup = st.tabs(
    [
        f"🏷️ {CAMPAIGNS['brand']['label']}",
        f"⚔️ {CAMPAIGNS['primary_competitor']['label']}",
        f"🔍 {CAMPAIGNS['secondary_competitor']['label']}",
        f"🌐 {CAMPAIGNS['generic_search']['label']}",
        "⚙️ Setup",
    ]
)

with tab_brand:
    render_campaign_tab("brand", refresh, time_filter, limit, include_comments, cache_minutes)

with tab_primary:
    render_campaign_tab("primary_competitor", refresh, time_filter, limit, include_comments, cache_minutes)

with tab_secondary:
    render_campaign_tab("secondary_competitor", refresh, time_filter, limit, include_comments, cache_minutes)

with tab_generic:
    render_campaign_tab("generic_search", refresh, time_filter, limit, include_comments, cache_minutes)

with tab_setup:
    render_setup_page(initial=False)
