"""Annonces et produits : fiche, catalogue, favoris, langue de lecture."""
import json, time, threading
from flask import (render_template, request, redirect, url_for,
                   jsonify, flash, abort, session, g)
import db, geo, ai, sources, engine, config, browser, profile, sellers
import reference, crawler, i18n, settings, auth, mailbox
from .helpers import (get_or_404, safe_next, parse_origins, _save_targets,
                      _state, _save_state, _run_bg, _run_one, _state_by_token,
                      _sparkline, SITE_DOMAINS, LOGIN_URLS)

from ._router import Router

app = Router()

@app.post("/langue")
def langue():
    """Langue de lecture, valable partout. 'orig' = les annonces telles quelles."""
    lang = request.form.get("lang", "")
    session["lang"] = lang if (lang in i18n.LANGS or lang == "orig") else ""
    return redirect(request.form.get("next") or url_for("index"))

@app.route("/listing/<int:lid>")
def listing(lid):
    """Detail card, so a click does not jump straight out to the marketplace."""
    l = get_or_404("listings", lid)
    prod = db.q("SELECT * FROM products WHERE id=?", (l["product_id"],), one=True) \
        if l["product_id"] else None
    m = db.q("""SELECT m.*, s.name search_name FROM matches m JOIN searches s ON s.id=m.search_id
                WHERE m.listing_id=? ORDER BY m.score DESC LIMIT 1""", (lid,), one=True)
    similar = db.q("""SELECT * FROM listings WHERE product_id=? AND id<>? AND price>0
                      ORDER BY price LIMIT 8""", (l["product_id"], lid)) if l["product_id"] else []
    # Search payloads carry one thumbnail. Fetch the full gallery the first
    # time someone opens the item, then keep it.
    images = json.loads(l["images"] or "[]")
    seller = db.q("SELECT * FROM sellers WHERE id=?", (l["seller_ref"],), one=True) \
        if l["seller_ref"] else None

    if l["source"] == "fb_marketplace" and (not images or sellers.stale(seller)):
        # one page view yields both, and facebook views must stay rare
        det = sources.fb_item_details(l["url"])
        if det.get("images"):
            images = det["images"]
            db.run("UPDATE listings SET images=?, image=COALESCE(?,image) WHERE id=?",
                   (json.dumps(images), images[0], lid))
        if det.get("seller_key"):
            info = sellers.fetch_facebook(det["profile_url"])
            # the profile name is better than the card's; keep the card as fallback
            info.setdefault("name", None)
            info["name"] = info["name"] or l["seller_name"]
            sid = sellers.upsert("fb_marketplace", det["seller_key"], **info)
            db.run("UPDATE listings SET seller_ref=? WHERE id=?", (sid, lid))
            seller = db.q("SELECT * FROM sellers WHERE id=?", (sid,), one=True)
        l = db.q("SELECT * FROM listings WHERE id=?", (lid,), one=True)
    elif not images and l["source"] in ("ricardo", "anibis", "tutti"):
        images = sources.gallery(l["url"], l["source"])
        if images:
            db.run("UPDATE listings SET images=?, image=COALESCE(?,image) WHERE id=?",
                   (json.dumps(images), images[0], lid))
            l = db.q("SELECT * FROM listings WHERE id=?", (lid,), one=True)

    seen = sellers.seen_count(seller["id"]) if seller else 0
    trust = sellers.assess(seller, observed=seen)
    seller_name = sellers.display_name(seller, l["seller_name"])
    other = db.q("""SELECT id,title,price,currency FROM listings
                    WHERE seller_ref=? AND id<>? ORDER BY last_seen DESC LIMIT 5""",
                 (seller["id"], lid)) if seller else []

    ref = request.referrer or ""
    back = ref if (ref.startswith(request.host_url) and "/listing/" not in ref) else None
    if back:
        back = "/" + back[len(request.host_url):]
    elif m:
        back = url_for("results", sid=m["search_id"])
    else:
        back = url_for("catalogue", tab="annonces")
    # Langue de lecture. Le choix colle à la session : on ne le redemande pas
    # à chaque annonce. "orig" = telle qu'elle a été publiée.
    lang = request.args.get("lang")
    if lang:
        session["lang"] = lang
    lang = lang or session.get("lang") or ""
    if lang and lang != "orig" and lang != (l["lang"] or ""):
        i18n.ensure(lid, lang)        # déjà en cache la plupart du temps
    title, desc, translated = i18n.view(l, lang)
    return render_template("listing.html", l=l, prod=prod, m=m, similar=similar,
                           seller=seller, trust=trust, seller_name=seller_name,
                           seller_seen=seen, seller_other=other, back=back,
                           attrs=json.loads(l["attrs"] or "{}"), images=images,
                           title=title, desc=desc, translated=translated,
                           lang=lang, ready=i18n.have(lid))

