# Street Chatter — Reddit Brand Monitor

[![python](https://img.shields.io/badge/python-3.9+-3776AB?logo=python&logoColor=white&labelColor=555555)](https://www.python.org/)
[![streamlit](https://img.shields.io/badge/streamlit-1.30+-FF4B4B?logo=streamlit&logoColor=white&labelColor=555555)](https://streamlit.io/)
[![playwright](https://img.shields.io/badge/playwright-1.40+-2EAD33?logo=playwright&logoColor=white&labelColor=555555)](https://playwright.dev/python/)
[![chromium](https://img.shields.io/badge/chromium-bundled-4285F4?logo=googlechrome&logoColor=white&labelColor=555555)](https://playwright.dev/python/docs/browsers)
[![claude](https://img.shields.io/badge/claude%20code-required-DA7756?logo=anthropic&logoColor=white&labelColor=555555)](https://docs.claude.com/en/docs/claude-code)
[![license](https://img.shields.io/badge/license-MIT-yellow?labelColor=555555)](LICENSE)

Open-source Reddit brand & competitor monitoring dashboard for marketers. Track mentions of your brand and your competitors across Reddit, score sentiment per-entity (so a post trashing a competitor while switching to you is correctly read as brand-positive), summarize themes, and draft on-brand replies that you can post directly from the dashboard.

Powered by:
- **Streamlit** for the dashboard
- **Reddit search + PullPush** for mention discovery
- **Claude** (via the local `claude` CLI — uses your Pro or Max plan auth, no API key) for entity-aware sentiment, action-type classification, executive summaries, and reply drafting
- **Playwright** for posting replies back to Reddit
- **SQLite** for persistent history

## What it does

Four campaign tabs, each with the same layout — top-line metrics, sentiment summary, trendlines, posts table, comment scraper, actionable panel:

1. **Brand** — your brand and its aliases
2. **Primary competitor** — your top competitors
3. **Secondary competitor** — longer-tail competitors
4. **Generic category search** — broad category keywords (no brand named) where you can plug yourself into a recommendation request

Plus a **Setup** tab where you configure all of this without touching code.

## Quick start

### Option A — Install via Claude Code (recommended)

simple install

```bash

Install this repo: https://github.com/chaitanyashah90/street-chatter/tree/main
```

### Option B 

```bash
git clone git@github.com:chaitanyashah90/street-chatter.git && cd street-chatter

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```
# Make sure `claude` CLI is on your PATH and you're logged in to your
# Claude Pro or Max plan: https://docs.claude.com/en/docs/claude-code

### Option A — guided onboarding via Claude Code (recommended)

Open the repo in [Claude Code](https://docs.claude.com/en/docs/claude-code) and run:

```
/onboard
```

Claude walks you through 8 conversational steps:

1. Brand name + URL
2. Primary + secondary competitors (multiple per bucket)
3. Generic category keywords
4. Brand guidelines doc (optional — Claude will read your PDF/Markdown/URL and extract persona, tone rules, and voice samples for you)
5. Generates `config.json`
6. Shows you a summary; asks for any edits
7. Search logic (region focus, specific subreddits, post-type expectations)
8. Launches `streamlit run app.py` and gives you the URL

Total time: ~5 minutes if you have a brand-guidelines doc handy.

### Option B — manual setup via the dashboard

```bash
streamlit run app.py
```

On first launch the app shows a working **Acme Protein** demo dashboard so you can see the layout. Click the **⚙️ Setup** tab to fill in:

- **Brand**: name, URL, 1-3 sentence description, "what we stand for", and every search keyword / alias / typo to monitor.
- **Voice**: persona, tone rules, optional voice samples (these flow into the reply drafter).
- **Competitors**: primary + secondary, with their search keywords.
- **Generic category keywords**: e.g. `whey protein`, `creatine` if you sell supplements; broad category terms where someone could be plugged a brand recommendation.
- **Subreddit filter**: defaults to `any` (search all of Reddit). Switch to `allowlist` if you want to lock to specific subs (e.g. region-specific).

Click **Save & launch dashboard** and you're in.

## Configuration

All brand-specific configuration lives in `config.json` (git-ignored). The Setup tab is the canonical way to edit it, but you can also edit the JSON directly. See `config.example.json` for the full schema with a placeholder "Acme Protein" brand filled in as a reference.

### Reply guidance templates

Reply drafts use four templates in `prompts/templates/`. They contain `${...}` placeholders (brand_name, brand_keyword, persona, tone_rules, etc.) that get substituted from `config.json` on every draft. The rendered version is written to `prompts/rendered/` so you can inspect what's actually being sent to the model.

Edit voice / tone via the Setup tab. Edit the templates directly only if you want to change the structure of the guidance (e.g. add a section, change the output JSON schema).

## Daily alert (optional)

`check_new_mentions.py` is a single-shot script that fetches new mentions since the last run and fires a macOS banner notification. To wire it up to a scheduler:

- See `com.example.reddit-monitor.plist` for a macOS LaunchAgent template — replace `REPLACE_WITH_REPO_PATH` with your absolute repo path and the Label with your own reverse-DNS string, then `launchctl load` it.
- On Linux, drop it into cron with the equivalent paths.

The banner title comes from `config.app.notification_title`.

## Posting replies

The first time you click **Login to Reddit** in the sidebar, Playwright opens a browser. Log in there once; your session persists in `data/playwright_session/`. From then on, draft → submit posts replies through that session. No API tokens, no app registration, no rate-limit headaches beyond what Reddit imposes on a normal logged-in browser.

## Caveats

- The reply drafter, sentiment, and summary all call the local `claude` CLI. Works with either a Claude **Pro** or **Max** subscription. Pro has tighter usage limits — heavy days (large competitor sweeps, batch drafting 20+ replies) may hit the rolling cap. If you outgrow Pro, upgrade to Max or swap the calls for the Anthropic API SDK with minor edits — every LLM call goes through one `subprocess.run(['claude', '-p', ...])` site per file.
- The keyword sentiment scorer has hand-tuned regexes for "never buy", "scam", "highly recommend", etc. These work for English. The LLM analyzer takes over for entity-aware scoring, so a post mixed across brands is scored correctly per-campaign.
- This is a marketing tool for surfacing relevant posts and drafting on-voice replies. It's not a posting bot — every reply is reviewed by you before submission. Be mindful of subreddit rules and Reddit's content policy.

## File map

```
CLAUDE.md                      # Project context for Claude Code sessions
.claude/commands/onboard.md    # The /onboard slash command
config.py                      # config loader, validator, campaign builder
config.example.json            # schema reference (Acme Protein placeholder)
config.json                    # YOUR config (git-ignored, created on first run)
setup_page.py                  # the Setup form

reddit_monitor.py              # Reddit/PullPush fetch + sentiment + LLM analyzer
reply_drafter.py               # Draft replies via `claude -p`
reddit_poster.py               # Playwright session + posting
summary.py                     # Executive summary via `claude -p`
store.py                       # SQLite persistence
check_new_mentions.py          # Daily-alert script (banner)

app.py                         # Streamlit dashboard
prompts/templates/             # Reply-guidance templates ($-placeholders)
prompts/rendered/              # Rendered output (git-ignored, regenerated)
data/                          # SQLite, caches, browser session (git-ignored)

com.example.reddit-monitor.plist  # macOS LaunchAgent template
```
