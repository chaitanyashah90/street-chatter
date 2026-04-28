---
description: Walk a new marketer through setting up their brand monitor and launch the dashboard
---

You are running the onboarding flow for a marketer who just cloned this Reddit brand-monitor repo. Walk them through the 9 steps below **in order, one at a time**. Be conversational — confirm understanding after each step before moving on. Don't ask many questions at once.

After collecting everything, write `config.json` and start the Streamlit dashboard.

# Before you begin

1. Read [config.example.json](config.example.json) to understand the schema and pick up sane defaults for `campaign_caps`, `app.user_agent`, and `app.notification_title`.
2. Greet the marketer briefly: "I'll set up your brand monitor in 9 quick steps — should take 5-7 minutes. You can skip any step and edit later from the Setup tab."

---

## Step 1 — Brand identity

Ask for:
- **Brand name** (display name, e.g. "CoffeeHabit")
- **Brand URL** (e.g. "https://coffeehabit.com" — optional)
- **One-line description** (what does the brand do?)
- **What does the brand stand for?** (positioning / values, 1-2 sentences)

Keep these as free-text prompts. Don't use AskUserQuestion for these.

---

## Step 2 — Competitors

Explain that competitors are split into two buckets:
- **Primary**: high-priority rivals you most want to plug your brand against
- **Secondary**: longer-tail or less-direct rivals

For each bucket, ask the marketer to paste a list, **one competitor per line**, in the format:

```
Brand Name | keyword1, keyword2, keyword3
```

The keywords are every spelling/alias/typo Reddit users mention them by. If they only give a name, default the keyword to the lowercase name. Multiple competitors per bucket are expected — keep accepting until they say "done".

Show them what you parsed before moving on.

---

## Step 3 — Generic category keywords

Explain: these are **broad category terms** where someone could naturally be plugged a brand recommendation but isn't naming a specific brand. Example for a coffee brand: `single origin coffee`, `pour over`, `coffee subscription`.

Ask for one per line. Accept 3-10 keywords typically.

Optionally also ask for **category hooks** — short notes about when to plug for each category. Example: `Pour-over questions: mention us if OP is asking where to source fresh beans.` This is optional — skip if they don't have a strong opinion yet.

---

## Step 4 — Brand guidelines (optional but high-value)

Ask: "Do you have a brand guidelines document, voice/tone guide, or messaging doc you'd like me to ingest? It can be a local file path (PDF, .docx, .md, .txt) or a public URL."

