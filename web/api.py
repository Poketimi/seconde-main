"""Points d'accès JSON pour le rafraîchissement automatique des pages."""
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

@app.get("/api/models")
def api_models():
    """Liste réelle des modèles que la clé actuelle peut atteindre."""
    ids = ai.list_models()
    return jsonify({"ok": ids is not None, "models": sorted(ids or [])})

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
