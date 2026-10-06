"""Réglages, connexions, santé des sources, audit du crawler, compte."""
import json, time, threading
from flask import (render_template, request, redirect, url_for,
                   jsonify, flash, abort, session, g)
from seconde_main import db, geo, ai, sources, engine, config, browser, profile, sellers
from seconde_main import reference, crawler, i18n, settings, auth, mailbox
from .helpers import (get_or_404, safe_next, parse_origins, _save_targets,
                      _state, _save_state, _run_bg, _run_one, _state_by_token,
                      _sparkline, SITE_DOMAINS, LOGIN_URLS)

from ._router import Router

app = Router()

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
                           mail_ok=mailbox.configured(),
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

@app.route("/crawler")
def crawler_page():
    """What this crawler is, and exactly what it has done.

    Published so a site operator (or you) can audit the behaviour and block it
    if they want to.
    """
    # Le domaine appartient-il à un site qu'on a décidé de ne pas crawler ?
    site_of = {v: k for k, v in SITE_DOMAINS.items()}
    doms = []
    for d in ("www.ricardo.ch", "www.anibis.ch", "www.tutti.ch"):
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
    engine.forget_removed_sources()      # une source retirée n'est pas une panne
    # Une source jamais lancée depuis qu'elle existe n'a pas de verdict : mieux
    # vaut le dire que d'afficher celui de l'adaptateur qu'elle a remplacé.
    engine.forget_stale_verdicts()
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