**If they provide one:**
- For a local path: use the Read tool. For PDFs, use the `pages` parameter as needed.
- For a URL: use WebFetch.
- Extract from it:
  - **Persona** (1-2 sentences: who is the reply author? age, vibe, where they post)
  - **Tone rules** (3-6 hard rules: dos and don'ts, length limits, language register)
  - **Voice samples** (2-4 short example lines that capture the voice)
- Show the marketer what you extracted in plain prose, and ask "Does this match how you want to sound?" Accept tweaks.

**If they don't have one:** offer them the **default baseline persona** (already in `config.example.json`) and ask if they want to use it or override:

> Default baseline: "25-year-old Reddit user. English-speaking. Soft-spoken, empathetic, and direct. Keeps replies short."
>
> Default tone rules: no marketing speak, no bullets/headings, 10-40 words, soft-spoken and direct, match the OP's register, no disclaimers, address the actual question.

If they accept the default, copy `voice` directly from `config.example.json`. If they want to override, ask the three questions yourself, free-text, one at a time:
1. Persona — describe the kind of person who would write your replies on Reddit.
2. Tone rules — 3-5 dos/don'ts (e.g. "no marketing speak", "30-80 words max").
3. Voice samples — 1-3 example lines (paraphrased real customer comments work great).

---

## Step 5 — Per-campaign reply guidance

Tell the marketer: "Now for the per-campaign reply playbook. Replies fall into 4 modes — I'll ask you how you want each to feel. Be concrete: when to acknowledge, when to push back, when to plug, when to stay silent."

Ask one at a time. For each, accept a free-text paragraph (or bullets). If the marketer is unsure, offer the default from `config.example.json`'s `campaign_guidance` block and ask "use this default, or want to override?".

### 5a — Brand replies

> "How should the reply behave when the post is about your brand — a complaint, a praise, or an 'is it legit?' question? E.g. 'On complaints, lead with empathy and offer a concrete next step. On praise, validate with one specific personal echo, never over-the-top.'"

### 5b — Primary competitor replies

> "How should the reply behave when the post mentions a primary competitor? E.g. 'Never trash them. Acknowledge the OP's pain. Mention switching to <brand> casually, like a friend's tip — one line max.'"

### 5c — Secondary competitor replies

> "Same as primary, or different? Most brands keep this softer / more optional. The default is to be even more casual and sometimes skip the plug entirely."

### 5d — Generic category replies

> "When the OP isn't naming a brand, how should the reply behave? E.g. 'Lead with expertise. Plug only when natural — recommendation requests, where-to-buy, comparisons. Skip the plug for medical / dosage / deal-share posts.'"

After each, paraphrase what they said into a tight paragraph and confirm. Use those paragraphs as the values in `config.json`'s `campaign_guidance` block. If the marketer accepted the default for any, copy from `config.example.json`'s `campaign_guidance.<key>`.

---

## Step 6 — Generate config.json

Now build the config and write it to `config.json`. Structure:

```json
{
  "brand": {
    "name": "<step 1>",
    "url": "<step 1>",
    "description": "<step 1>",
    "what_we_stand_for": "<step 1>",
    "search_keywords": ["<from name + any aliases the marketer gave>"],
    "voice": {
      "persona": "<step 4>",
      "tone_rules": ["..."],
      "examples": ["..."]
    }
  },
  "competitors": {
    "primary":   [{"name": "...", "search_keywords": ["..."]}],
    "secondary": [{"name": "...", "search_keywords": ["..."]}]
  },
  "generic_search": {
    "label": "Generic category search",
    "search_keywords": ["..."],
    "category_hooks": ["..."]
  },
  "campaign_guidance": {
    "brand": "<step 5a>",
    "primary_competitor": "<step 5b>",
    "secondary_competitor": "<step 5c>",
    "generic_search": "<step 5d>"
  },
  "subreddit_filter": {
    "mode": "any",
    "allowlist": [],
    "name_substrings": [],
    "include_user_pages": true
  },
  "campaign_caps": <copy from config.example.json>,
  "app": <copy from config.example.json — they can rename notification_title to include their brand>
}
```

Important:
- `search_keywords` for the brand: include the brand name lowercased, plus any space-stripped variant (e.g. "coffeehabit" + "coffee habit"). Ask the marketer if there are more aliases / common typos.
- The `subreddit_filter` defaults to `any` for now; we'll refine in step 7.
- Write to `config.json` — **never** modify `config.example.json`.

Use Python's `json.dumps(cfg, indent=2)` style indentation when writing.

---

## Step 7 — Show & confirm

Print a summary back to the marketer:
- Brand: `<name>` — `<url>`
- Brand description: `<one line>`
- Brand stands for: `<one line>`
- Search keywords (brand): N keywords (`<list>`)
- Primary competitors: N (`<names>`)
- Secondary competitors: N (`<names>`)
- Generic keywords: N (`<list>`)
- Voice persona: `<one line excerpt>`
- Tone rules: N rules
- Voice samples: N
- Campaign guidance: 4 blocks (brand / primary / secondary / generic) — first 60 chars of each

Ask: "Anything to change before we configure the search logic?"

If yes, take their corrections and re-write `config.json`. Loop until they're satisfied. **Common edits**: add a missed competitor keyword, soften a tone rule, swap a voice sample, refine campaign guidance.

---

## Step 8 — Search logic

Ask three sub-questions, one at a time:

### 8a — Region focus

Use AskUserQuestion with these options:
- "All of Reddit (no filter)" → `subreddit_filter.mode = "any"`
- "India-focused" → `mode = "allowlist"`, `name_substrings = ["india", "desi", "bharat"]`
- "Specific subreddits only" → `mode = "allowlist"`, populate from step 7b
- "Custom name patterns" → ask for substrings to match (e.g. "us", "uk", "australia"), populate `name_substrings`

### 8b — Specific channels

Ask: "Any specific subreddits you want to include? Paste them one per line, no `r/` prefix needed (e.g. `coffee`, `pourover`, `cafe`)." Add them to `subreddit_filter.allowlist`.

If they answered "Specific subreddits only" in 8a, this is mandatory. Otherwise it's optional.

### 8c — Post types

Tell them: "The dashboard auto-classifies every post into one of these types: `recommendation_request`, `complaint`, `praise`, `deal_share`, `general_discussion`, `off_topic`. The Actionable panel surfaces the relevant types per campaign automatically. You'll see this on the dashboard — you can filter or skip individual posts there."

This is informational; no config write needed for now.

After 8a and 8b, update `config.json` with the new `subreddit_filter` block.

---

## Step 9 — Install dependencies & launch

Tell the marketer: "Your config is saved at `config.json`. I'll install dependencies and launch the dashboard now — this takes 1-3 minutes the first time."

Then run, **in this order**, surfacing progress to the marketer at each step:

### 9a — Ensure the venv exists

```bash
test -x .venv/bin/python || python3 -m venv .venv
```

### 9b — Install Python dependencies

```bash
.venv/bin/pip install -q -r requirements.txt
```

`pip` is a no-op if already installed, so this is safe and fast on re-runs. The `-q` flag keeps the output short.

### 9c — Install Playwright Chromium

This is **required** for the "Login to Reddit" / posting feature. Run it as part of onboarding so the marketer doesn't hit a missing-browser error later.

```bash
.venv/bin/playwright install chromium
```

`playwright install` is idempotent — if Chromium is already installed for the current Playwright version, this is a fast no-op. If not, it downloads ~150 MB.

If the install fails (e.g. no network), warn the marketer:

> "Playwright Chromium install failed. The dashboard will still work for monitoring, but you won't be able to post replies until you run `.venv/bin/playwright install chromium` manually. Continuing…"

Don't block onboarding on this.

### 9d — Verify the `claude` CLI

```bash
which claude || echo "MISSING"
```

If missing, warn:

> "The `claude` CLI isn't on your PATH. Sentiment, summaries, and reply drafting need it. See https://docs.claude.com/en/docs/claude-code to install. The dashboard will still load, but LLM-powered features won't work until `claude` is available."

Don't block on this either — let them launch and fix later.

### 9e — Launch Streamlit in the background

```bash
.venv/bin/streamlit run app.py --server.headless true --browser.gatherUsageStats false
```

Use the Bash tool with `run_in_background: true`.

### 9f — Report the URL

Wait ~5 seconds, then tell the marketer:

> "Dashboard is up at **http://localhost:8501** (open it in your browser).
>
> What you'll see:
> - 4 campaign tabs (`🏷️ Brand`, `⚔️ Primary Competitor`, `🔍 Secondary Competitor`, `🌐 Generic`) plus an `⚙️ Setup` tab to edit any of this later.
> - Sidebar: editable search keywords per campaign, time window, **Login to Reddit** (Playwright one-time login — needed before you can post replies).
> - First load of any tab fetches live from Reddit and runs the LLM analyzer — give it ~30-60 seconds.
>
> When you're ready to draft + post replies: click **Login to Reddit** in the sidebar once, log in to your account in the browser window that opens, close it, and you're set. Drafts then post via that session."

---

# Important behaviour notes

- **Be conversational**: one step at a time, confirm understanding before moving on.
- **Make reasonable defaults**: if the marketer is unsure about a question, default to the value in `config.example.json` and tell them they can refine later via the Setup tab in the dashboard.
- **Preserve unspecified fields**: when you write `config.json`, copy over `campaign_caps` and `app` defaults from `config.example.json` unless the marketer specified otherwise.
- **Don't over-engineer**: if the marketer wants to skip a step ("I don't have brand guidelines"), skip cleanly and move on. The Setup tab is always there for later.
- **Brief but clear**: in your questions, use Markdown lists for multi-part asks. One short paragraph of context per step is plenty.
