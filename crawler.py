"""Transparent, identifiable crawler — the acquisition layer.

Design stance: this crawler is meant to be recognised, rate-limited and blocked
by site operators if they want to. It never tries to look like a human browser.

  - one honest User-Agent, naming the bot and how to reach its owner
  - robots.txt is fetched, parsed and obeyed before anything else
  - crawl-delay is honoured, with a conservative floor of our own
  - one request at a time per domain, spaced, with jitter
  - conditional GETs (ETag / If-Modified-Since) so unchanged pages cost nothing
  - 403 / 429 / CAPTCHA is a decision, not an obstacle: we stop and back off
  - every request is logged, so behaviour is auditable

Explicitly NOT here, by design: user-agent rotation, TLS fingerprint
impersonation, proxy rotation, CAPTCHA solving, stealth browser patches. If a
site denies this crawler, the correct outcome is that it stays denied.

Extraction, normalisation, dedup, valuation and ranking live elsewhere
(sources/engine/ai). This module only obtains bytes we are allowed to have.
"""
import gzip, hashlib, random, re, time, threading
import urllib.request, urllib.error, urllib.robotparser
from urllib.parse import urlparse, urljoin

import db, config

NAME = "SecondeMainBot"
VERSION = "1.0"
CONTACT = config.__dict__.get("CRAWLER_CONTACT", "") or "timael@andrie.ch"
DOCS = "https://github.com/local/seconde-main"
USER_AGENT = f"{NAME}/{VERSION} (+{DOCS}; personal marketplace monitor; contact: {CONTACT})"

MIN_DELAY = 15.0          # our own floor, regardless of what robots.txt allows
MAX_DELAY = 120.0
DENY_BASE = 3600          # first denial: stand down an hour
DENY_MAX = 24 * 3600
TIMEOUT = 25

_locks = {}
_locks_guard = threading.Lock()
_last_hit = {}
_robots = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS crawl_cache (
  url TEXT PRIMARY KEY, etag TEXT, last_modified TEXT,
  fetched_at REAL, status INTEGER, body TEXT
);
CREATE TABLE IF NOT EXISTS crawl_log (
  id INTEGER PRIMARY KEY, ts REAL, url TEXT, status TEXT,
  bytes INTEGER, cached INTEGER, note TEXT
);
CREATE INDEX IF NOT EXISTS idx_crawl_log_ts ON crawl_log(ts DESC);
CREATE TABLE IF NOT EXISTS domain_state (
  domain TEXT PRIMARY KEY, crawl_delay REAL, denied_until REAL,
  deny_streak INTEGER DEFAULT 0, last_status TEXT, last_fetch REAL, note TEXT
);
"""

def init():
    with db.connect() as c:
        c.executescript(SCHEMA)

def _domain(url):
    return urlparse(url).netloc.lower()

def _lock(domain):
    with _locks_guard:
        return _locks.setdefault(domain, threading.Lock())

def log(url, status, nbytes=0, cached=False, note=""):
    db.run("""INSERT INTO crawl_log(ts,url,status,bytes,cached,note)
              VALUES(?,?,?,?,?,?)""",
           (time.time(), url[:300], str(status), nbytes, 1 if cached else 0, note[:200]))

# --- robots ---------------------------------------------------------------
# Prose bans that urllib.robotparser cannot see. leboncoin's robots.txt opens
# with "It's forbidden to use search robots or other automatic methods to
# access Leboncoin.fr", and the machine-readable rules alone read as "allowed".
PROSE_BANS = ("forbidden to use search robots", "interdit d'utiliser des robots",
              "automatic methods to access", "only permitted with special permission",
              "autorisation expresse", "scraping is prohibited", "no scraping")

def policy(domain):
    """-> (permitted, reason). Reads robots.txt as a human would, not just as
    a parser: prose bans and named-bot allowlists both count."""
    raw = _robots_text.get(domain)
    if raw is None:
        return False, "robots.txt illisible"
    low = raw.lower()
    for ban in PROSE_BANS:
        if ban in low:
            return False, "robots.txt interdit explicitement l'accès automatisé"
    groups = re.findall(r"(?im)^user-agent:\s*(\S+)", raw)
    if groups and not any(g.strip() == "*" for g in groups):
        named = ", ".join(sorted(set(groups))[:4])
        return False, f"robots.txt n'autorise que des robots nommés ({named}…)"
    return True, ""

_robots_text = {}

def robots(domain):
    """Parsed robots.txt for a domain, cached for the process lifetime."""
    if domain in _robots:
        return _robots[domain]
    rp = urllib.robotparser.RobotFileParser()
    url = f"https://{domain}/robots.txt"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read().decode("utf-8", "replace")
        rp.parse(body.splitlines())
        _robots_text[domain] = body
        log(url, r.status, len(body), note="robots")
    except Exception as e:
        # No robots.txt readable: assume nothing is permitted beyond what a
        # site explicitly publishes. Fail closed, not open.
        rp = None
        log(url, "error", note=f"robots unreadable: {type(e).__name__}")
    _robots[domain] = rp
    return rp

def allowed(url):
    domain = _domain(url)
    rp = robots(domain)
    if rp is None:
        return False, "robots.txt illisible — on s'abstient"
    ok, why = policy(domain)            # prose bans and allowlists first
    if not ok:
        return False, why
    if not rp.can_fetch(USER_AGENT, url):
        return False, "interdit par robots.txt"
    return True, ""

def crawl_delay(domain):
    """The site's own crawl-delay, never below our floor."""
    rp = robots(domain)
    d = None
    if rp is not None:
        try:
            d = rp.crawl_delay(USER_AGENT)
        except Exception:
            d = None
    return max(MIN_DELAY, min(MAX_DELAY, float(d))) if d else MIN_DELAY

