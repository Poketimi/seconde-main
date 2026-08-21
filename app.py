"""Flask UI. Local, single user, no auth on purpose."""
import json, time, threading
from flask import (Flask, render_template, request, redirect, url_for, jsonify,
                   flash, abort, session, g)
import db, geo, ai, sources, engine, config, browser, profile, sellers, reference, crawler, i18n, settings, auth

app = Flask(__name__)
db.init()
app.secret_key = auth.secret_key()   # tirée une fois, gardée en base
settings.load()          # la base a le dernier mot sur .env

# Pages accessibles sans être connecté. Tout le reste passe par le verrou --
# une liste blanche plutôt qu'un décorateur à poser sur quarante routes, qu'on
# oublie sur la quarante-et-unième.
OPEN = {"login", "static"}

@app.before_request
def require_login():
    if not auth.enabled():          # aucun compte créé : app ouverte, comme avant
        return None
    if request.endpoint in OPEN or session.get("user"):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "login"}), 401
    return redirect(url_for("login", next=request.full_path))

@app.route("/login", methods=["GET", "POST"])
def login():
    if not auth.enabled():
        return redirect(url_for("compte"))
    if request.method == "POST":
        u, pw = request.form.get("username", ""), request.form.get("password", "")
        if auth.check(u, pw):
            session["user"] = u.strip()
            nxt = request.form.get("next") or ""
            return redirect(nxt if nxt.startswith("/") else url_for("index"))
        flash("Nom d'utilisateur ou mot de passe incorrect.")
        return redirect(url_for("login", next=request.form.get("next", "")))
    return render_template("login.html", next=request.args.get("next", ""))

