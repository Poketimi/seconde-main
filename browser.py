"""Browser tier — a REAL browser running the user's own signed-in session.

This is not a crawler and not a disguise. It exists for one case: sites like
Facebook Marketplace where the content is behind the user's own login, so no
identified crawler could ever be permitted to see it. It runs a plain
Playwright Chromium with no stealth patches, no UA override and no automation
masking, and it stops on any challenge.

Deliberate limitation: this does NOT solve CAPTCHAs or defeat bot detection.
Ricardo, the *Scout24 sites and Facebook all sit behind Cloudflare Turnstile or
a login. The supported route is a PERSISTENT PROFILE that you unlock yourself:

    python3 browser.py login https://www.ricardo.ch/fr

opens a real window; you clear the check (and log in, if the site needs it)
once, by hand. The cookies live in data/browser-profile/ and later headless
runs reuse them. When a challenge reappears, the fetch reports `blocked`
instead of trying to get around it -- rerun the login command.

Playwright is optional: without it every function here degrades to None and
the HTTP-tier adapters keep working.
"""
import sys, time, threading
from pathlib import Path
import config

PROFILE_DIR = config.ROOT / "data" / "browser-profile"
# No hardcoded User-Agent on purpose. A UA claiming Chrome/140 on an engine
# that is actually Chromium 151 contradicts itself, and that mismatch is exactly
# what makes a security check loop forever instead of accepting your click.
# The browser sends its own honest UA.
# Matched against VISIBLE TEXT, not raw HTML: challenge pages open with a huge
# base64 font blob that pushes the giveaway past any fixed byte window, so
# scanning the first N bytes of source reports a false "OK".
CHALLENGE_MARKERS = ("completing the captcha", "vérification rapide de sécurité",
                     "checking your browser", "sécurité de votre connexion",
                     "un instant", "just a moment", "verifying you are human",
                     "captcha", "access denied", "vérification de sécurité")

def available():
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False

def profile_busy():
    """Another Chrome already holds the profile -> every new launch dies.

    Playwright reports this as an opaque TargetClosedError, and a half-dead
    window is also why a security check can appear to loop forever.
    """
    import subprocess
    try:
        out = subprocess.run(["pgrep", "-f", f"user-data-dir={PROFILE_DIR}"],
                             capture_output=True, text=True, timeout=5).stdout.split()
    except Exception:
        return []
    return out

def _free_profile_or_warn():
    """Free the profile, reaping orphans.

    Chromium sometimes survives a crashed or interrupted run and keeps the
    profile locked, which blocks every later scan. Those are orphans and safe
    to kill -- EXCEPT while a login window is legitimately open, which is the
    one case where a live process owns the profile on purpose.
    """
    pids = profile_busy()
    if not pids:
        return True
    if LOGIN_STATE.get("running"):
        print(f"  [browser] fenêtre de connexion ouverte ({len(pids)} proc.) — on attend")
        return False
    import signal, os as _os, subprocess
    # Only reap processes that have been around a while. Killing one that was
    # launched seconds ago takes down a browser we are actively starting, which
    # surfaced as TargetClosedError mid-scan.
    old = []
    for pid in pids:
        try:
            etime = subprocess.run(["ps", "-o", "etimes=", "-p", pid],
                                   capture_output=True, text=True, timeout=3).stdout.strip()
            if etime and int(etime) >= 30:
                old.append(pid)
        except Exception:
            pass
    if not old:
        print(f"  [browser] {len(pids)} processus récents sur le profil — on attend")
        return False
    print(f"  [browser] {len(old)} processus orphelins (>30s) — nettoyage")
    for pid in old:
        try:
            _os.kill(int(pid), signal.SIGTERM)
        except Exception:
            pass
    time.sleep(2)
    for f in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (PROFILE_DIR / f).unlink(missing_ok=True)
        except Exception:
            pass
    return not profile_busy()

def _real_chrome():
    """Prefer the Chrome you actually have installed over Playwright's build."""
    return Path("/Applications/Google Chrome.app").exists()

def _context(pw, headless=True):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    # No --disable-blink-features=AutomationControlled: that flag exists only
    # to hide navigator.webdriver from the page. This browser is honest about
    # being automated; if a site refuses it on that basis, that is the site's
    # call and we stop.
    kw = dict(headless=headless, locale="fr-CH",
              viewport={"width": 1400, "height": 950})
    if _real_chrome():
        kw["channel"] = "chrome"
    try:
        return pw.chromium.launch_persistent_context(str(PROFILE_DIR), **kw)
    except Exception:
        kw.pop("channel", None)          # fall back to the bundled engine
        return pw.chromium.launch_persistent_context(str(PROFILE_DIR), **kw)

