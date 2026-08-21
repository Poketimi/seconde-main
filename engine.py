"""The background loop: scrape -> store -> filter -> match -> notify.

Filter order is deliberate and is the whole cost story: rules and distance are
free and reject most listings, so the AI batch only ever sees survivors.
"""
import json, re, time, threading, subprocess, shutil, traceback
from concurrent.futures import ThreadPoolExecutor
import db, ai, geo, sources, config, i18n

# --- strict matching against a named model -------------------------------
_NOISE = {"occasion", "ski", "skis", "de", "la", "le", "les", "des", "du", "cm",
          "taille", "paire", "et", "avec", "pour", "neuf", "vendre"}

def _norm(t):
    t = (t or "").lower().translate(_ACCENTS)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", t)).strip()

def target_tokens(name):
    """The tokens that actually identify a model: brand, model, and its number.

    A number is never optional -- Kore 93 and Kore 99 are different skis, and
    treating "99" as noise is how a keyword search returns the wrong ski.
    """
    out = []
    for w in _norm(name).split():
        if w in _NOISE and not w.isdigit():
            continue
        if len(w) >= 2 or w.isdigit():
            out.append(w)
    return out

CURRENCY = ("chf", "eur", "euro", "euros", "fr", "frs", "sfr")

def _number_belongs_to(word, num, hay, tight):
    """Is `num` this model's number, or just a price sitting next to the name?

    "Nordica Enforcer, 100 CHF" is a price; "Nordica Enforcer 100 - 186cm" is
    the model. The currency word right after is what separates them.
    """
    # `tight` keeps punctuation and only drops spaces, so "Enforcer, 100 CHF"
    # stays "enforcer,100" and is not mistaken for the model "Enforcer100"
    if f"{word}{num}" in tight:            # "QST99": never a price
        return True
    for m in re.finditer(rf"\b{re.escape(word)}\s+{re.escape(num)}\b", hay):
        after = hay[m.end():].strip().split(" ")
        if not after or after[0] not in CURRENCY:
            return True                    # at least one honest occurrence
    return False

def matches_target(title, target_name, description=None):
    """True only if EVERY identifying token of the model appears.

    Deliberately strict: with concrete models to look for, a partial match is
    almost always the wrong product rather than a near miss.
    """
    toks = target_tokens(target_name)
    if not toks:
        return False
    raw = f"{title} {description or ''}".lower().translate(_ACCENTS)
    hay = _norm(raw)
    words = set(hay.split())
    tight = re.sub(r"\s+", "", raw)    # sellers write "QST99" as often as "QST 99"
    for i, t in enumerate(toks):
        if not t.isdigit():
            if t not in hay:
                return False
            continue
        # A model number must sit next to its model word. Accepting it anywhere
        # made "Head Kore 93, 99 euros" match Kore 99 -- the 99 was the price.
        prev = toks[i - 1] if i else ""
        if prev and not prev.isdigit():
            if not _number_belongs_to(prev, t, hay, tight):
                return False
            continue
        if t not in words:      # leading number with nothing to anchor to
            return False
    return True

# --- duplicate detection ------------------------------------------------
# anibis and tutti run the same platform, so one seller's ad appears on both
# under different ids and URLs; sellers also relist the same item. Checked
# against real data: genuine cross-posts share title+price+postcode (a JBL at
# 99 CHF in 1004 Lausanne on both sites), while four different "Enceinte
# Bluetooth" at 30 EUR sit in Poitiers, Dunkerque, Orée d'Anjou... -- so the
# postcode is what keeps generic titles apart.
_ACCENTS = str.maketrans("àâäáãçèéêëìíîïñòóôöõùúûüýÿ", "aaaaaceeeeiiiinooooouuuuyy")