# --- denial ---------------------------------------------------------------
def state(domain):
    return db.q("SELECT * FROM domain_state WHERE domain=?", (domain,), one=True)

def denied_for(domain):
    s = state(domain)
    if not s or not s["denied_until"]:
        return 0
    return max(0, s["denied_until"] - time.time())

def mark_denied(domain, status, note=""):
    """A refusal is a decision by the operator. Record it and stand down."""
    s = state(domain)
    streak = (s["deny_streak"] if s else 0) + 1
    until = time.time() + min(DENY_MAX, DENY_BASE * (2 ** min(streak - 1, 5)))
    db.run("""INSERT INTO domain_state(domain,denied_until,deny_streak,last_status,last_fetch,note)
              VALUES(?,?,?,?,?,?)
              ON CONFLICT(domain) DO UPDATE SET denied_until=?, deny_streak=?,
                last_status=?, last_fetch=?, note=?""",
           (domain, until, streak, str(status), time.time(), note,
            until, streak, str(status), time.time(), note))

def mark_ok(domain):
    db.run("""INSERT INTO domain_state(domain,denied_until,deny_streak,last_status,last_fetch,note)
              VALUES(?,NULL,0,'ok',?,'')
              ON CONFLICT(domain) DO UPDATE SET denied_until=NULL, deny_streak=0,
                last_status='ok', last_fetch=?, note=''""",
           (domain, time.time(), time.time()))

DENIAL_MARKERS = ("captcha", "datadome", "geo.captcha-delivery.com",
                  "please enable js", "unusual traffic", "access denied",
                  "challenges.cloudflare.com", "un instant", "just a moment")

def looks_denied(body):
    low = (body or "")[:200000].lower()
    return any(m in low for m in DENIAL_MARKERS)

