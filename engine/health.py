"""Santé des sources, retraits exponentiels, reconnexion, notifications."""
import json, re, time, threading, subprocess, shutil, traceback
from concurrent.futures import ThreadPoolExecutor
import db, ai, geo, sources, config, i18n

def _as_str(s):
    """Quote for AppleScript: it accepts neither JSON \\uXXXX escapes nor raw quotes."""
    s = "".join(c for c in str(s) if c.isprintable())
    return '"' + s.replace("\\", "").replace('"', "'") + '"'

_NOTIFIER = shutil.which("terminal-notifier")

def notify(title, body, url=None):
    """macOS notification. Clickable when terminal-notifier is installed:
    `brew install terminal-notifier` -- osascript alone cannot open a URL."""
    try:
        if _NOTIFIER:
            cmd = [_NOTIFIER, "-title", str(title), "-message", str(body),
                   "-group", "seconde-main", "-sound", "Ping"]
            if url:
                cmd += ["-open", url]
            subprocess.run(cmd, check=False, timeout=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.run(["osascript", "-e",
                            f"display notification {_as_str(body)} with title {_as_str(title)}"],
                           check=False, timeout=5)
    except Exception:
        pass

_active = set()

_active_lock = threading.Lock()

def is_scanning():
    with _active_lock:
        return sorted(_active)

RECOVERY_HOME = {
    "fb_marketplace": "https://www.facebook.com/marketplace/",
    "autoscout24": "https://www.autoscout24.ch/fr",
    "motoscout24": "https://www.motoscout24.ch/fr",
    "immoscout24": "https://www.immoscout24.ch/fr",
    "ricardo": "https://www.ricardo.ch/fr",
}

RECOVERY_EVERY = 3600          # at most one attempt per source per hour

_last_recovery = {}

def try_recover(source, query, spec=None):
    """One silent attempt to bring a failed source back. Returns rows or []."""
    home = RECOVERY_HOME.get(source)
    if not home:
        return []
    if time.time() - _last_recovery.get(source, 0) < RECOVERY_EVERY:
        return []
    _last_recovery[source] = time.time()
    print(f"  [{source}] tentative de reconnexion automatique…")
    import browser
    browser.fetch(home, wait_ms=4000)          # refresh cookies with the profile
    rows = sources.search(source, query, spec)
    if rows:
        print(f"  [{source}] reconnecté ({len(rows)} annonces)")
    return rows

CYCLE_TALLY = {}

def commit_cycle_health():
    for src, t in CYCLE_TALLY.items():
        record_health(src, t["status"], t["detail"])
    CYCLE_TALLY.clear()

STATUS_LABEL = {"ok": "OK", "empty": "aucun résultat", "blocked": "bloqué",
                "login": "connexion requise", "error": "erreur"}

def record_health(source, status, detail=""):
    """Persist per-source state and alert when a working source goes down."""
    now = time.time()
    prev = db.q("SELECT * FROM source_health WHERE source=?", (source,), one=True)
    was_ok = bool(prev) and prev["status"] == "ok"
    streak = 0 if status == "ok" else ((prev["fail_streak"] if prev else 0) + 1)
    changed = now if (not prev or prev["status"] != status) else prev["changed_at"]
    db.run("""INSERT INTO source_health(source,status,detail,fail_streak,last_ok,last_run,changed_at)
              VALUES(?,?,?,?,?,?,?)
              ON CONFLICT(source) DO UPDATE SET status=?,detail=?,fail_streak=?,
                last_ok=COALESCE(?,source_health.last_ok),last_run=?,changed_at=?""",
           (source, status, detail, streak, now if status == "ok" else None, now, changed,
            status, detail, streak, now if status == "ok" else None, now, changed))
    if was_ok and status != "ok":
        notify(f"Source indisponible : {source}",
               f"{STATUS_LABEL.get(status, status)} — {detail or 'voir le journal'}",
               url="http://localhost:5055/sources")
    elif prev and not was_ok and status == "ok":
        notify(f"Source rétablie : {source}", "les scans reprennent normalement",
               url="http://localhost:5055/sources")

def in_backoff(source):
    """Should we leave this source alone for now?

    Retrying a blocked source every cycle is what turns a temporary challenge
    into a permanent one.
    """
    r = db.q("SELECT status, fail_streak, last_run FROM source_health WHERE source=?",
             (source,), one=True)
    if not r or not r["last_run"] or (r["fail_streak"] or 0) < 1:
        return 0
    # Only back off on an actual refusal. "empty" usually means the query had no
    # results -- backing off on it paused ricardo for 20 minutes while it was
    # happily returning 60 listings.
    if r["status"] not in ("blocked", "login", "error"):
        return 0
    wait = min(config.BACKOFF_MAX, config.BACKOFF_BASE * (2 ** min(r["fail_streak"] - 1, 8)))
    left = (r["last_run"] + wait) - time.time()
    return max(0, left)

def forget_removed_sources():
    """Oublie la santé des sources qui n'ont plus d'adaptateur.

    Retirer une source laissait sa ligne dans source_health, affichée
    indéfiniment en « erreur — aucun adaptateur » sur /sources : une panne
    permanente pour quelque chose qu'on a délibérément enlevé.
    """
    known = set(sources.ADAPTERS)
    stale = [r["source"] for r in db.q("SELECT source FROM source_health")
             if r["source"] not in known]
    for s in stale:
        db.run("DELETE FROM source_health WHERE source=?", (s,))
    return stale

def health():
    return db.q("SELECT * FROM source_health ORDER BY source")