def dup_key(d):
    t = (d.get("title") or "").lower().translate(_ACCENTS)
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    t = re.sub(r"\s+", " ", t)
    if not t:
        return None
    price = d.get("price")
    price = f"{float(price):.0f}" if price not in (None, "") else "?"
    where = (d.get("postal_code") or (d.get("location_raw") or "")[:12] or "?").strip().lower()
    return f"{t}|{price}|{where}"

# --- storage -----------------------------------------------------------
COLS = ("url source source_id title description price currency price_type category "
        "condition seller_type seller_name location_raw postal_code country lat lon "
        "shipping shipping_cost image images posted_at attrs auction_end bids dup_key raw").split()

def upsert_listing(d):
    """Insert or refresh. Returns (listing_id, is_new)."""
    now = time.time()
    row = db.q("SELECT id, price FROM listings WHERE url=?", (d["url"],), one=True)
    if row:
        # a price change is worth keeping: sellers drop prices over time
        if d.get("price") is not None and row["price"] != d["price"]:
            db.run("INSERT OR IGNORE INTO listing_prices(listing_id,ts,price) VALUES(?,?,?)",
                   (row["id"], now, d["price"]))
        # auctions move: refresh the live bid data every time we see them
        db.run("""UPDATE listings SET last_seen=?, active=1, status='active', gone_at=NULL,
                  price=COALESCE(?,price), bids=COALESCE(?,bids),
                  auction_end=COALESCE(?,auction_end) WHERE id=?""",
               (now, d.get("price"), d.get("bids"), d.get("auction_end"), row["id"]))
        return row["id"], False
    # one guard here beats a guard in every adapter: sqlite binds scalars only
    d = {**d, "dup_key": dup_key(d)}
    vals = [json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
            for v in (d.get(c) for c in COLS)]
    lid = db.run(f"INSERT INTO listings({','.join(COLS)},first_seen,last_seen,status) "
                 f"VALUES({','.join('?' * len(COLS))},?,?,'active')", (*vals, now, now))
    if d.get("price") is not None:
        db.run("INSERT OR IGNORE INTO listing_prices(listing_id,ts,price) VALUES(?,?,?)",
               (lid, now, d["price"]))
    return lid, True

def link_seller(lid, d):
    """Create/refresh the seller record from what the scan already returned.

    986 listings carried a seller name that was going nowhere: the record was
    only built when someone opened a facebook listing. This costs no request.
    """
    import sellers
    key = d.get("seller_key") or d.get("seller_name")
    if not key:
        return None
    sid = sellers.upsert(d["source"], key, name=d.get("seller_name"))
    if sid:
        db.run("UPDATE listings SET seller_ref=? WHERE id=?", (sid, lid))
    return sid

def ensure_coords(lid, d):
    """Offline postcode lookup for sources that don't ship coordinates."""
    if d.get("lat") is not None and d.get("lon") is not None:
        return (d["lat"], d["lon"])
    hit = geo.locate_listing(d.get("location_raw"), d.get("postal_code"), d.get("country"))
    if hit:
        db.run("UPDATE listings SET lat=?, lon=? WHERE id=?", (hit[0], hit[1], lid))
    return hit

# --- rules (free) ------------------------------------------------------
COND_RANK = {"parts": 0, "fair": 1, "good": 2, "like_new": 3, "new": 4, "unknown": 2}

def passes_rules(d, s):
    p = d.get("price")
    if s["price_min"] is not None and p is not None and p < s["price_min"]:
        return False
    if s["price_max"] is not None and p is not None and p > s["price_max"]:
        return False
    if s["seller_type"] and s["seller_type"] != "any" and d.get("seller_type") != s["seller_type"]:
        return False
    blob = f"{d.get('title') or ''} {d.get('description') or ''}".lower()
    for kw in (s["exclude_kw"] or "").split(","):
        if kw.strip() and kw.strip().lower() in blob:
            return False
    return True