@app.route("/favoris")
def favoris():
    rows = db.q("""SELECT m.*, l.*, m.id mid, s.name search_name
                   FROM matches m JOIN listings l ON l.id=m.listing_id
                   JOIN searches s ON s.id=m.search_id
                   WHERE m.starred=1 ORDER BY m.created_at DESC""")
    return render_template("favoris.html", rows=rows)

@app.route("/catalogue")
def catalogue():
    tab = request.args.get("tab", "produits")
    qtext = request.args.get("q", "").strip()
    products = listings = []
    if tab == "produits":
        if qtext:
            products = db.q("""SELECT p.*, COUNT(l.id) n FROM products p
                               LEFT JOIN listings l ON l.product_id=p.id
                               WHERE p.canonical_name LIKE ? OR p.brand LIKE ?
                               GROUP BY p.id ORDER BY n DESC LIMIT 300""",
                            (f"%{qtext}%", f"%{qtext}%"))
        else:
            products = db.q("""SELECT p.*, COUNT(l.id) n FROM products p
                               LEFT JOIN listings l ON l.product_id=p.id
                               GROUP BY p.id ORDER BY n DESC, p.canonical_name LIMIT 300""")
    else:
        if qtext:
            listings = db.q("""SELECT * FROM listings WHERE title LIKE ? OR description LIKE ?
                               ORDER BY first_seen DESC LIMIT 200""", (f"%{qtext}%", f"%{qtext}%"))
        else:
            listings = db.q("SELECT * FROM listings ORDER BY first_seen DESC LIMIT 200")
    return render_template("catalogue.html", tab=tab, q=qtext,
                           products=products, listings=listings)

@app.route("/product/<int:pid>")
def product(pid):
    p = get_or_404("products", pid)
    rows = db.q("SELECT * FROM listings WHERE product_id=? ORDER BY price", (pid,))
    hist = db.q("""SELECT day, n, p25, median, p75, lo, hi FROM price_points
                   WHERE product_id=? ORDER BY day""", (pid,))
    ref = reference.for_product(pid, p["canonical_name"])
    return render_template("product.html", p=p, rows=rows,
                           specs=json.loads(p["specs"] or "{}"),
                           hist=hist, ref=ref,
                           chart=_sparkline([dict(r) for r in hist]))

@app.route("/products")
def products():
    return redirect(url_for("catalogue", tab="produits"))

@app.route("/listings")
def listings():
    return redirect(url_for("catalogue", tab="annonces", q=request.args.get("q", "")))

@app.post("/match/<int:mid>/star")
def star(mid):
    """Set the flag explicitly rather than toggling.

    A toggle flips again whenever the browser replays the request (back
    navigation, refresh), which silently un-saved the item the user had
    just kept.
    """
    want = request.form.get("want")
    if want in ("0", "1"):
        db.run("UPDATE matches SET starred=? WHERE id=?", (int(want), mid))
    else:
        db.run("UPDATE matches SET starred=1-starred WHERE id=?", (mid,))
    return redirect(safe_next())