def visible_text(html):
    """Rough text extraction: drop script/style, strip tags, collapse space."""
    import re
    t = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t).strip()

_last_reason = ["unknown"]

def last_reason():
    """Why the most recent fetch failed: blocked | locked | unavailable | ok."""
    return _last_reason[0]

LOGIN_WORDS = ("se connecter", "log in", "sign in", "connexion",
               "create new account", "créer un compte", "mot de passe oublié",
               "informations de compte oubliées", "forgot password")

def needs_login(html):
    """A login WALL, not merely a login link in the header.

    Every marketplace has "Se connecter" in its nav, so a keyword alone would
    condemn working sites. A wall is when those words dominate the opening text
    and there is essentially no content behind them.
    """
    txt = visible_text(html)
    head = txt[:400].lower()
    hits = sum(head.count(w) for w in LOGIN_WORDS)
    return hits >= 2 and len(txt) < 400_000

def looks_blocked(html):
    """True when the page is an interstitial rather than real content."""
    txt = visible_text(html)
    head = txt[:3000].lower()
    if any(m in head for m in CHALLENGE_MARKERS):
        return True
    low = html.lower()
    # DataDome (leboncoin) serves a page with NO visible text at all, so the
    # keyword scan finds nothing and the page reads as "fine" while being empty
    if "datadome" in low or "geo.captcha-delivery.com" in low:
        return True
    if len(txt) < 40:
        return True                 # nothing rendered: never usable content
    return len(txt) < 600 and "challenges.cloudflare.com" in low

def check(sites=None):
    """Report whether the stored profile currently gets through each site.

    Run after `login` to see what your session actually buys you, and again a
    day later to see what expired.
    """
    import sources
    targets = sites or list(sources.BROWSER_SEARCH) + ["ricardo"]
    urls = dict(sources.BROWSER_SEARCH,
                ricardo="https://www.ricardo.ch/fr/s/{q}")
    for name in targets:
        url = urls.get(name, "").format(q="test")
        if not url:
            continue
        html = fetch(url, wait_ms=4000)
        if html is None:
            print(f"  {name:15} BLOCKED   -> python3 browser.py login {url.split('?')[0]}")
        else:
            has_data = "__NEXT_DATA__" in html or "__next_f" in html or "ld+json" in html
            print(f"  {name:15} OK        {len(html):>7} bytes"
                  f"{'' if has_data else '  (renders, but no structured data found)'}")

def fetch(url, wait_ms=3500, headless=None):
    """Rendered HTML, or None if unavailable/blocked. Never solves a challenge."""
    if not available():
        _last_reason[0] = "unavailable"
        return None
    if not _BROWSER_LOCK.acquire(timeout=LOCK_TIMEOUT):
        _last_reason[0] = "busy"
        return None
    try:
        return _fetch_locked(url, wait_ms, headless)
    finally:
        _BROWSER_LOCK.release()

def _fetch_locked(url, wait_ms, headless):
    if not _free_profile_or_warn():
        _last_reason[0] = "locked"
        return None
    from playwright.sync_api import sync_playwright
    if headless is None:
        headless = config.BROWSER_HEADLESS
    with sync_playwright() as pw:
        ctx = _context(pw, headless)
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(wait_ms)
            html = page.content()
        except Exception:
            html = None
        finally:
            ctx.close()
    if not html:
        _last_reason[0] = "unavailable"
        return None
    if looks_blocked(html):
        _last_reason[0] = "blocked"
        print(f"  [browser] challenge on {url}")
        return None
    if needs_login(html):
        _last_reason[0] = "login"
        print(f"  [browser] login required for {url}")
        return None
    _last_reason[0] = "ok"
    return html

# Chromium allows exactly ONE process per user-data-dir. Two scans that touch
# the browser at the same time used to kill each other -- all callers failed
# with "profile already open". Serialise instead: the second waits its turn.
_BROWSER_LOCK = threading.RLock()
LOCK_TIMEOUT = 180

# state the web UI polls while a login window is open
LOGIN_STATE = {"site": None, "running": False, "message": ""}

def open_login(url, site=None, timeout_s=900):
    """Open a real window and wait until the user closes it. Thread-safe entry
    point for the web UI -- the terminal `login()` blocks on input(), which a
    Flask request cannot do.

    The user signs in themselves; this never touches credentials and never
    answers a security check.
    """
    if not available():
        LOGIN_STATE.update(running=False, message="playwright non installé")
        return False
    if not _BROWSER_LOCK.acquire(timeout=20):
        LOGIN_STATE.update(running=False,
                           message="un scan utilise le navigateur — réessaie dans un instant")
        return False
    try:
        return _login_locked(url, site, timeout_s)
    finally:
        _BROWSER_LOCK.release()