# Words that, IN THE TITLE, mean the ad is not the item itself.
# Matching these against the description backfires badly: a genuine phone ad
# says "batterie 89%, étui inclus" and would be thrown away, while a repair
# service with a clean description would sail through.
NOT_THE_ITEM = ("coque", "housse", "étui", "chargeur", "câble", "cable", "vitre",
                "verre trempé", "protection", "réparation", "reparation", "repair",
                "service", "pièces", "pieces", "hülle", "zubehör", "ersatzteil",
                "recherche", "cherche", "achète", "suche")

def keyword_score(d, s):
    """Fallback relevance when no AI key is configured.

    ponytail: title keywords only -- crude on purpose. Set DEEPSEEK_API_KEY and
    ai.analyse() replaces this with something that actually reads the ad.
    """
    title = (d.get("title") or "").lower()
    blob = f"{title} {(d.get('description') or '').lower()}"
    terms = [t for t in (s["query"] or "").lower().split() if len(t) >= 2]
    if not terms:
        return 50
    score = 100 * sum(1 for t in terms if t in blob) / len(terms)
    if any(w in title for w in NOT_THE_ITEM):
        score *= 0.2
    return score

# --- distance ----------------------------------------------------------
def distance_ok(d, s, coords):
    """-> (ok, minutes, origin_label, mode). Shipped items skip distance."""
    origins = json.loads(s["origins"] or "[]")
    if not origins:
        return True, None, None, None
    if s["shipping_ok"] and d.get("shipping"):
        return True, None, "livraison", None
    if not coords:
        return False, None, None, None
    return geo.best_origin(coords, origins)

# --- notify ------------------------------------------------------------
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

# Where to send a source to refresh its session. Revisiting the home page with
# the stored profile fixes an EXPIRED SESSION (facebook recovers this way).
# It does NOT clear a Cloudflare challenge: tested headless and in a visible
# window, autoscout24 stayed blocked both times, because Turnstile wants a
# human click. Those still need you to press "Se connecter".
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

# What each source did across the whole cycle. A source is only "empty" if it
# produced nothing for EVERY search, not just for one narrow query.
CYCLE_TALLY = {}

def commit_cycle_health():
    for src, t in CYCLE_TALLY.items():
        record_health(src, t["status"], t["detail"])
    CYCLE_TALLY.clear()

def sweep_lifecycle():
    """Mark what has disappeared, keep everything.

    Two different signals, and they must not be confused:
      ended  an auction whose end date has passed -- a fact
      gone   not seen in a while -- could be sold, could be withdrawn, and we
             genuinely do not know which
    """
    now = time.time()
    ended = db.run_count("""UPDATE listings SET status='ended', active=0,
                        gone_at=COALESCE(gone_at, auction_end)
                      WHERE auction_end IS NOT NULL AND auction_end < ?
                        AND COALESCE(status,'active') = 'active'""", (now,))
    cutoff = now - config.GONE_AFTER_HOURS * 3600
    gone = db.run_count("""UPDATE listings SET status='gone', active=0, gone_at=?
                     WHERE last_seen < ? AND COALESCE(status,'active') = 'active'""",
                  (now, cutoff))
    # a listing seen again is back: sellers relist, auctions get extended
    back = db.run_count("""UPDATE listings SET status='active', active=1, gone_at=NULL
                     WHERE last_seen >= ? AND status = 'gone'""", (cutoff,))
    return ended, gone, back

# --- source health ---------------------------------------------------
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

# --- one search --------------------------------------------------------
def run_search(s, ai_budget=None):
    s = dict(s)
    with _active_lock:
        _active.add(s["name"])
    try:
        return _run_search(s, ai_budget)
    finally:
        with _active_lock:
            _active.discard(s["name"])

