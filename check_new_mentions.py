"""Daily alert: detect new Reddit mentions since last run and fire a macOS banner.

Designed to be invoked by a macOS LaunchAgent (or any cron-style scheduler).
Single-shot; exits after one check. Banner title comes from
config.app.notification_title.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from config import load_config
from reddit_monitor import DATA_DIR, Mention, fetch_mentions

SEEN_PATH = DATA_DIR / "seen_ids.json"
LOG_PATH = DATA_DIR / "monitor.log"
TIME_FILTER = "week"
MAX_SUBJECTS_IN_BANNER = 3


def _load_seen() -> set[str] | None:
    if not SEEN_PATH.exists():
        return None
    try:
        return set(json.loads(SEEN_PATH.read_text()))
    except (json.JSONDecodeError, OSError):
        return set()


def _save_seen(ids: set[str]) -> None:
    tmp = SEEN_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sorted(ids)))
    tmp.replace(SEEN_PATH)


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    line = f"[{ts}] {msg}\n"
    with LOG_PATH.open("a") as f:
        f.write(line)
    sys.stdout.write(line)


def _notify(title: str, subtitle: str, body: str) -> None:
    # AppleScript string-escape: backslash and double-quote.
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    script = (
        f'display notification "{esc(body)}" '
        f'with title "{esc(title)}" '
        f'subtitle "{esc(subtitle)}"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        _log(f"notify failed: {e}")


def _summarize(new: list[Mention]) -> tuple[str, str, str]:
    pos = sum(1 for m in new if m.sentiment_label == "positive")
    neg = sum(1 for m in new if m.sentiment_label == "negative")
    neu = sum(1 for m in new if m.sentiment_label == "neutral")

    cfg = load_config()
    title_prefix = (cfg.get("app") or {}).get("notification_title", "Reddit Monitor")
    title = f"{title_prefix}: {len(new)} new mention{'s' if len(new) != 1 else ''}"
    mix_parts = []
    if neg:
        mix_parts.append(f"{neg} negative")
    if pos:
        mix_parts.append(f"{pos} positive")
    if neu:
        mix_parts.append(f"{neu} neutral")
    subtitle = " · ".join(mix_parts) if mix_parts else "no sentiment"

    # Newest first; strip "re: " prefix from comments for cleaner display.
    top = sorted(new, key=lambda m: m.created_utc, reverse=True)[:MAX_SUBJECTS_IN_BANNER]
    lines = []
    for m in top:
        subject = m.title.removeprefix("re: ") if m.title else (m.body[:60] or "(no text)")
        lines.append(f"r/{m.subreddit}: {subject[:80]}")
    body = "\n".join(lines)
    return title, subtitle, body


def main() -> int:
    result = fetch_mentions(
        time_filter=TIME_FILTER,
        include_comments=True,
        cache_ttl_seconds=0,
    )
    all_ids = {m.id for m in result.mentions if m.id}

    seen = _load_seen()
    if seen is None:
        _save_seen(all_ids)
        _log(f"seeded {len(all_ids)} ids on first run; no banner fired")
        return 0

    new = [m for m in result.mentions if m.id and m.id not in seen]
    if not new:
        _log(f"0 new mentions (checked {len(all_ids)})")
        return 0

    title, subtitle, body = _summarize(new)
    _notify(title, subtitle, body)

    top_url = new[0].url or ""
    _log(f"{len(new)} new mentions | {subtitle} | top: {top_url}")

    _save_seen(seen | all_ids)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
