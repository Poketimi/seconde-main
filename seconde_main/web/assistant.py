"""Entretien assisté et données personnelles."""
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

@app.post("/assist/probe")
def assist_probe():
    """Test one suggested marketplace and remember the verdict."""
    url = request.form.get("url", "")
    if not url.startswith("http"):
        return jsonify({"status": "notfound", "detail": "URL invalide"})
    status, detail = sources.probe_market(url)
    return jsonify({"status": status, "detail": detail})

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

@app.route("/api/assist/status")
def assist_status():
    tok, st = _state()
    started = st.get("started") or 0
    return jsonify({"working": st.get("working"), "error": st.get("error"),
                    "ready": not st.get("working"),
                    "step": st.get("step"),
                    "elapsed": int(time.time() - started) if started else 0})

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
        def work(state, step):
            step("Lecture de ta demande")
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

    def work(state, step):
        # Les deux étapes ont des durées très différentes — ~35 s pour le
        # modèle, quelques secondes pour la vérification. Le dire.
        step("Le modèle choisit les modèles concrets à chercher")
        crit = ai.build_criteria(state["query"], state["answers"],
                                 profile.get_map(cat), known_sources=list(sources.ADAPTERS))
        state["crit"] = crit
        # the model has no web access, so its suggestions are checked before display
        if crit.get("other_markets"):
            step("Vérification des marchés proposés")
            crit["other_markets"] = sources.verify_markets(crit["other_markets"])
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