@app.post("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("login") if auth.enabled() else url_for("index"))

@app.route("/compte", methods=["GET", "POST"])
def compte():
    """Créer le compte (ce qui allume le verrou), ou changer le mot de passe."""
    if request.method == "POST":
        u = request.form.get("username", "")
        pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
        if pw != pw2:
            flash("Les deux mots de passe ne correspondent pas.")
        elif request.form.get("action") == "supprimer":
            auth.delete(session.get("user") or u)
            session.pop("user", None)
            flash("Compte supprimé — l'app est de nouveau ouverte sans mot de passe.")
        elif auth.enabled() and session.get("user"):
            ok, msg = auth.set_password(session["user"], pw)
            flash(msg)
        else:
            ok, msg = auth.create(u, pw)
            flash(msg)
            if ok:
                session["user"] = u.strip()
        return redirect(url_for("compte"))
    return render_template("compte.html", user=session.get("user"),
                           locked=auth.enabled())

@app.template_filter("ago")
def ago(ts):
    if not ts:
        return "—"
    d = time.time() - ts
    for lim, div, unit in ((60, 1, "s"), (3600, 60, "min"), (86400, 3600, "h"), (1e9, 86400, "j")):
        if d < lim:
            return f"il y a {int(d / div)}{unit}"
    return "—"

@app.template_filter("from_json")
def from_json(v):
    try:
        return json.loads(v or "[]")
    except Exception:
        return []

@app.template_filter("reject_sort")
def reject_sort(args):
    """Keep the active filters when switching sort order."""
    return {k: v for k, v in args.items() if k != "sort"}

@app.context_processor
def inject_now():
    return {"now": time.time(), "ui_lang": session.get("lang") or "",
            "LANGS": i18n.LANGS}

@app.template_filter("tr")
def tr(row):
    """Titre dans la langue de lecture choisie, original sinon.

    Un filtre plutôt qu'un JOIN dans chaque requête : les listes d'annonces
    sont construites par une dizaine de routes différentes, et l'affichage
    seul est concerné -- le matching, lui, reste sur le texte original.
    """
    lang = session.get("lang") or ""
    if not lang or lang == "orig":
        return row["title"]
    memo = g.setdefault("_tr", {})            # une seule requête par annonce et par page
    key = (i18n._id_of(row), lang)
    if key not in memo:
        memo[key] = i18n.title_for(row, lang)
    return memo[key]

@app.post("/langue")
def langue():
    """Langue de lecture, valable partout. 'orig' = les annonces telles quelles."""
    lang = request.form.get("lang", "")
    session["lang"] = lang if (lang in i18n.LANGS or lang == "orig") else ""
    return redirect(request.form.get("next") or url_for("index"))

@app.template_filter("dt")
def dt(ts):
    return time.strftime("%a %d %b, %H:%M", time.localtime(ts)) if ts else "—"

@app.template_filter("until")
def until(ts):
    """Time left on an auction: 'dans 2h14' / 'terminée'."""
    if not ts:
        return None
    d = ts - time.time()
    if d <= 0:
        return "terminée"
    if d < 3600:
        return f"dans {int(d // 60)}min"
    if d < 86400:
        return f"dans {int(d // 3600)}h{int((d % 3600) // 60):02d}"
    return f"dans {int(d // 86400)}j"

@app.template_filter("money")
def money(v, cur="CHF"):
    return f"{v:,.0f} {cur}".replace(",", "'") if v is not None else "—"

def _save_targets(sid, text):
    """One model per line: "Salomon QST 99" or "Salomon QST 99 | why".

    Rewrites the list but keeps the active flag of models already there, so
    editing the text does not silently re-enable something switched off.
    """
    wanted = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        name, _, why = line.partition("|")
        wanted.append((name.strip()[:80], why.strip()[:140]))
    existing = {r["name"]: r for r in db.q("SELECT * FROM targets WHERE search_id=?", (sid,))}
    keep = set()
    for name, why in wanted:
        keep.add(name)
        if name in existing:
            db.run("UPDATE targets SET why=? WHERE id=?", (why, existing[name]["id"]))
        else:
            db.run("""INSERT INTO targets(search_id,name,query,why,active,created_at)
                      VALUES(?,?,?,?,1,?)""", (sid, name, name, why, time.time()))
    for name, row in existing.items():
        if name not in keep:
            db.run("DELETE FROM targets WHERE id=?", (row["id"],))

def _state(new=False):
    """Assistant state, stored server-side and keyed by a token in the session.

    A cookie caps at 4 KB and Flask discards a bigger one silently, which lost
    the criteria and made the assistant look like it had produced nothing.
    """
    import uuid
    if new or not session.get("assist_token"):
        session["assist_token"] = uuid.uuid4().hex
    tok = session["assist_token"]
    row = db.q("SELECT data FROM assist_state WHERE token=?", (tok,), one=True)
    try:
        return tok, (json.loads(row["data"]) if row else {})
    except Exception:
        return tok, {}

def _save_state(tok, data):
    db.run("INSERT OR REPLACE INTO assist_state(token,data,updated_at) VALUES(?,?,?)",
           (tok, json.dumps(data, ensure_ascii=False), time.time()))

def safe_next(default_endpoint="index", **kw):
    """An internal URL to return to. Never trust an absolute/foreign target."""
    for cand in (request.form.get("next"), request.args.get("next"), request.referrer):
        if cand and cand.startswith("/") and not cand.startswith("//"):
            return cand
    return url_for(default_endpoint, **kw)

def get_or_404(table, rid):
    """Rows get deleted; stale links must 404, not blow up the page."""
    row = db.q(f"SELECT * FROM {table} WHERE id=?", (rid,), one=True)
    if row is None:
        abort(404)
    return row

def parse_origins(form):
    """Rows of (place, minutes, mode) -> geocoded origin dicts."""
    out = []
    for place, mins, mode in zip(form.getlist("o_place"), form.getlist("o_min"),
                                 form.getlist("o_mode")):
        place = (place or "").strip()
        if not place:
            continue
        c = geo.geocode(place)
        out.append({"label": place, "lat": c[0] if c else None, "lon": c[1] if c else None,
                    "max_minutes": float(mins or 20), "mode": mode or "car"})
    return out

@app.route("/")
def index():
    searches = db.q("""SELECT s.*, (SELECT COUNT(*) FROM matches m WHERE m.search_id=s.id) n,
                       (SELECT COUNT(*) FROM matches m WHERE m.search_id=s.id AND m.seen=0) unseen
                       FROM searches s ORDER BY s.active DESC, s.id""")
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
    s = get_or_404("searches", sid) if sid else None
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
                            origins,sources,active,created_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (*args, time.time()))
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