def _run_search(s, ai_budget=None):
    picked = json.loads(s["sources"] or "[]") or list(sources.ADAPTERS)
    total_new, total_matched = 0, 0

    # A search is now a list of concrete models ("Salomon QST 99", "Head Kore
    # 99"...). Each is queried by name, which beats one fuzzy query by a mile.
    # Without targets we fall back to the old single-query behaviour.
    targets = db.q("SELECT * FROM targets WHERE search_id=? AND active=1 ORDER BY id",
                   (s["id"],))
    if targets:
        jobs = [(t["id"], t["name"], t["query"]) for t in targets]
    else:
        jobs = [(None, None, (s["reference"] or "").strip() or s["query"])]

    # Fetch every (model x source) pair at once: network-bound and independent.
    # Browser-backed sources still queue behind browser._BROWSER_LOCK.
    # Rotate which models go to the heavy browser sources, so a long model list
    # does not turn into hundreds of automated page views per hour.
    # Skip whatever is currently backing off, and rotate models so a long list
    # does not multiply into a burst of requests.
    ready = []
    for src in picked:
        left = in_backoff(src)
        if left:
            print(f"  [{src}] en pause encore {left/60:.0f}min (refus répétés)")
            continue
        ready.append(src)

    def rotation(k):
        k = max(1, min(k, len(jobs)))
        off = int(time.time() // max(60, config.POLL_SECONDS)) % len(jobs)
        return [jobs[(off + i) % len(jobs)] for i in range(k)]

    pairs = []
    for src in ready:
        k = (config.BROWSER_TARGETS_PER_CYCLE if src in sources.NEEDS_BROWSER
             else config.TARGETS_PER_CYCLE)
        pairs += [(tid, tname, q, src) for (tid, tname, q) in rotation(k)]
    fetched = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(pairs)))) as pool:
        futs = {(tid, src): pool.submit(sources.search, src, q, s)
                for (tid, tname, q, src) in pairs}
        for key, fut in futs.items():
            try:
                fetched[key] = fut.result()
            except Exception:
                traceback.print_exc(); fetched[key] = []

    # Collect candidates across every source first, then run ONE AI pass.
    # Calling the model per source meant 5 small requests instead of a few full
    # batches, so nothing could be parallelised and the scan crawled.
    all_candidates = []
    per_source_found = {src: 0 for src in picked}
    for (tid, tname, q, src) in pairs:
        rows = fetched.get((tid, src), [])
        st_now = sources.LAST_STATUS.get(src, ("", ""))[0]
        # "empty" is usually stale rather than broken: tutti, anibis and
        # leboncoin were all recorded empty while returning 30+ listings.
        if not rows and st_now in ("login", "blocked", "empty"):
            rows = try_recover(src, q, s)
        per_source_found[src] += len(rows)

        for d in rows:
            if not d.get("url") or not d.get("title"):
                continue
            # Searching a model by name means the site returns near misses too
            # (Kore 93 when we asked for Kore 99). With an exact model in hand a
            # partial match is the wrong product, so require every token.
            if tname and not matches_target(d["title"], tname, d.get("description")):
                continue
            lid, is_new = upsert_listing(d)
            total_new += is_new
            link_seller(lid, d)
            coords = ensure_coords(lid, d)
            if db.q("SELECT 1 FROM matches WHERE search_id=? AND listing_id=?",
                    (s["id"], lid), one=True):
                continue
            if not passes_rules(d, s):
                continue
            ok, mins, label, mode = distance_ok(d, s, coords)
            if not ok:
                continue
            all_candidates.append((lid, d, mins, label, mode, tid, tname))

    for src in ready:
        status, detail = sources.LAST_STATUS.get(src, ("empty", ""))
        found = per_source_found.get(src, 0)
        if found:
            status, detail = "ok", f"{found} annonces"
        # remembered for the cycle; the verdict is written once in run_all
        CYCLE_TALLY.setdefault(src, {"found": 0, "status": status, "detail": detail})
        CYCLE_TALLY[src]["found"] += found
        if found:
            CYCLE_TALLY[src].update(status="ok", detail=f"{found} annonces")
        elif CYCLE_TALLY[src]["status"] != "ok":
            CYCLE_TALLY[src].update(status=status, detail=detail)
        db.run("INSERT INTO runlog(ts,source,search_id,found,new,ok,note) VALUES(?,?,?,?,?,?,?)",
               (time.time(), src, s["id"], found, 0,
                1 if status == "ok" else 0, detail if status != "ok" else ""))

    # Two phases, because the AI is latency-bound and its cost swings 2-3x
    # between runs. Phase 1 writes every candidate immediately with a keyword
    # score, so the results page has content within seconds instead of minutes.
    seen_keys = {r["dup_key"] for r in db.q(
        """SELECT DISTINCT l.dup_key FROM matches m JOIN listings l ON l.id=m.listing_id
           WHERE m.search_id=? AND l.dup_key IS NOT NULL""", (s["id"],))}
    for lid, d, mins, label, mode, tid, tname in all_candidates:
        k = dup_key(d)
        if k and k in seen_keys:
            continue                     # same item, another site or a relist
        if k:
            seen_keys.add(k)
        db.run("INSERT OR IGNORE INTO matches(search_id,listing_id,target_id,score,reason,"
               "travel_minutes,travel_origin,travel_mode,created_at)"
               " VALUES(?,?,?,?,?,?,?,?,?)",
               (s["id"], lid, tid,
                100.0 if tname else keyword_score(d, s),
                f"modèle {tname}" if tname else PENDING,
                mins, label, mode, time.time()))
        total_matched += 1

    # Phase 2 refines them: real relevance, product fiches, and dropping the
    # accessories keyword matching let through.
    req = (f"{s['query']} | ref={s['reference'] or '-'} | "
           f"budget={s['price_min']}-{s['price_max']}")
    total_matched += judge(s, all_candidates, req, ai_budget)

    db.run("UPDATE searches SET last_run=? WHERE id=?", (time.time(), s["id"]))

    fresh = db.q("SELECT m.id, l.title, l.price, l.currency, m.travel_minutes, m.travel_origin"
                 " FROM matches m JOIN listings l ON l.id=m.listing_id"
                 " WHERE m.search_id=? AND m.notified=0 ORDER BY m.score DESC", (s["id"],))
    if fresh:
        top = fresh[0]
        where = f" · {top['travel_minutes']:.0f}min de {top['travel_origin']}" \
                if top["travel_minutes"] is not None else ""
        notify(f"{len(fresh)} nouveau(x) · {s['name']}",
               f"{top['title'][:60]} — {top['price']} {top['currency']}{where}",
               url=f"http://localhost:5055/search/{s['id']}?sort=new")
        db.run("UPDATE matches SET notified=1 WHERE search_id=? AND notified=0", (s["id"],))
    return total_new, total_matched

