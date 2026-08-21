"""L'interface web, découpée par domaine.

    helpers.py    base de données, état de l'assistant, tâches de fond
    searches.py   recherches et résultats
    items.py      annonces, produits, catalogue, favoris
    assistant.py  entretien assisté et données personnelles
    admin.py      réglages, connexions, sources, crawler, compte
    api.py        points d'accès JSON du rafraîchissement automatique

`create_app()` assemble le tout : filtres de gabarit, garde de connexion,
puis les blueprints. Ajouter une page = une route dans le fichier de son
domaine, rien à déclarer ailleurs.
"""
import json, time
from flask import (Flask, request, redirect, url_for, jsonify, session, g,
                   render_template)
import db, config, ai, i18n, settings, auth

from . import helpers, searches, items, assistant, admin, api
from .helpers import OPEN


def create_app():
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    db.init()
    app.secret_key = auth.secret_key()   # tirée une fois, gardée en base
    settings.load()                      # la base a le dernier mot sur .env

    # `claude auth status` coûte ~350 ms de processus. Le préchauffer en fond
    # évite que la première visite de /reglages le paie.
    import threading
    threading.Thread(target=lambda: ai.cli_auth(), daemon=True).start()

    _filters(app)
    _guards(app)
    for mod in (searches, items, assistant, admin, api):
        mod.app.apply(app)
    return app


def _filters(app):
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
    

    @app.context_processor
    def inject_now():
        return {"now": time.time(), "ui_lang": session.get("lang") or "",
                "LANGS": i18n.LANGS}
    


def _guards(app):
    @app.before_request
    def require_login():
        if not auth.enabled():          # aucun compte créé : app ouverte, comme avant
            return None
        if request.endpoint in OPEN or session.get("user"):
            return None
        if request.path.startswith("/api/"):
            return jsonify({"error": "login"}), 401
        return redirect(url_for("login", next=request.full_path))
    
    @app.after_request
    def no_store(resp):
        """Back navigation must not restore a pre-save copy of the page."""
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        return resp
    
    @app.errorhandler(404)
    def not_found(_):
        return render_template("notfound.html"), 404
    