def _run_one(sid):
    s = db.q("SELECT * FROM searches WHERE id=?", (sid,), one=True)
    if s:
        try:
            engine.run_search(s)
        except Exception as e:
            print("run error", e)

@app.route("/search/<int:sid>")
def results(sid):
    s = get_or_404("searches", sid)
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

@app.route("/reglages", methods=["GET", "POST"])
def reglages():
    """Ton fournisseur, ta clé, tes modèles. Rien ici n'exige de redémarrer."""
    if request.method == "POST":
        vals = {k: request.form.get(k, "") for k in settings.FIELDS}
        # une case décochée n'est pas envoyée : sans ça on ne pourrait plus l'éteindre
        vals["CLAUDE_CLI"] = "1" if request.form.get("CLAUDE_CLI") else "0"
        # changer de fournisseur sans toucher au reste : reprendre ses défauts
        prov = (vals.get("AI_PROVIDER") or "").strip().lower()
        if prov and prov != config.AI_PROVIDER and prov in config.PROVIDERS:
            base, model = config.PROVIDERS[prov]
            vals["AI_BASE_URL"] = vals.get("AI_BASE_URL") or base
            vals["AI_MODEL"] = vals.get("AI_MODEL") or model
            vals["SMART_MODEL"] = vals.get("SMART_MODEL") or \
                config.PROVIDER_SMART.get(prov, model)
        # tableau des travaux : un select + un champ par ligne
        routes = {}
        for j in config.JOBS:
            routes[j] = {"account": request.form.get(f"acct_{j}", ""),
                         "model": request.form.get(f"model_{j}", "")}
        if any(request.form.get(f"acct_{j}") is not None for j in config.JOBS):
            vals["JOB_ROUTES"] = json.dumps(routes)
        settings.save(vals)
        if request.form.get("probe"):
            ok, msg = settings.probe()
            flash(("✓ " if ok else "✗ ") + msg)
        else:
            flash("Réglages enregistrés.")
        return redirect(url_for("reglages"))
    return render_template("reglages.html", cur=settings.current(),
                           mail_ok=__import__("mailbox").configured(),
                           jobs=config.JOBS, accounts=config.ACCOUNTS,
                           no_sub=config.JOBS_NO_SUBSCRIPTION,
                           routes=ai.job_table(),
                           providers=config.PROVIDERS, smart=config.PROVIDER_SMART,
                           spent=ai.spend_since(), left=ai.budget_left(),
                           credit=ai.credit(), tiers=ai.tier_status(),
                           cli_found=ai.cli_available(),
                           found=settings.detect(), info=config.PROVIDER_INFO,
                           recent=db.q("""SELECT model, purpose, cost_usd, ts FROM ai_spend
                                          ORDER BY ts DESC LIMIT 8"""))

@app.post("/reglages/connecter")
def reglages_connecter():
    """Brancher un service en un geste, depuis les cartes en haut de page."""
    ok, msg = settings.connect(request.form.get("which", ""),
                               request.form.get("key", "").strip())
    flash(("✓ " if ok else "✗ ") + msg)
    return redirect(url_for("reglages"))

@app.post("/reglages/oubli")
def reglages_oubli():
    """Effacer la clé stockée : .env (ou aucune clé) reprend la main."""
    settings.clear("AI_API_KEY")
    config.AI_API_KEY = ""
    flash("Clé effacée de la base. Redémarre pour reprendre celle de .env.")
    return redirect(url_for("reglages"))