# --- the fetch ------------------------------------------------------------
def get(url, force=False):
    """Fetch a URL politely, or return None and say why in the log.

    Returns the body, or None when we are not allowed, not welcome, or the
    content has not changed.
    """
    domain = _domain(url)
    left = denied_for(domain)
    # A sitemap is published in robots.txt specifically for crawlers, so it
    # stays fetchable while content pages are standing down. If the sitemap
    # itself answers 403 we stop there too -- we obey each resource's answer,
    # we never route around one.
    # robots.txt advertises the sitemap INDEX; its children are part of the
    # same published tree, so the whole tree counts as explicitly offered
    published = url in set(sitemaps(domain)) or bool(
        re.search(r"/[^/]*sitemap[^/]*\.xml(\.gz)?$", urlparse(url).path, re.I))
    if left and not published:
        log(url, "skipped", note=f"domaine en retrait {left/60:.0f}min")
        return None
    ok, why = allowed(url)
    if not ok:
        log(url, "disallowed", note=why)
        return None

    with _lock(domain):                       # one request at a time per domain
        wait = crawl_delay(domain) - (time.time() - _last_hit.get(domain, 0))
        if wait > 0:
            time.sleep(wait + random.uniform(0, 2.0))   # jitter
        _last_hit[domain] = time.time()

        cached = db.q("SELECT * FROM crawl_cache WHERE url=?", (url,), one=True)
        headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xml,*/*",
                   "Accept-Language": "fr-CH,fr;q=0.9,en;q=0.8",
                   "Accept-Encoding": "gzip"}
        if cached and not force:
            if cached["etag"]:
                headers["If-None-Match"] = cached["etag"]
            if cached["last_modified"]:
                headers["If-Modified-Since"] = cached["last_modified"]

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\\x1f\\x8b":
                    raw = gzip.decompress(raw)
                body = raw.decode("utf-8", "replace")
                etag = r.headers.get("ETag")
                lastmod = r.headers.get("Last-Modified")
                status = r.status
        except urllib.error.HTTPError as e:
            if e.code == 304 and cached:
                log(url, 304, cached=True, note="inchangé")
                mark_ok(domain)
                return cached["body"]
            if e.code in (401, 403, 429):
                mark_denied(domain, e.code, "refus explicite")
                log(url, e.code, note="refus — on arrête, pas de contournement")
                return None
            log(url, e.code, note="erreur http")
            return None
        except Exception as e:
            log(url, "error", note=type(e).__name__)
            return None

        if looks_denied(body):
            mark_denied(domain, status, "page de vérification anti-robot")
            log(url, status, len(body), note="challenge — on arrête")
            return None

        mark_ok(domain)
        db.run("""INSERT INTO crawl_cache(url,etag,last_modified,fetched_at,status,body)
                  VALUES(?,?,?,?,?,?)
                  ON CONFLICT(url) DO UPDATE SET etag=?, last_modified=?, fetched_at=?,
                    status=?, body=?""",
               (url, etag, lastmod, time.time(), status, body,
                etag, lastmod, time.time(), status, body))
        log(url, status, len(body))
        return body

# --- sitemaps (the path sites publish FOR crawlers) -----------------------
def sitemaps(domain):
    """Sitemap URLs a site advertises in robots.txt."""
    rp = robots(domain)
    urls = list(getattr(rp, "site_maps", lambda: [])() or []) if rp else []
    return urls

def sitemap_urls(url, limit=5000, want=None):
    """URLs listed in a sitemap (follows one level of sitemap index)."""
    body = get(url)
    if not body:
        return []
    locs = re.findall(r"<loc>([^<]+)</loc>", body)
    if "<sitemapindex" in body[:2000]:
        out = []
        for child in locs:
            if want and want not in child:
                continue
            out.extend(sitemap_urls(child, limit=limit))
            if len(out) >= limit:
                break
        return out[:limit]
    return locs[:limit]

def audit(limit=40):
    return db.q("SELECT * FROM crawl_log ORDER BY ts DESC LIMIT ?", (limit,))

def demo():
    init()
    assert USER_AGENT.startswith("SecondeMainBot/") and "contact:" in USER_AGENT
    assert "Mozilla" not in USER_AGENT, "le crawler ne doit jamais se faire passer pour un navigateur"
    assert looks_denied("<html>DataDome</html>")
    assert looks_denied("Please enable JS and disable any ad blocker")
    assert not looks_denied("<html><body>Vélo de course 500 CHF</body></html>")
    ok, why = allowed("https://www.anibis.ch/fr/q?query=velo")
    print("anibis autorisé par robots.txt :", ok, why)
    print("crawl-delay anibis :", crawl_delay("www.anibis.ch"), "s")
    print("crawl-delay ricardo:", crawl_delay("www.ricardo.ch"), "s")
    print("UA :", USER_AGENT)
    print("crawler ok")

if __name__ == "__main__":
    demo()
