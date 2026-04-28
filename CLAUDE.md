# Reddit Brand Monitor — guide for Claude Code

This is an open-source Reddit brand & competitor monitoring dashboard. Marketers configure their brand once and use it to track mentions, score sentiment, and draft on-voice replies.

## For first-time users

If `config.json` does not exist, **or** still contains the placeholder brand (`Acme Protein`), suggest the user run `/onboard`. That command walks them through 8 setup steps and launches the dashboard.

```
/onboard
```

After onboarding, the marketer can edit any setting from the **⚙️ Setup** tab inside the dashboard — no need to re-run `/onboard`.

## Architecture

- [config.py](config.py) — loads/validates `config.json`, builds the 4-campaign dict, exposes template variables for prompt rendering.
- [reddit_monitor.py](reddit_monitor.py) — Reddit search + PullPush comment fetch, hybrid keyword + LLM analyzer (entity-aware sentiment, action-type classification).
- [reply_drafter.py](reply_drafter.py) — drafts replies via the local `claude` CLI; loads templates from `prompts/templates/` and substitutes brand/voice from config.
- [summary.py](summary.py) — executive summaries via `claude -p`.
- [store.py](store.py) — SQLite persistence (`data/monitor.db`).
- [app.py](app.py) — Streamlit dashboard, 4 campaign tabs + Setup tab.
- [setup_page.py](setup_page.py) — the Setup tab UI; mirrors `/onboard` for in-dashboard edits.

The 4 campaign roles (`brand`, `primary_competitor`, `secondary_competitor`, `generic_search`) are populated from `config.json`. All brand/voice strings are templated — see `prompts/templates/`.

## Running

```bash
# First time
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Launch
streamlit run app.py
```

The `claude` CLI must be on `$PATH` and authenticated to a Claude **Pro or Max** plan — every LLM call uses your existing auth, no API key needed. Pro works fine for typical use; heavy fetches and batch drafting may hit Pro's rolling usage cap, in which case Max gives more headroom.

## Common tasks

| Task | How |
| --- | --- |
| Add a new competitor | Edit via the **⚙️ Setup** tab in the dashboard, OR edit `config.json` directly |
| Tune brand voice | Setup tab → Voice & persona section. Saves auto-re-render `prompts/rendered/` |
| Change subreddit filter (region/allowlist) | Setup tab → Subreddit filter |
| Reset / re-onboard for a different brand | `rm config.json && /onboard` (or just edit Setup tab) |
| Inspect what's actually sent to the LLM | Read `prompts/rendered/<campaign>_reply_guidance.md` after a draft |

## What NOT to do

- **Don't put brand-specific strings in code.** Everything brand-related lives in `config.json` or in the `prompts/templates/` files (themselves `$`-templated). Hardcoding a brand name anywhere in `.py` would defeat the OSS design.
- **Don't commit `config.json` or `data/`.** Both are in `.gitignore` — they hold the marketer's local state.
- **Don't modify `config.example.json`** unless you're updating the schema. It's the reference template shipped with the repo.
- **Don't bypass `string.Template.safe_substitute`** in `reply_drafter._load_guidance` — it's intentional that missing placeholders silently leave `${var}` in the output (loud failures break drafting; visible placeholders are debuggable).