@app.get("/api/models")
def api_models():
    """Liste réelle des modèles que la clé actuelle peut atteindre."""
    ids = ai.list_models()
    return jsonify({"ok": ids is not None, "models": sorted(ids or [])})

@app.post("/assist/probe")
def assist_probe():
    """Test one suggested marketplace and remember the verdict."""
    url = request.form.get("url", "")
    if not url.startswith("http"):
        return jsonify({"status": "notfound", "detail": "URL invalide"})
    status, detail = sources.probe_market(url)
    return jsonify({"status": status, "detail": detail})

@app.route("/api/status")
def api_status():
    """Cheap fingerprint of everything a page might be showing.

    The client polls this and reloads only when the value changes, so a scan
    finishing or a login completing shows up without touching the keyboard.
    """
    r = db.q("""SELECT (SELECT COUNT(*) FROM matches) m,
                       (SELECT COUNT(*) FROM listings) l,
                       (SELECT COALESCE(MAX(created_at),0) FROM matches) last_m,
                       (SELECT COALESCE(MAX(last_run),0) FROM searches) last_run,
                       (SELECT COUNT(*) FROM searches) s""", one=True)
    health = "|".join(f"{h['source']}:{h['status']}" for h in engine.health())
    login = browser.LOGIN_STATE
    return jsonify({
        "v": f"{r['m']}-{r['l']}-{r['s']}-{int(r['last_m'])}-{int(r['last_run'])}-{health}",
        "matches": r["m"], "listings": r["l"],
        "login_running": bool(login.get("running")),
        "login_site": login.get("site"), "login_message": login.get("message"),
        "scanning": engine.is_scanning(),
    })

# --- assisted search ---------------------------------------------------
@app.route("/assist", methods=["GET", "POST"])
def assist():
    """Broad object in. POST only stores state and redirects.

    Post/Redirect/Get throughout: these pages used to be rendered straight from
    a POST, so any reload -- including the status poller's -- hit them with GET
    and got 405.
    """
    if request.method == "POST":
        query = (request.form.get("query") or "").strip()
        if not query:
            flash("Décris l'objet que tu cherches.")
            return redirect(url_for("assist"))
        tok, _ = _state(new=True)
        _save_state(tok, {"query": query, "answers": {}, "asked": [],
                          "round": 0, "pending": None, "cat": "", "crit": None})
        return redirect(url_for("assist_questions"))
    return render_template("assist.html", budget=ai.budget_left(),
                           cap=config.SMART_BUDGET_USD, model=config.SMART_MODEL)

def _run_bg(tok, stage, fn):
    """Do slow model work off the request, so the page can show progress."""
    def job():
        _, st = _state_by_token(tok)
        try:
            fn(st)
            st["error"] = None
        except Exception as e:
            st["error"] = f"{type(e).__name__}: {e}"
            print("assist job failed:", e)
        st["working"] = None
        _save_state(tok, st)
    _, st = _state_by_token(tok)
    st["working"] = stage
    _save_state(tok, st)
    threading.Thread(target=job, daemon=True).start()

def _state_by_token(tok):
    row = db.q("SELECT data FROM assist_state WHERE token=?", (tok,), one=True)
    try:
        return tok, (json.loads(row["data"]) if row else {})
    except Exception:
        return tok, {}

@app.route("/api/assist/status")
def assist_status():
    tok, st = _state()
    return jsonify({"working": st.get("working"), "error": st.get("error"),
                    "ready": not st.get("working")})