PENDING = "analyse en cours…"

def judge(s, candidates, req=None, ai_budget=None):
    """Phase 2 : le vrai verdict sur des matchs provisoires. Delta de matchs.

    Extraite du scan parce qu'un cycle interrompu laissait ses lignes en
    « analyse en cours… » pour toujours : 139 des 271 matchs de la base
    étaient dans cet état, dont un foil Armstrong dans une recherche de sac à
    dos. `finish_pending` rappelle donc exactement ce code plus tard, au lieu
    d'une seconde implémentation qui dériverait.
    """
    if not candidates:
        return 0
    req = req or (f"{s['query']} | ref={s['reference'] or '-'} | "
                  f"budget={s['price_min']}-{s['price_max']}")
    verdicts = ai.analyse([c[1] for c in candidates], req, budget=ai_budget)
    delta_count = 0
    for k, (lid, d, mins, label, mode, tid, tname) in enumerate(candidates):
        v = verdicts.get(k)
        if not v:
            if tname:
                db.run("UPDATE matches SET reason=? WHERE search_id=? AND listing_id=?",
                       (f"modèle {tname}", s["id"], lid))
            elif keyword_score(d, s) < 40:    # no AI: apply the keyword cut-off
                db.run("DELETE FROM matches WHERE search_id=? AND listing_id=?", (s["id"], lid))
                delta_count -= 1
            else:
                db.run("UPDATE matches SET reason=? WHERE search_id=? AND listing_id=?",
                       ("match par mots-clés", s["id"], lid))
            continue

        pid = ai.upsert_product(v.get("product"), d.get("category")) if v.get("is_item", True) else None
        db.run("UPDATE listings SET product_id=?, condition=?, attrs=?, ai_enriched=1 WHERE id=?",
               (pid, v.get("condition"),
                json.dumps(v.get("attrs") or {}, ensure_ascii=False), lid))
        if pid:
            db.recompute_product_stats(pid)

        score = float(v.get("score") or 0)
        if tname and v.get("is_item", True):
            score = max(score, 80.0)      # the exact model was matched by name
        if not v.get("is_item", True) or score < 40:
            db.run("DELETE FROM matches WHERE search_id=? AND listing_id=?", (s["id"], lid))
            delta_count -= 1
            continue

        deal = None
        if pid:
            pr = db.q("SELECT price_median FROM products WHERE id=?", (pid,), one=True)
            if pr and pr["price_median"] and d.get("price"):
                deal = (d["price"] - pr["price_median"]) / pr["price_median"] * 100
        db.run("""UPDATE matches SET score=?, reason=?, deal_delta=?
                  WHERE search_id=? AND listing_id=?""",
               (score, v.get("reason") or "", deal, s["id"], lid))
    return delta_count

