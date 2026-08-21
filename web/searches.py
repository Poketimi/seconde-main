"""Recherches : créer, éditer, lancer, lire les résultats et les modèles ciblés."""
import json, time, threading
from flask import (render_template, request, redirect, url_for,
                   jsonify, flash, abort, session, g)
import db, geo, ai, sources, engine, config, browser, profile, sellers
import reference, crawler, i18n, settings, auth, mailbox
from .helpers import (me, mine, admin_only, owned_or_404,
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
    if num("fmins") is not None:
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
    db.run("UPDATE matches SET seen=1 WHERE search_id=?", (sid,))
    log = db.q("SELECT * FROM runlog WHERE search_id=? ORDER BY ts DESC LIMIT 12", (sid,))
    srcs = [r["source"] for r in db.q("""SELECT DISTINCT l.source FROM matches m
                JOIN listings l ON l.id=m.listing_id WHERE m.search_id=? ORDER BY l.source""", (sid,))]
    return render_template("results.html", s=s, rows=rows, log=log,
                           origins=json.loads(s["origins"] or "[]"),
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