@app.route("/assist/questions")
def assist_questions():
    tok, st = _state()
    if not st.get("query"):
        return redirect(url_for("assist"))
    if st.get("working"):
        return render_template("assist_wait.html", stage=st["working"],
                               query=st.get("query", ""), next=url_for("assist_questions"))
    if st.get("error"):
        flash(f"L'assistant a échoué : {st['error']}")
        st["error"] = None
        _save_state(tok, st)
    if st.get("pending"):                       # a follow-up wave is waiting
        questions, cached = st["pending"], False
        cat = st.get("cat") or ""
    elif not st.get("q_ready"):
        def work(state):
            cat, questions, cached = ai.ask_questions(state["query"])
            state["cat"] = cat
            state["pending"] = questions if questions else None
            state["q_ready"] = True
            state["cached"] = cached
        _run_bg(tok, "questions", work)
        return render_template("assist_wait.html", stage="questions",
                               query=st["query"], next=url_for("assist_questions"))
    else:
        questions = st.get("pending") or []
        cat, cached = st.get("cat") or "", st.get("cached", False)
        if len(questions) < 2:
            flash("L'assistant n'a pas pu proposer de questions utiles — "
                  "voici le formulaire classique avec ta demande telle quelle.")
            st["crit"] = {"ok": False, "name": st["query"], "query": st["query"]}
            _save_state(tok, st)
            return redirect(url_for("assist_review"))
    return render_template("assist_questions.html", query=st["query"], cat=cat,
                           questions=questions, known=profile.get_map(cat),
                           cached=cached, round=st.get("round", 0) + 1,
                           max_rounds=ai.MAX_ROUNDS, budget=ai.budget_left())

@app.post("/assist/answers")
def assist_answers():
    tok, st = _state()
    if not st.get("query"):
        return redirect(url_for("assist"))
    cat = st.get("cat") or request.form.get("cat") or ""

    # form.items() yields only the FIRST value of a repeated field, which would
    # silently drop every extra box ticked on a multi-select question
    for k in {k for k in request.form if k.startswith("q_")}:
        values = [v for v in request.form.getlist(k) if v]
        if not values:
            continue
        qid = k[2:]
        answer = ", ".join(values)
        st["answers"][qid] = answer
        st["asked"].append(qid)
        label = request.form.get(f"label_{qid}") or qid
        scope = request.form.get(f"scope_{qid}") or "domain"
        # remembered: reused silently next time, and visible at /profil
        profile.upsert(qid if scope == "global" else f"{cat}.{qid}", label, answer, scope)

    st["round"] = st.get("round", 0) + 1
    st["pending"] = None
    # A broad answer often hides the decisive detail: "avancé" says nothing about
    # an 80/20 vs 50/50 piste/freeride split. Ask again while it still matters.
    if st["round"] < ai.MAX_ROUNDS:
        more = ai.followup_questions(st["query"], st["answers"], st["asked"])
        if more:
            st["pending"] = more
            st["q_ready"] = True          # questions already in hand
            _save_state(tok, st)
            return redirect(url_for("assist_questions"))
    st["pending"] = None
    _save_state(tok, st)

    def work(state):
        crit = ai.build_criteria(state["query"], state["answers"],
                                 profile.get_map(cat), known_sources=list(sources.ADAPTERS))
        # the model has no web access, so its suggestions are checked before display
        crit["other_markets"] = sources.verify_markets(crit.get("other_markets") or [])
        state["crit"] = crit
    _run_bg(tok, "criteres", work)
    return redirect(url_for("assist_review"))

