"""La boucle : scanner, filtrer, juger, cataloguer, traduire, notifier."""
import json, re, time, threading, subprocess, shutil, traceback
from concurrent.futures import ThreadPoolExecutor
import db, ai, geo, sources, config, i18n, browser
from .match import (matches_target, keyword_score, passes_rules, dup_key,
                    distance_ok, target_tokens)
from .store import COLS, upsert_listing, link_seller, ensure_coords, sweep_lifecycle
from .health import (notify, is_scanning, try_recover, CYCLE_TALLY, health,
                     commit_cycle_health, record_health, in_backoff, _active,
                     _active_lock, forget_removed_sources, STATUS_LABEL)

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
                # `tname` dit d'où vient l'annonce, pas ce qu'elle est : le
                # site renvoie ce qu'il veut pour « Burton Custom ». Sans le
                # modèle dans le titre, on retombe sur le score mots-clés.
                100.0 if (tname and matches_target(d.get("title"), tname))
                else keyword_score(d, s),
                f"modèle {tname}" if (tname and matches_target(d.get("title"), tname))
                else PENDING,
                mins, label, mode, time.time()))
        total_matched += 1

    # Phase 2 refines them: real relevance, product fiches, and dropping the
    # accessories keyword matching let through.
    req = (f"{s['query']} | ref={s['reference'] or '-'} | "
           f"budget={s['price_min']}-{s['price_max']}")
    total_matched += judge(s, all_candidates, req, ai_budget)

    db.run("UPDATE searches SET last_run=? WHERE id=?", (time.time(), s["id"]))

    # Une fois le lot jugé et stable, désigner ce qui sort du lot. Ne coûte un
    # appel que si les annonces ont bougé depuis la dernière fois.
    try:
        fresh = db.q("SELECT * FROM searches WHERE id=?", (s["id"],), one=True)
        n = ai.recommend(fresh)
        if n:
            print(f"  [{s['name']}] {n} recommandation(s)")
    except Exception:
        traceback.print_exc()

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
            if tname and matches_target(d.get("title"), tname):
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
        # Le plancher ne vaut que si le TITRE correspond vraiment au modèle.
        # Il s'appliquait dès que l'annonce venait d'une recherche lancée pour
        # ce modèle — or un site renvoie ce qu'il veut : chercher « Burton
        # Custom » ramenait une guitare Ibanez et un livre sur Fender, que l'IA
        # rejetait explicitement (« Guitare, pas un snowboard ») avant que le
        # max() ne les repêche à 80. 91 des 247 matchs ciblés étaient dans ce
        # cas.
        # TITRE SEUL, jamais la description : la boutique qui liste tout son
        # stock (« aussi en stock : … ») faisait correspondre n'importe quel
        # modèle. Même piège que le score mots-clés, corrigé au même endroit.
        if tname and v.get("is_item", True) and matches_target(d.get("title"), tname):
            score = max(score, 80.0)      # le modèle exact est nommé dans le titre
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

def fb_backfill(limit=None, delay=None):
    """Récupère les descriptions facebook manquantes, très lentement.

    Elles ne sont pas dans les résultats de recherche : il faut ouvrir chaque
    annonce. Les 237 d'un coup, c'est le motif qui fait restreindre un compte.
    On en prend quelques-unes par cycle, espacées, les plus récentes d'abord —
    ce sont celles qu'on est susceptible d'ouvrir.

    Les annonces déjà disparues sont ignorées : leur page n'existe plus.
    """
    if "fb_marketplace" not in sources.ADAPTERS or not browser.available():
        return 0
    limit = config.FB_BACKFILL_PER_CYCLE if limit is None else limit
    delay = config.FB_BACKFILL_DELAY if delay is None else delay
    if limit <= 0:
        return 0
    rows = db.q("""SELECT id, url, title FROM listings
                   WHERE source='fb_marketplace' AND status='active'
                     AND (description IS NULL OR description='')
                   ORDER BY last_seen DESC LIMIT ?""", (limit,))
    done = 0
    for i, r in enumerate(rows):
        if i:
            time.sleep(delay)
        try:
            det = sources.fb_item_details(r["url"])
        except Exception:
            continue
        d = (det.get("description") or "").strip()
        if d and d.lower() == (r["title"] or "").strip().lower():
            d = ""          # le titre recopié n'est pas une description
        if not d:
            # rien à lire : marquer pour ne pas y revenir à chaque cycle
            db.run("UPDATE listings SET description='' WHERE id=?", (r["id"],))
            continue
        db.run("UPDATE listings SET description=?, ai_enriched=0 WHERE id=?",
               (d[:4000], r["id"]))
        done += 1
    return done

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
        n = fb_backfill()
        if n:
            print(f"  [fb] {n} descriptions récupérées")
    except Exception:
        traceback.print_exc()
    try:
        n, dropped = finish_pending()
        if n:
            print(f"  [ia] {n} matchs restés provisoires jugés ({dropped} écartés)")
    except Exception:
        traceback.print_exc()
    return out

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

