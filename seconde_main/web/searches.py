"""Recherches : créer, éditer, lancer, lire les résultats et les modèles ciblés."""
import json, time, threading
from flask import (render_template, request, redirect, url_for,
                   jsonify, flash, abort, session, g)
from seconde_main import db, geo, ai, sources, engine, config, browser, profile, sellers
from seconde_main import reference, crawler, i18n, settings, auth, mailbox
from .helpers import (ADVISOR, me, mine, admin_only, owned_or_404,
                      get_or_404, safe_next, parse_origins, _save_targets,
                      _state, _save_state, _run_bg, _run_one, _state_by_token,
                      _sparkline, SITE_DOMAINS, LOGIN_URLS)

from ._router import Router

app = Router()

@app.route("/")
def index():
    searches = db.q("""SELECT s.*, (SELECT COUNT(*) FROM matches m WHERE m.search_id=s.id) n,
                       (SELECT COUNT(*) FROM matches m WHERE m.search_id=s.id AND m.seen=0) unseen
                       FROM searches s WHERE {} ORDER BY s.active DESC, s.id"""
                    .format(mine("s.user_id")[0]), mine("s.user_id")[1])
    recent = db.q("""SELECT m.*, l.title, l.price, l.currency, l.image, l.url, l.source,
                            l.location_raw, s.name search_name
                     FROM matches m JOIN listings l ON l.id=m.listing_id
                     JOIN searches s ON s.id=m.search_id
                     ORDER BY m.created_at DESC LIMIT 24""")
    stats = db.q("""SELECT (SELECT COUNT(*) FROM listings) listings,
                           (SELECT COUNT(*) FROM products) products,
                           (SELECT COUNT(*) FROM matches)  matches,
                           (SELECT COUNT(*) FROM matches WHERE seen=0) unseen""", one=True)
    health = [dict(r) for r in engine.health()]
    for h in health:
        h["label"] = engine.STATUS_LABEL.get(h["status"], h["status"])
    down = [h for h in health if h["status"] != "ok"]
    return render_template("index.html", searches=searches, recent=recent, stats=stats,
                           health=health, down=down, ai_on=ai.available(),
                           poll=config.POLL_SECONDS)