@app.route("/assist/review")
def assist_review():
    _tok, st = _state()
    if st.get("working"):
        return render_template("assist_wait.html", stage=st["working"],
                               query=st.get("query", ""), next=url_for("assist_review"))
    crit = st.get("crit")
    if not crit:
        return redirect(url_for("assist"))
    if not crit.get("ok", True):
        flash("⚠ L'assistant n'a pas réussi à construire les critères "
              "(appel au modèle en échec). La requête ci-dessous est ta demande "
              "brute — corrige-la, ou relance l'assistant.")
    last = db.q("SELECT origins FROM searches ORDER BY id DESC LIMIT 1", one=True)
    origins = json.loads(last["origins"]) if last and last["origins"] else []
    s = {"id": None, "name": crit.get("name"), "query": crit.get("query"),
         "reference": None, "category": crit.get("category"),
         "price_min": crit.get("price_min"), "price_max": crit.get("price_max"),
         "condition_min": crit.get("condition_min"), "seller_type": "any",
         "shipping_ok": 1, "exclude_kw": crit.get("exclude_kw"), "active": 1}
    return render_template("search_form.html", s=s, origins=origins,
                           tradeoffs=crit.get("tradeoffs") or [],
                           targets_text="\n".join(
                               f"{t['name']} | {t['why']}" for t in (crit.get("targets") or [])),
                           picked=crit.get("sources") or [],
                           explain=crit.get("explain"),
                           leads=crit.get("other_markets") or [],
                           all_sources=sorted(sources.ADAPTERS),
                           needs_browser=sorted(sources.NEEDS_BROWSER))

# --- personal data -----------------------------------------------------
@app.route("/profil")
def profil():
    return render_template("profil.html", facts=profile.get_all(),
                           spent=ai.spend_since(), cap=config.SMART_BUDGET_USD,
                           calls=db.q("SELECT * FROM ai_spend ORDER BY ts DESC LIMIT 10"))

@app.post("/profil/<path:k>/edit")
def profil_edit(k):
    row = db.q("SELECT * FROM profile_facts WHERE k=?", (k,), one=True)
    if row:
        profile.upsert(k, row["label"], request.form.get("value", ""), row["scope"])
        flash(f"« {row['label']} » corrigé.")
    return redirect(url_for("profil"))

@app.post("/profil/<path:k>/delete")
def profil_delete(k):
    profile.delete(k)
    flash("Donnée supprimée.")
    return redirect(url_for("profil"))

@app.post("/profil/wipe")
def profil_wipe():
    profile.wipe()
    flash("Toutes les données personnelles ont été effacées.")
    return redirect(url_for("profil"))

@app.route("/search/<int:sid>/targets")
def targets_page(sid):
    s = get_or_404("searches", sid)
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
    get_or_404("searches", sid)
    name = (request.form.get("name") or "").strip()
    if name:
        db.run("""INSERT INTO targets(search_id,name,query,active,created_at)
                  VALUES(?,?,?,1,?)""", (sid, name[:80], name[:80], time.time()))
    return redirect(url_for("targets_page", sid=sid))

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

def _sparkline(points, w=680, h=170, pad=28):
    """Median line with a p25-p75 band, as inline SVG.

    One series, so no legend: the title names it. Colours come from the theme
    tokens rather than being hard-coded, so it reads in light and dark.
    """
    if len(points) < 2:
        return None
    xs = list(range(len(points)))
    lows = [p["p25"] or p["median"] or 0 for p in points]
    highs = [p["p75"] or p["median"] or 0 for p in points]
    meds = [p["median"] or 0 for p in points]
    lo, hi = min(lows), max(highs)
    if hi <= lo:
        lo, hi = lo * 0.9 or 0, hi * 1.1 or 1
    def X(i):
        return pad + i * (w - 2 * pad) / max(1, len(points) - 1)
    def Y(v):
        return h - pad - (v - lo) * (h - 2 * pad) / (hi - lo)
    band = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(highs))
    band += " " + " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in reversed(list(enumerate(lows))))
    line = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(meds))
    dots = [(X(i), Y(v), points[i]["day"], v) for i, v in enumerate(meds)]
    return {"band": band, "line": line, "dots": dots, "w": w, "h": h,
            "lo": lo, "hi": hi, "y_lo": Y(lo), "y_hi": Y(hi), "pad": pad,
            "first": points[0]["day"], "last": points[-1]["day"]}

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

SITE_DOMAINS = {
    "fb_marketplace": "facebook.com", "ricardo": "ricardo.ch",
    "anibis": "anibis.ch", "tutti": "tutti.ch", "leboncoin": "leboncoin.fr",
}

