"""Post a comment to a Reddit submission via Playwright (non-headless).

Reuses a persistent browser context at data/playwright_session/ so the user
logs in once and the session sticks. Two entry points:

  python reddit_poster.py login
      Opens Reddit in a real Chrome window. User logs in manually (handles 2FA,
      captchas, anything). Browser closes when user is done; cookies persist.

  python reddit_poster.py post <post_url> <reply_text_path>
      Navigates to the post, opens the reply box, fills the text from the
      given file path, and submits. Returns success/failure as JSON on stdout.

Streamlit calls the `post` mode via subprocess so the browser runs in its own
process — Playwright sync API cannot share the loop with Streamlit.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from playwright.sync_api import (
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

ROOT = Path(__file__).parent
SESSION_DIR = ROOT / "data" / "playwright_session"
SESSION_DIR.mkdir(parents=True, exist_ok=True)


def _launch(p: Playwright, headless: bool = False) -> BrowserContext:
    return p.chromium.launch_persistent_context(
        user_data_dir=str(SESSION_DIR),
        headless=headless,
        viewport={"width": 1280, "height": 900},
        args=[
            # The default chromium fingerprint is detectable; these reduce flags.
            "--disable-blink-features=AutomationControlled",
        ],
    )


def login_flow() -> dict:
    """Open Reddit and let the user log in manually. Verifies session before exiting."""
    with sync_playwright() as p:
        ctx = _launch(p, headless=False)
        page = ctx.new_page()
        page.goto("https://www.reddit.com/login/", wait_until="domcontentloaded")
        print(
            "Reddit login page opened. Log in fully (wait until you see your "
            "logged-in homepage), then close the browser window."
        )
        # Wait for the browser to be closed by the user (or for ~10 min).
        try:
            page.wait_for_event("close", timeout=10 * 60 * 1000)
        except PlaywrightTimeoutError:
            pass

        # Re-open a hidden page in the same context to verify the session.
        try:
            verify_page = ctx.new_page()
            verify_page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=20000)
            ok, diag = _is_logged_in(verify_page)
        except Exception as e:
            ok, diag = False, f"verify exception: {type(e).__name__}: {e}"

        ctx.close()

    if ok:
        return {
            "ok": True,
            "msg": (
                f"Login verified — {diag}. You can close this notification "
                "and submit replies from the Actionable panel."
            ),
        }
    return {
        "ok": False,
        "msg": (
            f"Login NOT detected ({diag}). Common causes: you closed the "
            "browser before the homepage finished loading, or you used a "
            "passwordless flow that didn't save cookies. Click 'Login to "
            "Reddit (one-time)' again, log in fully, wait until you see "
            "your username at the top of reddit.com, then close the window."
        ),
    }


def _is_logged_in(page: Page) -> tuple[bool, str]:
    """Robust login check via Reddit's /api/me.json using the page's cookies.

    Returns (logged_in, debug_message). The API endpoint returns user data
    when logged in (has `data.name`) and an empty object `{}` when not — so
    this works regardless of UI / shadow-DOM changes.

    Also falls back to a couple of UI selectors as a tertiary check (some
    rate-limited or A/B-tested cases return non-200 from /api/me.json even
    when logged in).
    """
    # Primary: API check
    try:
        resp = page.context.request.get(
            "https://www.reddit.com/api/me.json", timeout=10000
        )
        if resp.ok:
            try:
                payload = resp.json()
                name = (
                    (payload or {}).get("data", {}).get("name")
                    if isinstance(payload, dict)
                    else None
                )
                if name:
                    return True, f"logged in as u/{name} (via /api/me.json)"
                return False, "/api/me.json returned no user data — not logged in"
            except Exception as e:
                pass  # fall through to UI fallback
        # If non-OK status, fall through.
    except Exception as e:
        pass

    # Fallback: UI selectors (best-effort)
    candidates = [
        "header [aria-label*='Open user menu' i]",
        "[data-testid='user-drawer-button']",
        "faceplate-tracker[noun='user-drawer']",
        "button[aria-label*='Expand user menu' i]",
        "shreddit-app:has(faceplate-tracker[noun='user-drawer'])",
    ]
    for sel in candidates:
        try:
            if page.locator(sel).count() > 0:
                return True, f"matched UI selector {sel!r}"
        except Exception:
            continue
    return False, "no UI selectors matched and /api/me.json gave no name"


def _find_reply_textbox(page: Page):
    """Return a Playwright Locator for the OPEN comment composer's contenteditable.

    Reddit's modern composer (shreddit) collapses by default and only renders
    the Lexical contenteditable as visible after the trigger button is clicked.
    Prefers the Lexical editor (`data-lexical-editor='true'`) over generic
    contenteditable matches. Returns the first visible match, falling back
    to any match if none is visible.
    """
    sels = [
        # Old reddit textarea
        "textarea[name='text']",
        # Modern shreddit Lexical editor (the actual typing target after expand)
        "shreddit-composer div[data-lexical-editor='true']",
        "div[data-lexical-editor='true']",
        # Generic contenteditable inside the composer
        "shreddit-composer div[role='textbox'][contenteditable='true']",
        "shreddit-composer textarea",
        "[name='comment'] textarea",
        "div[role='textbox'][contenteditable='true']",
        "textarea[placeholder*='comment' i]",
    ]
    for sel in sels:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            continue
    for sel in sels:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def _open_composer(page: Page) -> bool:
    """Click Reddit's collapsed comment composer trigger to expand it.

    Reddit (shreddit) wraps the comment box in `<comment-composer-host>` with
    a collapsed `<faceplate-textarea-input data-testid='trigger-button'
    placeholder='Join the conversation'>` placeholder. Clicking it expands the
    actual Lexical editor inside `<shreddit-composer>`. Returns True once a
    click successfully landed.

    After this returns True, wait briefly (~700ms) and call _find_reply_textbox
    to grab the now-visible contenteditable.
    """
    triggers = [
        # Modern shreddit (data-testid set on the placeholder textarea-input)
        "faceplate-textarea-input[data-testid='trigger-button']",
        "[data-testid='trigger-button']",
        # Comment-composer-host wrapper (clicking anywhere on it works too)
        "comment-composer-host faceplate-textarea-input",
        "comment-composer-host",
        # Placeholder text fallback
        "faceplate-textarea-input[placeholder*='Join the conversation' i]",
        "[aria-placeholder*='Join the conversation' i]",
        # Older shreddit naming
        "[data-testid='comment-textbox']",
        "[data-testid='add-comment-textarea']",
        "button:has-text('Add a comment')",
        "button[aria-label*='Add a comment' i]",
        "div[aria-label*='Add a comment' i]",
        # Generic shreddit-composer last resort
        "shreddit-composer",
        # Old reddit textarea (already interactive)
        "textarea[name='text']",
    ]
    for sel in triggers:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            try:
                loc.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            try:
                loc.click(timeout=2000, force=False)
                print(f"[poster] opened composer via {sel!r}", flush=True, file=sys.stderr)
                return True
            except Exception:
                try:
                    loc.click(timeout=2000, force=True)
                    print(f"[poster] opened composer via {sel!r} (force)", flush=True, file=sys.stderr)
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


def _click_submit_button(page: Page) -> bool:
    """Click the comment-submit button. Returns True if clicked."""
    sels = [
        "shreddit-composer button[type='submit']",
        "button:has-text('Comment')",
        "button:has-text('Post')",
        "button[type='submit']:has-text('Comment')",
    ]
    for sel in sels:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_enabled():
                btn.click()
                return True
        except Exception:
            continue
    return False


def _post_one_in_page(page: Page, post_url: str, reply_text: str) -> dict:
    """Post one reply on an existing browser page. Returns the result dict.

    Caller manages the browser context lifecycle. Use this for single or
    bulk posting (one shared context, many sequential posts).
    """
    def _log(msg: str) -> None:
        print(f"[poster] {msg}", flush=True, file=sys.stderr)

    try:
        _log(f"goto {post_url}")
        page.goto(post_url, wait_until="domcontentloaded", timeout=45000)
        _log("goto done")

        # Accept any cookie banners.
        for label in ["Accept all", "Allow all", "Accept"]:
            try:
                btn = page.get_by_role("button", name=label, exact=False)
                if btn.count() > 0:
                    btn.first.click(timeout=2000)
                    break
            except Exception:
                pass

        _log("checking login")
        logged_in, diag = _is_logged_in(page)
        _log(f"login: ok={logged_in} diag={diag}")
        if not logged_in:
            return {
                "ok": False,
                "msg": (
                    f"Not logged in ({diag}). "
                    "Click the sidebar's 'Login to Reddit (one-time)' "
                    "button — log in in the browser that opens, then "
                    "close that window and retry the submission."
                ),
                "diag": diag,
            }

        # Open the (likely collapsed) composer first.
        _log("opening composer")
        opened = _open_composer(page)
        _log(f"composer opened: {opened}")
        page.wait_for_timeout(900)

        _log("locating textbox")
        box = _find_reply_textbox(page)
        if box is None:
            _log("textbox NOT found — aborting")
            return {
                "ok": False,
                "msg": "Could not locate the comment textbox. Reddit UI may have changed.",
            }
        _log("textbox found")

        # Focus: try click → fall back to JS .focus() if blocked.
        focused = False
        try:
            box.click(timeout=2500)
            focused = True
            _log("textbox click OK")
        except Exception as e:
            _log(f"textbox click failed: {e}; trying JS focus")
            try:
                box.evaluate("el => el.focus()")
                focused = True
                _log("textbox focused via JS")
            except Exception as e2:
                _log(f"JS focus also failed: {e2}")
        if not focused:
            return {
                "ok": False,
                "msg": (
                    "Couldn't focus the comment composer. The composer is "
                    "probably still collapsed / behind a stub element. "
                    "Open Reddit manually, click into the comment box once, "
                    "then retry."
                ),
            }
        page.wait_for_timeout(300)

        # Clear any auto-saved draft, then insert.
        try:
            page.keyboard.press("Meta+A")
            page.wait_for_timeout(80)
            page.keyboard.press("Backspace")
            page.wait_for_timeout(120)
        except Exception as e:
            _log(f"clear keys failed (continuing): {e}")

        # Strategy A: classic textarea — fill() handles clear+set atomically.
        filled = False
        try:
            box.fill(reply_text, timeout=3000)
            filled = True
            _log("filled via box.fill()")
        except Exception as e:
            _log(f"box.fill failed: {e}; trying insert_text")

        if not filled:
            try:
                page.keyboard.insert_text(reply_text)
                filled = True
                _log("filled via insert_text")
            except Exception as e:
                _log(f"insert_text failed, trying keyboard.type: {e}")
                page.keyboard.type(reply_text, delay=15)
                filled = True

        page.wait_for_timeout(600)
        _log("verifying composer content")

        # Read back & verify before submitting.
        actual = ""
        try:
            actual = box.input_value()
        except Exception:
            try:
                actual = box.evaluate(
                    "el => el.innerText || el.textContent || el.value || ''"
                ) or ""
            except Exception as e:
                _log(f"readback eval failed: {e}")
        actual_norm = (actual or "").strip()
        expected_norm = reply_text.strip()
        _log(f"read back len={len(actual_norm)} (expected {len(expected_norm)})")

        if not actual_norm:
            # Empty composer = the focus didn't land or text went nowhere.
            # ABORT — don't submit a blank comment.
            return {
                "ok": False,
                "msg": (
                    "Composer is empty after fill — focus probably didn't "
                    "land on the contenteditable. Refresh Reddit, click into "
                    "the comment box once manually so it expands, then retry."
                ),
            }
        if expected_norm and not actual_norm.endswith(expected_norm[-30:]):
            return {
                "ok": False,
                "msg": (
                    f"Composer text mismatch — expected to end with "
                    f"…{expected_norm[-30:]!r}, got …{actual_norm[-30:]!r}. "
                    "Aborted before submitting."
                ),
            }

        page.wait_for_timeout(400)
        _log("clicking submit")
        clicked = _click_submit_button(page)
        if not clicked:
            _log("submit button NOT found / not enabled")
            return {
                "ok": False,
                "msg": "Filled the textbox but couldn't find an enabled Comment/Post button.",
            }
        _log("submit clicked, waiting for post to register")

        page.wait_for_timeout(3500)
        _log("done")

        return {
            "ok": True,
            "msg": "Reply submitted. Verify on the post page.",
            "post_url": post_url,
        }
    except PlaywrightTimeoutError as e:
        _log(f"PlaywrightTimeoutError: {e}")
        return {"ok": False, "msg": f"Timeout: {e}"}
    except Exception as e:
        _log(f"Exception: {type(e).__name__}: {e}")
        return {"ok": False, "msg": f"Error: {type(e).__name__}: {e}"}


def post_comment(post_url: str, reply_text: str) -> dict:
    """Single-post wrapper: launches Playwright, posts once, closes."""
    with sync_playwright() as p:
        ctx = _launch(p, headless=False)
        page = ctx.new_page()
        try:
            return _post_one_in_page(page, post_url, reply_text)
        finally:
            time.sleep(1.5)
            ctx.close()


def post_bulk(jobs: list[dict]) -> list[dict]:
    """Post a list of replies in a single browser session.

    `jobs` is a list of {"post_id": "...", "url": "...", "text": "..."} dicts.
    Returns a parallel list of result dicts, each with the original `post_id`
    plus `ok` / `msg` fields. Stops early if no jobs.
    """
    out: list[dict] = []
    if not jobs:
        return out
    with sync_playwright() as p:
        ctx = _launch(p, headless=False)
        page = ctx.new_page()
        try:
            for i, job in enumerate(jobs):
                pid = job.get("post_id", "")
                url = job.get("url", "")
                text = job.get("text", "")
                if not url or not text:
                    out.append(
                        {"post_id": pid, "ok": False, "msg": "missing url or text"}
                    )
                    continue
                print(f"[poster] {i+1}/{len(jobs)} → {url}")
                result = _post_one_in_page(page, url, text)
                result["post_id"] = pid
                out.append(result)
                # Polite pause between posts to avoid Reddit's anti-spam.
                page.wait_for_timeout(2500)
        finally:
            time.sleep(1.5)
            ctx.close()
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(json.dumps({"ok": False, "msg": "Usage: reddit_poster.py login | post <url> <text-path> | bulk <jobs.json>"}))
        return 2
    cmd = argv[1]
    if cmd == "login":
        print(json.dumps(login_flow()))
        return 0
    if cmd == "post":
        if len(argv) < 4:
            print(json.dumps({"ok": False, "msg": "post needs <url> <text-path>"}))
            return 2
        url = argv[2]
        text_path = Path(argv[3])
        text = text_path.read_text(encoding="utf-8")
        result = post_comment(url, text)
        print(json.dumps(result))
        return 0 if result.get("ok") else 1
    if cmd == "bulk":
        if len(argv) < 3:
            print(json.dumps({"ok": False, "msg": "bulk needs <jobs.json>"}))
            return 2
        jobs_path = Path(argv[2])
        try:
            jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(json.dumps({"ok": False, "msg": f"bad jobs file: {e}"}))
            return 2
        results = post_bulk(jobs)
        print(json.dumps({"ok": True, "results": results}))
        return 0
    print(json.dumps({"ok": False, "msg": f"unknown command: {cmd}"}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