def finish_pending(older_than=600, limit=200):
    """Reprend les matchs laissés provisoires par un cycle interrompu.

    Redémarrer le serveur pendant un scan tuait la phase 2 : les lignes de la
    phase 1 gardaient leur score mots-clés et la mention « analyse en cours… »
    indéfiniment, affichée à l'utilisateur comme si un verdict était en route.
    Ne juge que ce qui a eu le temps d'être abandonné, jamais un scan en cours.
    """
    rows = db.q("""SELECT m.search_id, m.listing_id, m.target_id, m.travel_minutes,
                          m.travel_origin, m.travel_mode, t.name tname
                   FROM matches m LEFT JOIN targets t ON t.id = m.target_id
                   WHERE m.reason = ? AND m.created_at < ?
                   ORDER BY m.created_at LIMIT ?""",
                (PENDING, time.time() - older_than, limit))
    if not rows:
        return 0, 0
    by_search = {}
    for r in rows:
        by_search.setdefault(r["search_id"], []).append(r)
    seen = dropped = 0
    for sid, group in by_search.items():
        s = db.q("SELECT * FROM searches WHERE id=?", (sid,), one=True)
        if not s:
            continue
        cands = []
        for r in group:
            l = db.q("SELECT * FROM listings WHERE id=?", (r["listing_id"],), one=True)
            if l:
                cands.append((r["listing_id"], dict(l), r["travel_minutes"],
                              r["travel_origin"], r["travel_mode"],
                              r["target_id"], r["tname"]))
        seen += len(cands)
        dropped -= judge(s, cands)
    return seen, dropped

def enrich_backlog(limit=None):
    """Build product entries for every listing seen, matched or not.

    Match-time enrichment only ever sees listings that survived the distance
    filter, so most of what gets scraped would never reach the AI. This pass
    catalogues the rest, oldest first, under a budget.
    """
    if not ai.available():
        return 0
    limit = limit or config.AI_ENRICH_BUDGET
    rows = db.q("SELECT * FROM listings WHERE ai_enriched=0 ORDER BY first_seen LIMIT ?",
                (limit,))
    if not rows:
        return 0
    items = [dict(r) for r in rows]
    verdicts = ai.analyse(items)          # no search context: classify + canonicalise
    done = 0
    for i, d in enumerate(items):
        v = verdicts.get(i)
        if not v:
            continue
        pid = ai.upsert_product(v.get("product"), d.get("category")) \
            if v.get("is_item", True) else None
        db.run("UPDATE listings SET product_id=?, condition=?, attrs=?, ai_enriched=1"
               " WHERE id=?",
               (pid, v.get("condition"),
                json.dumps(v.get("attrs") or {}, ensure_ascii=False), d["id"]))
        if pid:
            db.recompute_product_stats(pid)
        done += 1
    return done