@app.route("/search/new", methods=["GET", "POST"])
@app.route("/search/<int:sid>/edit", methods=["GET", "POST"])
def search_form(sid=None):
    s = owned_or_404(sid) if sid else None
    if request.method == "POST":
        f = request.form
        origins = json.dumps(parse_origins(f), ensure_ascii=False)
        picked = json.dumps(f.getlist("sources"))
        args = (f["name"], f["query"], f.get("reference") or None, f.get("category") or None,
                float(f["price_min"]) if f.get("price_min") else None,
                float(f["price_max"]) if f.get("price_max") else None,
                f.get("condition_min") or None, f.get("seller_type") or "any",
                1 if f.get("shipping_ok") else 0, f.get("exclude_kw") or None,
                origins, picked, 1 if f.get("active") else 0)
        targets_txt = f.get("targets") or ""
        if s:
            db.run("""UPDATE searches SET name=?,query=?,reference=?,category=?,price_min=?,
                      price_max=?,condition_min=?,seller_type=?,shipping_ok=?,exclude_kw=?,
                      origins=?,sources=?,active=? WHERE id=?""", (*args, sid))
        else:
            sid = db.run("""INSERT INTO searches(name,query,reference,category,price_min,
                            price_max,condition_min,seller_type,shipping_ok,exclude_kw,
                            origins,sources,active,created_at,user_id)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (*args, time.time(), me()))
        _save_targets(sid, targets_txt)
        flash("Recherche enregistrée. Premier scan en cours…")
        threading.Thread(target=_run_one, args=(sid,), daemon=True).start()
        return redirect(url_for("results", sid=sid))
    tg = db.q("SELECT name, why FROM targets WHERE search_id=? ORDER BY id", (sid,)) if sid else []
    return render_template("search_form.html", s=s,
                           targets_text="\n".join(
                               f"{r['name']}" + (f" | {r['why']}" if r["why"] else "") for r in tg),
                           origins=json.loads(s["origins"]) if s else [],
                           picked=json.loads(s["sources"]) if s else [],
                           all_sources=sorted(sources.ADAPTERS),
                           needs_browser=sorted(sources.NEEDS_BROWSER))

@app.route("/search/<int:sid>")
def results(sid):
    s = owned_or_404(sid)
    order = {"score": "m.score DESC", "price": "l.price ASC",
             "new": "l.first_seen DESC", "near": "m.travel_minutes ASC",
             "deal": "m.deal_delta ASC",
             "ending": "l.auction_end ASC"}.get(request.args.get("sort", "score"), "m.score DESC")

    # optional filters, applied on top of the search's own criteria
    f, where, args = request.args, [], []
    def num(k):
        try:
            return float(f[k]) if f.get(k) not in (None, "") else None
        except ValueError:
            return None
    if num("fmin") is not None:
        where.append("l.price >= ?"); args.append(num("fmin"))
    if num("fmax") is not None:
        where.append("l.price <= ?"); args.append(num("fmax"))
    # Le trajet stocké n'existe que si la recherche avait des origines — six
    # des sept n'en ont pas. Ce filtre-ci se calcule après coup, depuis un
    # point de départ choisi ici : c'est celui qui sert vraiment.
    if num("fmins") is not None and not f.get("forig"):
        where.append("(m.travel_minutes IS NULL OR m.travel_minutes <= ?)"); args.append(num("fmins"))
    if f.get("fsource"):
        where.append("l.source = ?"); args.append(f["fsource"])
    if f.get("floc"):
        where.append("l.location_raw LIKE ?"); args.append(f"%{f['floc']}%")
    # « livrable » : les annonces à retirer sur place sont inutiles quand le
    # vendeur est à 600 km. Le drapeau vient de l'API du site, corrigé quand le
    # vendeur écrit noir sur blanc qu'il n'envoie pas.
    if f.get("fship") == "1":
        where.append("l.shipping = 1")
    elif f.get("fship") == "0":
        where.append("l.shipping = 0")          # su, pas supposé
    elif f.get("fship") == "?":
        where.append("l.shipping IS NULL")
    if f.get("fseller"):
        where.append("l.seller_type = ?"); args.append(f["fseller"])
    if f.get("fauction") == "1":
        where.append("l.auction_end IS NOT NULL AND l.auction_end > ?"); args.append(time.time())
    if f.get("fstar") == "1":
        where.append("m.starred = 1")
    clause = (" AND " + " AND ".join(where)) if where else ""

    rows = db.q(f"""SELECT m.*, l.*, m.id mid, p.canonical_name, p.price_median
                    FROM matches m JOIN listings l ON l.id=m.listing_id
                    LEFT JOIN products p ON p.id=l.product_id
                    WHERE m.search_id=?{clause} ORDER BY {order}""", (sid, *args))
    # --- distance calculée à la volée ------------------------------------
    # Pas en SQL : le temps de trajet dépend d'un point de départ que
    # l'utilisateur choisit maintenant, pas de ce qui était réglé à la
    # création de la recherche. Estimation hors ligne (vol d'oiseau x détour
    # / vitesse), pas de routage : sur 250 annonces ce serait 250 appels.
    dist = {}
    forig, fmode = (f.get("forig") or "").strip(), (f.get("fmode") or "car")
    if forig:
        here = geo.by_postcode(forig, "CH") or geo.by_place_name(forig, "CH") \
            or geo.by_postcode(forig, "FR") or geo.by_place_name(forig, "FR")
        if not here:
            flash(f"Lieu « {forig} » introuvable — filtre distance ignoré.")
        else:
            limit = num("fmins")
            kept = []
            for r in rows:
                c = geo.locate_listing(r["location_raw"], r["postal_code"], r["country"])
                mins = geo.estimate_minutes(here, c, fmode) if c else None
                dist[r["listing_id"]] = mins
                # sans coordonnées on ne tranche pas : une annonce sans lieu
                # n'est pas « loin », elle est inconnue — on la garde.
                if limit is None or mins is None or mins <= limit:
                    kept.append(r)
            rows = kept

    # Les recommandations sont indépendantes des filtres : elles portent sur
    # le lot entier, pas sur ce qui est affiché à l'écran.
    picks = db.q("""SELECT m.reco_rank, m.reco_why, m.listing_id,
                           l.title, l.price, l.currency, l.image, l.source
                    FROM matches m JOIN listings l ON l.id = m.listing_id
                    WHERE m.search_id=? AND m.reco_rank IS NOT NULL
                    ORDER BY m.reco_rank""", (sid,))
    reco_summary = (db.q("SELECT reco_summary FROM searches WHERE id=?", (sid,),
                         one=True) or {})["reco_summary"] if picks else None
    try:
        tweak = json.loads(s["tweak_json"]) if s["tweak_json"] else None
    except (ValueError, TypeError):
        tweak = None
    db.run("UPDATE matches SET seen=1 WHERE search_id=?", (sid,))
    log = db.q("SELECT * FROM runlog WHERE search_id=? ORDER BY ts DESC LIMIT 12", (sid,))
    srcs = [r["source"] for r in db.q("""SELECT DISTINCT l.source FROM matches m
                JOIN listings l ON l.id=m.listing_id WHERE m.search_id=? ORDER BY l.source""", (sid,))]
    return render_template("results.html", s=s, rows=rows, log=log,
                           origins=json.loads(s["origins"] or "[]"),
                           picks=picks, reco_summary=reco_summary, tweak=tweak, dist=dist,
                           advising=(ADVISOR['busy'] == sid),
                           sort=request.args.get("sort", "score"),
                           srcs=srcs, f=f, nfilters=len(where))

@app.route("/search/<int:sid>/targets")
def targets_page(sid):
    s = owned_or_404(sid)
    rows = db.q("""SELECT t.*, (SELECT COUNT(*) FROM matches m WHERE m.target_id=t.id) n
                   FROM targets t WHERE t.search_id=? ORDER BY t.active DESC, t.id""", (sid,))
    return render_template("targets.html", s=s, rows=rows)

@app.post("/target/<int:tid>/toggle")
def target_toggle(tid):
    t = db.q("SELECT * FROM targets WHERE id=?", (tid,), one=True) or abort(404)
    db.run("UPDATE targets SET active=1-active WHERE id=?", (tid,))
    return redirect(url_for("targets_page", sid=t["search_id"]))

@app.post("/target/<int:tid>/delete")
def target_delete(tid):
    t = db.q("SELECT * FROM targets WHERE id=?", (tid,), one=True) or abort(404)
    db.run("DELETE FROM targets WHERE id=?", (tid,))
    return redirect(url_for("targets_page", sid=t["search_id"]))

@app.post("/search/<int:sid>/targets/add")
def target_add(sid):
    owned_or_404(sid)
    name = (request.form.get("name") or "").strip()
    if name:
        db.run("""INSERT INTO targets(search_id,name,query,active,created_at)
                  VALUES(?,?,?,1,?)""", (sid, name[:80], name[:80], time.time()))
    return redirect(url_for("targets_page", sid=sid))

@app.post("/search/<int:sid>/run")
def run_now(sid):
    threading.Thread(target=_run_one, args=(sid,), daemon=True).start()
    flash("Scan lancé.")
    return redirect(url_for("results", sid=sid))

@app.post("/search/<int:sid>/delete")
def delete(sid):
    db.run("DELETE FROM searches WHERE id=?", (sid,))
    return redirect(url_for("index"))

@app.post("/search/<int:sid>/toggle")
def toggle(sid):
    db.run("UPDATE searches SET active=1-active WHERE id=?", (sid,))
    return redirect(request.referrer or url_for("index"))


@app.post("/search/<int:sid>/tweak")
def apply_tweak(sid):
    """Applique l'ajustement proposé — sur clic, jamais tout seul.

    L'app ne réécrit pas une recherche que tu as réglée : elle propose, montre
    l'avant/après, et attend. « Ignorer » efface la proposition sans rien
    changer.
    """
    s = owned_or_404(sid)
    try:
        t = json.loads(s["tweak_json"] or "null") or {}
    except ValueError:
        t = {}
    if request.form.get("action") == "ignorer" or not t:
        db.run("UPDATE searches SET tweak_json=NULL WHERE id=?", (sid,))
        flash("Proposition écartée.")
        return redirect(url_for("results", sid=sid))
    changed = []
    for k in ai.reco.TWEAKABLE:
        if k in t:
            db.run(f"UPDATE searches SET {k}=? WHERE id=?", (t[k], sid))
            changed.append(f"{k} : {s[k]} → {t[k]}")
    # la proposition est consommée, et le lot doit être rejugé avec les
    # nouveaux critères
    db.run("UPDATE searches SET tweak_json=NULL, reco_key=NULL WHERE id=?", (sid,))
    flash("Recherche ajustée — " + " · ".join(changed) + ". Nouveau scan lancé.")
    threading.Thread(target=_run_one, args=(sid,), daemon=True).start()
    return redirect(url_for("results", sid=sid))


@app.post("/search/<int:sid>/conseil")
def ask_advisor(sid):
    """Demander un avis maintenant, sans attendre le prochain scan.

    Le conseil part normalement après un scan qui a changé quelque chose. Ce
    bouton le force : utile quand on regarde une liste et qu'on veut un avis
    tout de suite, ou après avoir modifié ses filtres.
    """
    s = owned_or_404(sid)
    if ADVISOR["busy"]:
        flash("Un avis est déjà en cours de préparation.")
        return redirect(url_for("results", sid=sid))

    def job():
        try:
            fresh = db.q("SELECT * FROM searches WHERE id=?", (sid,), one=True)
            ai.recommend(fresh, force=True)
        except Exception as e:
            print("advisor failed:", e)
        finally:
            ADVISOR["busy"] = None

    ADVISOR["busy"] = sid
    threading.Thread(target=job, daemon=True).start()
    flash("L'assistant regarde le lot — la page se mettra à jour toute seule.")
    return redirect(url_for("results", sid=sid))