def _login_locked(url, site, timeout_s):
    if not _free_profile_or_warn():
        LOGIN_STATE.update(running=False,
                           message="une fenêtre est déjà ouverte — ferme-la d'abord")
        return False
    from playwright.sync_api import sync_playwright
    LOGIN_STATE.update(site=site or url, running=True,
                       message="fenêtre ouverte — connecte-toi puis ferme-la")
    ok = False
    try:
        with sync_playwright() as pw:
            ctx = _context(pw, headless=False)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                if not ctx.pages:                 # user closed the window
                    ok = True
                    break
                page.wait_for_timeout(1000)
            try:
                ctx.close()
            except Exception:
                pass
        LOGIN_STATE.update(running=False,
                           message="session enregistrée" if ok else "expiré (15 min)")
    except Exception as e:
        LOGIN_STATE.update(running=False, message=f"échec : {type(e).__name__}")
    return ok

def eval_page(url, js, wait_ms=6000, wait_for=None):
    """Load a page and run JS in it, returning the result.

    Needed for sites that render everything client-side and expose no
    JSON blob (Facebook), where the DOM is the only source of data.
    """
    if not available():
        _last_reason[0] = "unavailable"
        return None
    if not _BROWSER_LOCK.acquire(timeout=LOCK_TIMEOUT):
        _last_reason[0] = "busy"
        return None
    try:
        return _eval_locked(url, js, wait_ms, wait_for)
    finally:
        _BROWSER_LOCK.release()

def _eval_locked(url, js, wait_ms, wait_for):
    if not _free_profile_or_warn():
        _last_reason[0] = "locked"
        return None
    from playwright.sync_api import sync_playwright
    out = None
    with sync_playwright() as pw:
        ctx = _context(pw, headless=config.BROWSER_HEADLESS)
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            settled = False
            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=wait_ms)
                    settled = True
                except Exception:
                    pass
            # Once the cards are in the DOM, a short settle is enough; the full
            # wait only applies when we never saw them.
            page.wait_for_timeout(1500 if settled else wait_ms)
            html = page.content()
            if looks_blocked(html):
                _last_reason[0] = "blocked"
            elif needs_login(html):
                _last_reason[0] = "login"
            else:
                out = page.evaluate(js)
                _last_reason[0] = "ok"
        except Exception:
            _last_reason[0] = "unavailable"
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    return out

AUTH_COOKIES = ("auth0", "sessionid", "c_user", "xs", "sid", "session",
                "li_at", "access_token", "SMGSESSION", "JSESSIONID")

def session_info(domain):
    """What the stored profile actually holds for a site.

    Logging in and passing the bot check are SEPARATE gates: autoscout24 can
    hold a valid auth0 login while Cloudflare still refuses the automated
    browser. Without showing both, a successful login looks like it did
    nothing at all.
    """
    db_path = PROFILE_DIR / "Default" / "Cookies"
    out = {"logged_in": False, "clearance": False, "names": []}
    if not db_path.exists():
        return out
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        rows = con.execute("SELECT host_key, name FROM cookies").fetchall()
        con.close()
    except Exception:
        return out
    key = domain.replace("www.", "")
    for host, name in rows:
        if key not in host:
            continue
        out["names"].append(name)
        if name in AUTH_COOKIES:
            out["logged_in"] = True
        if name == "cf_clearance":
            out["clearance"] = True
    return out

def probe(url):
    """Fetch once and report why it failed, without storing anything."""
    html = fetch(url, wait_ms=4000)
    return ("ok", f"{len(html)} octets") if html else (last_reason(), "")

def login(url):
    """Open a real window so YOU can clear the check / sign in. Cookies persist."""
    if not available():
        print("playwright missing:  pip install playwright && python3 -m playwright install chromium")
        return
    if not _free_profile_or_warn():
        return
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx = _context(pw, headless=False)
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        print(f"Window open on {url}.\n"
              "Clear the security check / sign in yourself, then press Enter here.")
        try:
            input()
        except EOFError:
            page.wait_for_timeout(120000)
        ctx.close()
    print(f"Session saved to {PROFILE_DIR}")

if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "login":
        login(sys.argv[2])
    elif len(sys.argv) > 1 and sys.argv[1] == "check":
        check(sys.argv[2:] or None)
    else:
        print(__doc__)
        print("playwright installed:", available())