def run_all():
    out = []
    for s in db.q("SELECT * FROM searches WHERE active=1"):
        try:
            n, m = run_search(s)
            out.append((s["name"], n, m))
            print(f"  [{s['name']}] {n} nouvelles annonces, {m} matchs")
        except Exception:
            traceback.print_exc()
    try:
        commit_cycle_health()
    except Exception:
        traceback.print_exc()
    try:
        e_, g_, b_ = sweep_lifecycle()
        if e_ or g_ or b_:
            print(f"  [cycle] {e_} enchères terminées, {g_} annonces disparues, {b_} revenues")
    except Exception:
        traceback.print_exc()
    try:
        n = enrich_backlog()
        if n:
            print(f"  [ia] {n} annonces cataloguées")
    except Exception:
        traceback.print_exc()
    try:
        seen, wrote = i18n.backlog()
        if wrote:
            print(f"  [ia] {seen} annonces traduites ({wrote} versions)")
    except Exception:
        traceback.print_exc()
    try:
        n, dropped = finish_pending()
        if n:
            print(f"  [ia] {n} matchs restés provisoires jugés ({dropped} écartés)")
    except Exception:
        traceback.print_exc()
    return out

# --- loop --------------------------------------------------------------
_started = False

def start_loop():
    global _started
    if _started:
        return
    _started = True

    def loop():
        while True:
            try:
                if db.q("SELECT 1 FROM searches WHERE active=1", one=True):
                    print(f"[{time.strftime('%H:%M:%S')}] cycle")
                    run_all()
            except Exception:
                traceback.print_exc()
            time.sleep(config.POLL_SECONDS)

    threading.Thread(target=loop, daemon=True).start()

def demo():
    db.init()
    s = {"id": -1, "query": "iphone 13 pro", "reference": None, "price_min": 100,
         "price_max": 500, "seller_type": "any", "exclude_kw": "cassé,broken",
         "origins": "[]", "shipping_ok": 1, "sources": "[]", "name": "t"}
    assert passes_rules({"price": 300, "seller_type": "private", "title": "iPhone 13 Pro"}, s)
    assert not passes_rules({"price": 900, "title": "x"}, s), "price ceiling ignored"
    assert not passes_rules({"price": 300, "title": "iPhone cassé"}, s), "exclude kw ignored"
    s2 = dict(s, seller_type="private")
    assert not passes_rules({"price": 300, "seller_type": "pro", "title": "x"}, s2)

    assert keyword_score({"title": "iPhone 13 Pro 256Go"}, s) > 90
    assert keyword_score({"title": "Coque iPhone 13 Pro"}, s) < 40, "accessory not penalised"

    lausanne = geo.by_postcode("1015", "CH")
    orig = [{"label": "EPFL", "lat": lausanne[0], "lon": lausanne[1],
             "max_minutes": 10, "mode": "foot"}]
    s3 = dict(s, origins=json.dumps(orig), shipping_ok=1)
    ok, *_ = distance_ok({"shipping": 0}, s3, geo.by_postcode("1422", "CH"))
    assert not ok, "Grandson must fail a 10min walk from EPFL"
    ok, _, label, _ = distance_ok({"shipping": 1}, s3, None)
    assert ok and label == "livraison", "shipped items must bypass distance"
    print("engine ok")

if __name__ == "__main__":
    demo()