LOGIN_URLS = {
    "fb_marketplace": "https://www.facebook.com/marketplace/",
    "ricardo": "https://www.ricardo.ch/fr",
}

@app.route("/crawler")
def crawler_page():
    """What this crawler is, and exactly what it has done.

    Published so a site operator (or you) can audit the behaviour and block it
    if they want to.
    """
    # Le domaine appartient-il à un site qu'on a décidé de ne pas crawler ?
    site_of = {v: k for k, v in SITE_DOMAINS.items()}
    doms = []
    for d in ("www.ricardo.ch", "www.anibis.ch", "www.tutti.ch", "www.leboncoin.fr"):
        st = crawler.state(d)
        rp = crawler.robots(d)
        site = site_of.get(d.replace("www.", ""), "")
        ok, why = crawler.policy(d)
        refused = site in sources.DENIED_BY_OPERATOR or not ok
        # "actif" ne voulait dire que « aucune pause HTTP en cours ». Un site
        # qu'on ne visite jamais n'accumule aucun refus, donc leboncoin
        # s'affichait actif sur la page censée dire la vérité sur ce que ce
        # robot fait. L'état part maintenant de la règle, pas du compteur.
        etat = ("refusé" if refused
                else "en pause" if crawler.denied_for(d) > 0
                else "actif" if (st and st["last_fetch"]) else "jamais visité")
        doms.append({
            "domain": d,
            "robots": "lu" if rp is not None else "illisible",
            "delay": crawler.crawl_delay(d) if not refused else None,
            # afficher un nombre de sitemaps pour un site refusé laissait
            # croire qu'on comptait s'en servir
            "sitemaps": len(crawler.sitemaps(d)) if not refused else None,
            "etat": etat,
            "refused": refused,
            "denied_min": crawler.denied_for(d) / 60,
            "streak": (st["deny_streak"] if st else 0),
            "note": (why if refused else (st["note"] if st else "")) or "",
            "last": (st["last_fetch"] if st else None),
        })
    return render_template("crawler.html", ua=crawler.USER_AGENT, doms=doms,
                           log=crawler.audit(60), min_delay=crawler.MIN_DELAY,
                           denied=sources.DENIED_BY_OPERATOR)

@app.route("/sources")
def sources_page():
    health = {r["source"]: dict(r) for r in engine.health()}
    rows = []
    for name in sorted(sources.ADAPTERS):
        h = health.get(name, {})
        sess = browser.session_info(SITE_DOMAINS.get(name, name)) \
            if name in sources.NEEDS_BROWSER else {}
        rows.append({
            "name": name,
            "session": sess,
            "needs_browser": name in sources.NEEDS_BROWSER,
            "login_url": LOGIN_URLS.get(name),
            "status": h.get("status") or "unknown",
            "label": engine.STATUS_LABEL.get(h.get("status"), "jamais lancé"),
            "detail": h.get("detail") or "",
            "fail_streak": h.get("fail_streak") or 0,
            "last_ok": h.get("last_ok"), "last_run": h.get("last_run"),
        })
    return render_template("sources.html", rows=rows,
                           login_state=browser.LOGIN_STATE,
                           playwright=browser.available())

