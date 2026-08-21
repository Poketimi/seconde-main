"""Fonctions partagées par les blueprints : accès base, état de l'assistant,
tâches de fond, et les deux tables de correspondance site -> domaine/URL."""
import json, time, threading
from flask import (Blueprint, render_template, request, redirect, url_for,
                   jsonify, flash, abort, session, g, current_app)
import db, geo, ai, sources, engine, config, browser, profile, sellers
import reference, crawler, i18n, settings, auth, mailbox

OPEN = {"login", "static"}

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

def _run_one(sid):
    s = db.q("SELECT * FROM searches WHERE id=?", (sid,), one=True)
    if s:
        try:
            engine.run_search(s)
        except Exception as e:
            print("run error", e)

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

SITE_DOMAINS = {
    "fb_marketplace": "facebook.com", "ricardo": "ricardo.ch",
    "anibis": "anibis.ch", "tutti": "tutti.ch", "leboncoin": "leboncoin.fr",
}

LOGIN_URLS = {
    "fb_marketplace": "https://www.facebook.com/marketplace/",
    "ricardo": "https://www.ricardo.ch/fr",
}


# --- multi-comptes ------------------------------------------------------
# Les annonces et les fiches produit restent communes : c'est un catalogue
# partagé, et le dupliquer par utilisateur multiplierait le scan et la dépense
# IA pour rien. Ce qui appartient à quelqu'un — recherches, favoris, profil —
# porte son user_id.

def me():
    """L'id de l'utilisateur connecté, ou None quand l'app est ouverte."""
    return auth.user_id(session.get("user")) if session.get("user") else None

def mine(col="user_id"):
    """(fragment SQL, args) pour ne montrer que ce qui m'appartient.

    Une ligne sans propriétaire (créée avant les comptes, ou pendant que l'app
    était ouverte) reste visible par tous : la migration n'efface rien.
    """
    uid = me()
    if uid is None:
        return "1=1", ()
    return f"({col} IS NULL OR {col} = ?)", (uid,)

def owned_or_404(sid):
    """La recherche demandée, si elle m'appartient. Sinon 404.

    404 plutôt que 403 : l'existence d'une recherche d'un autre compte n'a pas
    à être confirmée.
    """
    s = db.q("SELECT * FROM searches WHERE id=?", (sid,), one=True)
    uid = me()
    if not s or (uid is not None and s["user_id"] is not None and s["user_id"] != uid):
        abort(404)
    return s

def admin_only():
    """Renvoie une réponse si l'utilisateur n'a pas la main, sinon None."""
    if auth.is_admin(session.get("user")):
        return None
    flash("Réservé à l'administrateur.")
    return redirect(url_for("index"))