@app.route("/connexions")
def connexions():
    """Une page qui répond à « qu'est-ce qui est branché, et qu'est-ce qui ne l'est pas ».

    Les connexions vivaient à trois endroits : la clé d'IA dans Réglages, les
    sessions de sites dans Sources, l'abonnement nulle part. Quand une session
    expire il faut un seul endroit où le voir et un seul bouton pour réparer.
    """
    auth_cli = ai.cli_auth()
    cards = [{
        "id": "claude_cli", "titre": "Abonnement Claude Code",
        "quoi": "Fait tourner l'entretien de l'assistant sur ton abonnement, "
                "sans clé d'API ni budget entamé.",
        "present": ai.cli_available(),
        "ok": bool(auth_cli and auth_cli.get("loggedIn")) and config.CLAUDE_CLI,
        "etat": ("session expirée" if auth_cli and not auth_cli.get("loggedIn")
                 else "actif" if config.CLAUDE_CLI else "installé, non activé")
                if ai.cli_available() else "binaire `claude` absent",
        "action": "cli_login", "bouton": "Se connecter",
        "aide": "Une fenêtre Terminal s'ouvre et te connecte à ton compte. "
                "Cette page se met à jour toute seule quand c'est fait.",
    }]
    for t in ai.tier_status():
        if t["provider"].startswith("claude_cli"):
            continue
        cards.append({
            "id": t["name"], "titre": f"Compte {t['name']} — {t['provider']}",
            "quoi": f"Modèle : {t['model']}",
            "present": True, "ok": t["live"],
            "etat": "en service" if t["live"]
                    else f"en pause {max(1, t['retry_in'] // 60)} min",
            "lien": url_for("reglages"), "bouton": "Régler",
            "aide": "Une clé refusée ou un compte vide met le compte de côté "
                    "un quart d'heure ; le suivant prend le relais.",
        })
    sites = []
    for name in sources.NEEDS_BROWSER:
        sess = browser.session_info(SITE_DOMAINS.get(name, name))
        sites.append({"name": name, "session": sess,
                      "login_url": LOGIN_URLS.get(name)})
    return render_template("connexions.html", cards=cards, sites=sites,
                           cli_login=ai.CLI_LOGIN, credit=ai.credit(),
                           login_state=browser.LOGIN_STATE,
                           playwright=browser.available())

@app.post("/connexions/claude")
def connexions_claude():
    if not ai.cli_login():
        flash("✗ " + (ai.CLI_LOGIN.get("message") or "impossible d'ouvrir la connexion"))
    else:
        settings.save({"CLAUDE_CLI": "1"})
        flash("Fenêtre Terminal ouverte — connecte-toi, la page se mettra à jour seule.")
    return redirect(url_for("connexions"))

@app.get("/api/connexions")
def api_connexions():
    """Sondage : dit à la page quand une connexion aboutit."""
    ai.cli_login_done()
    st = ai.cli_auth() or {}
    return jsonify({"cli_logged": bool(st.get("loggedIn")),
                    "cli_running": ai.CLI_LOGIN["running"],
                    "cli_message": ai.CLI_LOGIN["message"],
                    "login_running": bool(browser.LOGIN_STATE.get("running")),
                    "login_message": browser.LOGIN_STATE.get("message") or ""})

@app.post("/sources/<name>/login")
def source_login(name):
    target = LOGIN_URLS.get(name)
    if not target:
        flash(f"Pas d'URL de connexion pour {name}.")
        return redirect(url_for("sources_page"))
    if browser.LOGIN_STATE.get("running"):
        flash("Une fenêtre de connexion est déjà ouverte.")
        return redirect(url_for("sources_page"))
    threading.Thread(target=browser.open_login, args=(target, name), daemon=True).start()
    flash(f"Fenêtre ouverte pour {name} : connecte-toi, puis ferme la fenêtre.")
    return redirect(url_for("sources_page"))

@app.post("/sources/<name>/check")
def source_check(name):
    def run():
        # Always run the real adapter: a page that loads but yields no listings
        # is NOT ok, and reporting bytes-received as "ok" makes this page lie.
        sources.search(name, "velo")
        st, det = sources.LAST_STATUS.get(name, ("empty", ""))
        engine.record_health(name, st, det)
    threading.Thread(target=run, daemon=True).start()
    flash(f"Vérification de {name} lancée.")
    return redirect(url_for("sources_page"))

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

@app.after_request
def no_store(resp):
    """Back navigation must not restore a pre-save copy of the page."""
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp

@app.errorhandler(404)
def not_found(_):
    return render_template("notfound.html"), 404

if __name__ == "__main__":
    db.init()
    engine.start_loop()
    app.run(port=5055, debug=False)
