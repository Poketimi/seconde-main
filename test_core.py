"""Run: python3 test_core.py   (no framework, no network)

Covers the logic that silently rots: filters, travel time, dedupe, and the
full scrape->match path against a fake adapter.
"""
import json, time, sys, tempfile, shutil, pathlib
from pathlib import Path

import config, db

# Run against a scratch database: these tests DELETE rows, and pointing them at
# the live db wiped real listings on every run.
_TMP = Path(tempfile.mkdtemp(prefix="seconde-main-test-"))
_REAL_DB = config.DB_PATH
config.DB_PATH = db.DB_PATH = _TMP / "test.db"
db.init()
# postcode centroids are read-only reference data: copy them in rather than
# re-downloading GeoNames for every test run
if _REAL_DB.exists():
    import sqlite3
    src = sqlite3.connect(f"file:{_REAL_DB}?mode=ro", uri=True)   # read-only: never touch it
    rows = src.execute("SELECT country,postcode,name,admin1,lat,lon FROM places").fetchall()
    src.close()
    with db.connect() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS places (country TEXT, postcode TEXT, name TEXT,
                                               admin1 TEXT, lat REAL, lon REAL);
            CREATE INDEX IF NOT EXISTS idx_places_pc ON places(country, postcode);
            CREATE INDEX IF NOT EXISTS idx_places_name ON places(country, name);""")
        c.executemany("INSERT INTO places VALUES(?,?,?,?,?,?)", rows)

import geo, ai, sources, engine, browser, profile, sellers, reference, crawler, i18n, settings, auth, mailbox
crawler.init()          # crawl_cache / crawl_log / domain_state on the scratch db

def test_modules():
    geo.demo(); ai.demo(); sources.demo(); engine.demo(); i18n.demo(); settings.demo(); auth.demo(); mailbox.demo()

def test_end_to_end():
    """Whole pipeline on a fake source: no network, fully deterministic."""
    db.init()
    for t in ("matches", "listings", "searches"):
        db.run(f"DELETE FROM {t}")

    lausanne = geo.by_postcode("1003", "CH")
    geneve   = geo.by_postcode("1205", "CH")
    assert lausanne and geneve, "run: python3 seed_places.py"

    fake = [
        {"url": "https://x/1", "source": "fake", "title": "iPhone 13 Pro 256Go",
         "price": 400.0, "currency": "CHF", "postal_code": "1003", "country": "CH",
         "location_raw": "Lausanne", "seller_type": "private"},
        {"url": "https://x/2", "source": "fake", "title": "Coque iPhone 13 Pro",
         "price": 15.0, "currency": "CHF", "postal_code": "1003", "country": "CH",
         "location_raw": "Lausanne", "seller_type": "private"},
        {"url": "https://x/3", "source": "fake", "title": "iPhone 13 Pro 128Go",
         "price": 450.0, "currency": "CHF", "postal_code": "1205", "country": "CH",
         "location_raw": "Genève", "seller_type": "private"},
        {"url": "https://x/4", "source": "fake", "title": "iPhone 13 Pro 512Go",
         "price": 9999.0, "currency": "CHF", "postal_code": "1003", "country": "CH",
         "location_raw": "Lausanne", "seller_type": "private"},
        {"url": "https://x/5", "source": "fake", "title": "iPhone 13 Pro, livré",
         "price": 420.0, "currency": "CHF", "postal_code": "1205", "country": "CH",
         "location_raw": "Genève", "seller_type": "private", "shipping": 1},
    ]
    sources.ADAPTERS["fake"] = lambda q, s=None: fake

    origins = [{"label": "Lausanne", "lat": lausanne[0], "lon": lausanne[1],
                "max_minutes": 15, "mode": "car"}]
    sid = db.run("""INSERT INTO searches(name,query,price_min,price_max,seller_type,
        shipping_ok,exclude_kw,origins,sources,active,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,1,?)""",
        ("t", "iphone 13 pro", 100, 700, "any", 1, "", json.dumps(origins),
         json.dumps(["fake"]), time.time()))
    s = db.q("SELECT * FROM searches WHERE id=?", (sid,), one=True)
    new, matched = engine.run_search(s)
    assert new == 5, f"all 5 listings must be stored, got {new}"

    got = {r["url"] for r in db.q(
        "SELECT l.url FROM matches m JOIN listings l ON l.id=m.listing_id WHERE m.search_id=?",
        (sid,))}
    assert "https://x/1" in got, "the nearby in-budget iPhone must match"
    assert "https://x/2" not in got, "an accessory must not match"
    assert "https://x/3" not in got, "Geneve is beyond 15min of Lausanne"
    assert "https://x/4" not in got, "9999 is over the price ceiling"
    assert "https://x/5" in got, "a shipped item bypasses the distance filter"

    # idempotence: a second cycle must not duplicate anything
    new2, _ = engine.run_search(db.q("SELECT * FROM searches WHERE id=?", (sid,), one=True))
    assert new2 == 0, f"re-scan must add no listings, got {new2}"
    assert db.q("SELECT COUNT(*) n FROM listings", one=True)["n"] == 5
    assert len(got) == len({r["url"] for r in db.q(
        "SELECT l.url FROM matches m JOIN listings l ON l.id=m.listing_id WHERE m.search_id=?",
        (sid,))}), "duplicate matches after re-scan"

def test_challenge_detection():
    """A false OK is worse than a block: the scraper would store the interstitial."""
    blob = "A" * 40000                      # the base64 font that hid the giveaway
    challenge = (f'<style>@font-face{{src:url(data:font/woff2;base64,{blob})}}</style>'
                 '<body><h1>Un instant…</h1>'
                 '<iframe src="https://challenges.cloudflare.com/x"></iframe></body>')
    assert browser.looks_blocked(challenge), "must flag a challenge hidden behind a font blob"

    quiet = ('<body><div></div><script src="https://challenges.cloudflare.com/turnstile"></script>'
             '</body>')
    assert browser.looks_blocked(quiet), "near-empty page + cloudflare frame is a challenge"

    real = ("<body>" + "<p>iPhone 13 Pro 256Go, très bon état, batterie 89%, "
            "vendu avec chargeur et étui. Retrait à Lausanne.</p>" * 20 + "</body>")
    assert not browser.looks_blocked(real), "a genuine listing page must not be flagged"

    # the word may legitimately appear deep in a long page
    ok = "<body>" + "<p>Annonce normale. </p>" * 200 + "<p>captcha</p></body>"
    assert not browser.looks_blocked(ok), "late mention must not trip the detector"

def test_transit_and_radius():
    """Transit must be routed, not guessed, and ricardo must not pre-filter."""
    import sources, inspect, re
    src = inspect.getsource(sources.ricardo)
    # look for the URL actually being built with the param, not for the word
    # appearing in a comment explaining why we don't
    code = re.sub(r"#.*", "", src)
    assert 'f"zip_code=' not in code and "'zip_code=" not in code, \
        "ricardo must not narrow server-side; we filter on real travel time"

    laus = geo.by_postcode("1003", "CH")
    gr = geo.by_postcode("1422", "CH")
    est = geo.estimate_minutes(laus, gr, "transit")
    assert est > 90, "the naive transit estimate should be the pessimistic one"
    real = geo.transit_minutes(laus, gr)
    if real is not None:                      # skip when offline
        assert 20 < real < est, f"routed transit should beat the guess: {real} vs {est}"
        assert geo.travel_minutes(laus, gr, "transit") <= real + 0.01

    assert geo._in_ch(laus) and not geo._in_ch((48.85, 2.35)), "CH bounds wrong"
    assert geo.transit_minutes((48.85, 2.35), (48.86, 2.36)) is None, \
        "transit routing is CH-only and must decline elsewhere"

def test_truncated_model_replies_are_retried():
    """A reply cut off by max_tokens parses as nothing, and the criteria step
    silently fell back to the raw query -- the "paire de ski" bug."""
    assert ai.looks_truncated('{"a": 1, "b": [{"c": 2}')
    assert ai.looks_truncated('```json\n{"targets": [{"name": "x"')
    assert not ai.looks_truncated('{"a": 1}')
    assert not ai.looks_truncated('```json\n{"a": 1}\n```')
    assert not ai.looks_truncated("") and not ai.looks_truncated(None)

def test_tradeoffs_are_surfaced():
    """When the model contradicts a stated preference it must say so, instead
    of quietly returning something else."""
    real = ai.smart_chat
    ai.smart_chat = lambda sysm, usr, purpose, **kw: (json.dumps({
        "name": "moto", "query": "roadster", "category": "moto",
        "targets": [{"name": "Yamaha MT-09", "query": "Yamaha MT-09"}],
        "tradeoffs": [{"demande": "un GT", "propose": "des roadsters",
                       "pourquoi": "hors budget à cette taille",
                       "sinon": "monter à 8000 CHF"},
                      {"demande": "incomplet"}],          # pas de raison -> ignoré
    }), False)
    try:
        c = ai.build_criteria("moto", {}, {}, known_sources=[])
    finally:
        ai.smart_chat = real
    assert len(c["tradeoffs"]) == 1, c["tradeoffs"]
    t = c["tradeoffs"][0]
    assert t["demande"] == "un GT" and t["sinon"] == "monter à 8000 CHF"
    assert "budget" in t["pourquoi"]

def test_live_reload_is_opt_in():
    """Auto-reload used to be opt-out, so detail pages reloaded while being
    read: an outgoing click raced the reload, and Safari cancels a pending new
    tab when its opener navigates."""
    import app as webapp, re
    c = webapp.app.test_client()
    lid = db.run("""INSERT INTO listings(url,source,title,price,first_seen,status,active)
                    VALUES('https://x/live','f','t',1,0,'active',1)""")
    def live(path):
        m = re.search(r'<body data-live="(\d)"', c.get(path).data.decode())
        return m.group(1) if m else None
    for path in ("/", "/catalogue", "/sources"):
        assert live(path) == "1", f"{path} doit se rafraîchir"
    for path in (f"/listing/{lid}", "/favoris", "/assist", "/profil"):
        assert live(path) == "0", f"{path} ne doit JAMAIS se recharger sous l'utilisateur"
    db.run("DELETE FROM listings WHERE id=?", (lid,))

def test_budget_is_a_ceiling_not_a_floor():
    """Answering "150-300€" means at most 300 — a bargain at 90 is better, not
    worse. Turning it into a floor threw away exactly what the user wants."""
    real = ai.smart_chat
    def crit(pmin, pmax):
        ai.smart_chat = lambda sysm, usr, purpose, **kw: (json.dumps({
            "name": "x", "query": "y", "category": "sport",
            "price_min": pmin, "price_max": pmax}), False)
        try:
            return ai.build_criteria("x", {}, {}, known_sources=[])
        finally:
            ai.smart_chat = real

    c = crit(150, 300)
    assert c["price_min"] is None and c["price_max"] == 300, c
    c = crit(150, None)
    assert c["price_min"] is None, "un plancher seul est presque toujours un budget mal lu"
    # a genuinely low floor (junk filter) survives
    c = crit(20, 400)
    assert c["price_min"] == 20 and c["price_max"] == 400, c
    c = crit(None, 200)
    assert c["price_min"] is None and c["price_max"] == 200

def test_multi_select_questions():
    """Some questions have several right answers (piste AND freeride), but the
    answers must stay closed-ended -- checkboxes, never a text field."""
    qs = ai._valid_questions([
        {"id": "terrain", "text": "Terrains ?", "type": "multi",
         "options": ["Piste", "Poudreuse", "Park"], "scope": "domain"},
        {"id": "niveau", "text": "Niveau ?", "type": "choice",
         "options": ["Débutant", "Avancé"]},
        {"id": "seul", "text": "Multi à une option", "type": "multi", "options": ["a"]},
        {"id": "libre", "text": "Ta taille ?", "type": "text"},
    ])
    by = {q["id"]: q for q in qs}
    assert set(by) == {"terrain", "niveau"}, set(by)
    assert by["terrain"]["type"] == "multi" and len(by["terrain"]["options"]) == 3
    assert all(q["type"] in ("choice", "multi", "bool") for q in qs)

def test_multi_answers_are_all_kept():
    """request.form.items() yields only the FIRST value of a repeated field,
    which silently dropped every extra box ticked."""
    from werkzeug.datastructures import MultiDict
    import app as webapp
    profile.wipe()
    c = webapp.app.test_client()
    c.post("/assist", data={"query": "test multi"})
    c.post("/assist/answers", data=MultiDict([
        ("query", "test multi"), ("cat", "ski"),
        ("q_terrain", "Piste"), ("q_terrain", "Poudreuse"), ("q_terrain", "Park"),
        ("label_terrain", "Terrains"), ("scope_terrain", "domain"),
    ]))
    got = profile.get_map("ski").get("ski.terrain")
    assert got == "Piste, Poudreuse, Park", got
    profile.wipe()

def test_interview_is_closed_questions_only():
    """The MCQ/true-false rule must hold in code: a model that slips in a
    free-text field would otherwise put it straight in front of the user."""
    raw = [
        {"id": "level", "text": "Niveau ?", "type": "choice",
         "options": ["Débutant", "Avancé"], "scope": "domain"},
        {"id": "height", "text": "Ta taille ?", "type": "text"},          # free text
        {"id": "empty", "text": "Sans options", "type": "choice", "options": []},
        {"id": "single", "text": "Un seul choix", "type": "choice", "options": ["a"]},
        {"id": "new", "text": "Neuf ?", "type": "bool", "scope": "global"},
        {"text": "sans id", "type": "bool"},
    ]
    qs = ai._valid_questions(raw)
    assert [q["id"] for q in qs] == ["level", "new"], [q["id"] for q in qs]
    assert all(q["type"] in ("choice", "bool") for q in qs)
    assert all(q["options"] for q in qs if q["type"] == "choice")
    assert ai._valid_questions(None) == [] and ai._valid_questions([{}]) == []

def test_interview_template_cache():
    """Storing under the category but looking up by the phrase meant the cache
    never hit and every interview was paid for twice."""
    db.run("DELETE FROM interview_templates")
    calls = []
    real = ai.smart_chat
    ai.smart_chat = lambda sysm, usr, purpose, **kw: (calls.append(purpose) or (json.dumps({
        "category": "ski",
        "questions": [{"id": "level", "text": "Niveau ?", "type": "choice",
                       "options": ["Débutant", "Avancé"], "scope": "domain"},
                      {"id": "new", "text": "Neuf ?", "type": "bool", "scope": "global"}]
    }), False))
    try:
        cat, qs, cached = ai.ask_questions("une paire de ski")
        assert cat == "ski" and len(qs) == 2 and not cached
        cat2, qs2, cached2 = ai.ask_questions("une paire de ski")
        assert cached2, "même formulation: doit venir du cache"
        cat3, qs3, cached3 = ai.ask_questions("ski")
        assert cached3, "la catégorie doit aussi servir de clé"
        assert len(calls) == 1, f"{len(calls)} appels au modèle cher au lieu de 1"
    finally:
        ai.smart_chat = real

def test_smart_budget_cap():
    """Past the yearly cap the interview degrades to the cheap model."""
    db.run("DELETE FROM ai_spend")
    assert abs(ai.budget_left() - config.SMART_BUDGET_USD) < 1e-9
    db.run("""INSERT INTO ai_spend(ts,model,purpose,tokens_in,tokens_out,cost_usd)
              VALUES(?,?,?,?,?,?)""",
           (time.time(), config.SMART_MODEL, "test", 1000, 1000,
            config.SMART_BUDGET_USD + 1))
    assert ai.budget_left() == 0, "le plafond doit être atteint"

    called = {}
    real = ai.chat
    ai.chat = lambda sys_, usr, **kw: called.setdefault("models", kw.get("models")) or "{}"
    try:
        ai.smart_chat("s", "u", "test")
    finally:
        ai.chat = real
    assert config.SMART_MODEL not in called["models"], \
        "au-delà du plafond, le modèle cher ne doit plus être appelé"

    # spend older than the window must not count
    db.run("DELETE FROM ai_spend")
    db.run("""INSERT INTO ai_spend(ts,model,purpose,tokens_in,tokens_out,cost_usd)
              VALUES(?,?,?,?,?,?)""",
           (time.time() - 400 * 86400, "m", "old", 1, 1, 99))
    assert ai.budget_left() == config.SMART_BUDGET_USD, "fenêtre glissante cassée"

def test_spend_estimate_without_provider_cost():
    """If a provider omits usage.cost we must still charge the ledger."""
    db.run("DELETE FROM ai_spend")
    c = ai.record_spend("anthropic/claude-sonnet-5",
                        {"prompt_tokens": 1_000_000, "completion_tokens": 0}, "t")
    assert abs(c - 2.00) < 1e-6, f"attendu 2.00 $/M en entrée, obtenu {c}"
    c2 = ai.record_spend("anthropic/claude-sonnet-5",
                         {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.5}, "t")
    assert c2 == 0.5, "le coût annoncé par le fournisseur prime sur l'estimation"

def test_profile_crud():
    profile.wipe()
    profile.upsert("height_cm", "Taille", "170-180", "global")
    profile.upsert("ski.level", "Niveau", "Intermédiaire", "domain")
    profile.upsert("tennis.level", "Niveau", "Débutant", "domain")
    m = profile.get_map("ski")
    assert m["height_cm"] == "170-180" and m["ski.level"] == "Intermédiaire"
    assert "tennis.level" not in m, "les domaines ne doivent pas fuiter entre eux"
    profile.upsert("height_cm", "Taille", "180-190", "global")
    assert profile.get_map()["height_cm"] == "180-190"
    profile.delete("ski.level")
    assert "ski.level" not in profile.get_map("ski")
    profile.wipe()
    assert not profile.get_all()

def test_profile_pages():
    import app as webapp
    c = webapp.app.test_client()
    profile.wipe()
    profile.upsert("height_cm", "Taille", "170-180", "global")
    assert b"170-180" in c.get("/profil").data
    c.post("/profil/height_cm/edit", data={"value": "180-190"})
    assert profile.get_map()["height_cm"] == "180-190", "correction via l'UI cassée"
    c.post("/profil/height_cm/delete")
    assert not profile.get_all(), "suppression via l'UI cassée"
    profile.upsert("a", "A", "1", "global"); profile.upsert("b", "B", "2", "global")
    c.post("/profil/wipe")
    assert not profile.get_all(), "wipe via l'UI cassé"

def test_crawler_identifies_itself_honestly():
    """The crawler must be recognisable and blockable, never a disguise."""
    ua = crawler.USER_AGENT
    assert ua.startswith("SecondeMainBot/"), ua
    assert "contact:" in ua and "http" in ua, "un opérateur doit pouvoir nous joindre"
    for lie in ("Mozilla", "AppleWebKit", "Chrome/", "Safari/", "Gecko"):
        assert lie not in ua, f"le UA ne doit pas imiter un navigateur ({lie})"
    # no fingerprint impersonation or stealth flags in any acquisition path.
    # Strip comments AND docstrings first: the files explain why these things
    # are absent, and the explanation must not trip the check.
    import ast
    def live_code(path):
        tree = ast.parse(Path(path).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
               and isinstance(node.value.value, str):
                node.value.value = ""          # blank out docstrings
        return ast.unparse(tree)
    for mod in ("net.py", "crawler.py", "browser.py"):
        code = live_code(mod)
        assert "impersonate=" not in code, f"{mod} imite une empreinte TLS"
        assert "AutomationControlled" not in code, f"{mod} masque l'automatisation"
        assert "undetected" not in code.lower(), f"{mod} utilise un navigateur furtif"

def test_robots_prose_and_allowlists_are_obeyed():
    """robots.txt is written for humans too. leboncoin's opens with "It's
    forbidden to use search robots", then allowlists Googlebot & co with no
    User-agent: * group -- and the machine-readable rules alone read as
    "allowed", which is exactly the mistake this guards against."""
    crawler._robots_text["prose.test"] = (
        "## It's forbidden to use search robots or other automatic methods.\n"
        "User-agent: *\nDisallow:\n")
    ok, why = crawler.policy("prose.test")
    assert not ok and "interdit" in why, (ok, why)

    crawler._robots_text["allowlist.test"] = (
        "User-agent: Googlebot\nDisallow: /private\n"
        "User-agent: bingbot\nDisallow: /private\n")
    ok, why = crawler.policy("allowlist.test")
    assert not ok and "nommés" in why, (ok, why)

    crawler._robots_text["open.test"] = "User-agent: *\nDisallow: /admin\n"
    ok, why = crawler.policy("open.test")
    assert ok, (ok, why)

    crawler._robots_text.pop("unknown.test", None)
    assert crawler.policy("unknown.test")[0] is False, "sans robots.txt: on s'abstient"

def test_leboncoin_is_never_crawled():
    """Their robots.txt forbids automated access in writing.

    leboncoin does have an adapter again, but it reads the alert emails
    leboncoin itself sends -- no request ever goes to their site. The rule
    being pinned is "never crawled", which is what their robots.txt asks for,
    not "no code path", which was only ever a proxy for it.
    """
    assert "leboncoin" in sources.DENIED_BY_OPERATOR, "the refusal must stand"
    assert "leboncoin" in mailbox.SITES, "its adapter must be the mail one"
    # allowed() returns (ok, reason); asserting on the tuple alone is always true
    ok, why = crawler.allowed("https://www.leboncoin.fr/recherche?text=velo")
    assert ok is False, f"the crawler would fetch leboncoin: {why}"

def test_crawler_stops_on_denial():
    """403/429/CAPTCHA is a decision to respect, not an obstacle to route around."""
    db.run("DELETE FROM domain_state")
    assert crawler.denied_for("example.invalid") == 0
    crawler.mark_denied("example.invalid", 403, "refus")
    first = crawler.denied_for("example.invalid")
    assert first > 0, "un refus doit mettre le domaine en retrait"
    crawler.mark_denied("example.invalid", 403, "refus")
    assert crawler.denied_for("example.invalid") > first, "le retrait doit s'allonger"
    crawler.mark_ok("example.invalid")
    assert crawler.denied_for("example.invalid") == 0, "un succès remet à zéro"
    # challenge pages are denials even with a 200
    assert crawler.looks_denied("<html>DataDome</html>")
    assert crawler.looks_denied("Please enable JS and disable any ad blocker")
    assert not crawler.looks_denied("<p>Vélo de course 500 CHF Lausanne</p>" * 20)

def test_no_web_adapter_for_sites_that_refuse():
    """A denied site may only be reached by something it sends us itself."""
    for name in sources.DENIED_BY_OPERATOR:
        if name in sources.ADAPTERS:
            assert name in mailbox.SITES, \
                f"{name} refuse ce robot: seul un adaptateur e-mail est permis"

def test_crawl_delay_never_below_our_floor():
    """A site may permit a fast crawl; we still go slowly."""
    assert crawler.MIN_DELAY >= 10
    for dom in ("www.ricardo.ch",):
        d = crawler.crawl_delay(dom)
        assert d >= crawler.MIN_DELAY, f"{dom}: {d}s sous notre plancher"

def test_strategy_chain_falls_through():
    """Extraction tiers: try another way of READING the page, in order."""
    calls = []
    def dead():
        calls.append("dead"); return []
    def good():
        calls.append("good"); return [{"url": "https://x/1", "title": "t"}]
    rows, blocked = sources.try_strategies("s_chain", [("a", dead), ("b", good)])
    assert len(rows) == 1 and calls == ["dead", "good"]
    assert sources.LAST_STRATEGY["s_chain"] == "b", "la tier gagnante est mémorisée"
    # a first tier that works short-circuits the rest
    calls.clear()
    rows, _ = sources.try_strategies("s_chain", [("b", good), ("a", dead)])
    assert calls == ["good"], "on n'essaie pas les suivantes inutilement"
    # an exception in one tier must not sink the chain
    def boom():
        raise ValueError("cassé")
    rows, _ = sources.try_strategies("s_chain", [("x", boom), ("b", good)])
    assert len(rows) == 1
    rows, _ = sources.try_strategies("s_chain", [("a", dead)])
    assert rows == [] and sources.LAST_STRATEGY["s_chain"] is None

def test_datadome_and_blank_pages_are_blocks():
    """leboncoin's block page has NO visible text, so a keyword scan reads it
    as fine while it contains nothing at all."""
    assert browser.looks_blocked("<html><body></body></html>"), "page vide = blocage"
    assert browser.looks_blocked('<html><body>x<script>datadome</script></body></html>')
    real = "<html><body>" + "<p>Vélo de course carbone 500 CHF Lausanne</p>" * 30 + "</body></html>"
    assert not browser.looks_blocked(real), "une vraie page ne doit pas être signalée"
    import net
    assert net.looks_challenged("Please enable JS and disable any ad blocker")

def test_http_block_is_not_reported_as_empty():
    """Ricardo's CAPTCHA was recorded as "empty", so the backoff never fired
    and we kept hammering a site that was refusing us."""
    import net
    assert net.looks_challenged("<h1>Un instant…</h1>")
    assert net.looks_challenged("Completing the CAPTCHA proves you are human")
    assert not net.looks_challenged("<h1>Vélo de course carbone</h1> 500 CHF")

    def dead(q, s=None):
        return []
    sources.ADAPTERS["dead_src"] = dead
    net.LAST_BLOCKED.update(url="x", blocked=True)
    sources.search("dead_src", "velo")
    assert sources.LAST_STATUS["dead_src"][0] == "blocked", sources.LAST_STATUS["dead_src"]
    net.LAST_BLOCKED.update(url="x", blocked=False)
    sources.search("dead_src", "velo")
    assert sources.LAST_STATUS["dead_src"][0] == "empty"

def test_bulk_delete_is_guarded():
    """An ad-hoc script running DELETE FROM searches wiped this database twice."""
    import os
    # the guard protects market.db only, so point it there to exercise it
    real, db.DB_PATH = db.DB_PATH, Path("data/market.db")
    os.environ.pop("ALLOW_BULK_DELETE", None)
    try:
        for stmt in ("DELETE FROM searches", "delete from listings",
                     "DELETE FROM matches", "DROP TABLE targets"):
            try:
                db._guard(stmt)
                assert False, f"« {stmt} » aurait dû être refusé"
            except RuntimeError:
                pass
        db._guard("DELETE FROM searches WHERE id=-1")   # ciblé: autorisé
        db._guard("DELETE FROM ai_cache")               # table technique: autorisée
        os.environ["ALLOW_BULK_DELETE"] = "1"
        db._guard("DELETE FROM matches")                # explicitement autorisé
    finally:
        os.environ.pop("ALLOW_BULK_DELETE", None)
        db.DB_PATH = real

def test_health_is_a_whole_cycle_verdict():
    """Health was written per search, so one niche query returning nothing
    marked a source broken while it was serving 30 listings to another."""
    db.run("DELETE FROM matches"); db.run("DELETE FROM listings")
    db.run("DELETE FROM targets"); db.run("DELETE FROM searches")
    db.run("DELETE FROM source_health")
    engine.CYCLE_TALLY.clear()

    def flaky(q, s=None):
        if "introuvable" in q:
            return []
        return [dict(url="https://f/1", source="flaky", title="Vélo", price=100.0,
                     postal_code="1000", location_raw="Lausanne", seller_type="private")]
    sources.ADAPTERS["flaky"] = flaky
    for name, q in [("large", "velo"), ("niche", "introuvable xyz")]:
        db.run("""INSERT INTO searches(name,query,seller_type,shipping_ok,origins,sources,
                  active,created_at) VALUES(?,?,'any',1,'[]',?,1,0)""",
               (name, q, json.dumps(["flaky"])))
    real = ai.analyse
    ai.analyse = lambda i, r=None, budget=None: {}
    try:
        engine.run_all()
    finally:
        ai.analyse = real
    h = db.q("SELECT status, fail_streak FROM source_health WHERE source='flaky'", one=True)
    assert h["status"] == "ok", f"une requête vide ne doit pas condamner la source ({h['status']})"
    assert h["fail_streak"] == 0

def test_backoff_only_on_real_refusals():
    """"empty" means the query found nothing, not that the site refused.
    Backing off on it paused ricardo 20min while it returned 60 listings."""
    db.run("DELETE FROM source_health")
    now = time.time()
    for src, status, streak in [("s_empty", "empty", 5), ("s_blocked", "blocked", 3),
                                ("s_login", "login", 2), ("s_ok", "ok", 0),
                                ("s_error", "error", 1)]:
        db.run("""INSERT INTO source_health(source,status,detail,fail_streak,last_run,changed_at)
                  VALUES(?,?,'',?,?,?)""", (src, status, streak, now, now))
    assert engine.in_backoff("s_empty") == 0, "un résultat vide ne doit pas mettre en pause"
    assert engine.in_backoff("s_ok") == 0
    assert engine.in_backoff("s_blocked") > 0, "un refus doit mettre en pause"
    assert engine.in_backoff("s_login") > 0
    assert engine.in_backoff("s_error") > 0
    # and it must grow with repeated refusals, capped
    db.run("UPDATE source_health SET fail_streak=1 WHERE source='s_blocked'")
    short = engine.in_backoff("s_blocked")
    db.run("UPDATE source_health SET fail_streak=4 WHERE source='s_blocked'")
    assert engine.in_backoff("s_blocked") > short, "la pause doit croître"
    db.run("UPDATE source_health SET fail_streak=99 WHERE source='s_blocked'")
    assert engine.in_backoff("s_blocked") <= config.BACKOFF_MAX + 1, "plafonnée"
    db.run("DELETE FROM source_health")

def test_auto_recovery_is_throttled():
    """Recovery may retry a dead source, but not on every single cycle."""
    engine._last_recovery.clear()
    calls = []
    real_search, real_browser = sources.search, engine.__dict__.get("browser")
    sources.search = lambda src, q, spec=None: (calls.append(src) or [])
    import browser as br
    real_fetch = br.fetch
    br.fetch = lambda *a, **k: None
    try:
        engine.try_recover("fb_marketplace", "velo")
        assert len(calls) == 1, "la première tentative doit avoir lieu"
        engine.try_recover("fb_marketplace", "velo")
        assert len(calls) == 1, "deuxième tentative dans l'heure: doit être bloquée"
        engine._last_recovery["fb_marketplace"] = time.time() - engine.RECOVERY_EVERY - 1
        engine.try_recover("fb_marketplace", "velo")
        assert len(calls) == 2, "après expiration du délai, on réessaie"
        assert engine.try_recover("anibis", "velo") == [], "pas de home connue: pas de tentative"
    finally:
        sources.search, br.fetch = real_search, real_fetch

def test_assist_pages_survive_reload():
    """The status poller reloads pages. Anything rendered straight from a POST
    answered 405 on that reload and looked like the server had died."""
    import app as webapp
    c = webapp.app.test_client()
    for path in ("/assist", "/assist/questions", "/assist/review"):
        code = c.get(path).status_code
        assert code in (200, 302), f"GET {path} -> {code} (jamais 405)"
    # every POST endpoint must redirect, never render in place
    for rule in webapp.app.url_map.iter_rules():
        if "assist" in rule.rule and "POST" in rule.methods and "GET" not in rule.methods:
            assert rule.rule.rstrip("/") != "/assist/questions", \
                "/assist/questions ne doit plus être un POST rendu sur place"

def test_fb_gallery_excludes_other_listings():
    """A facebook item page renders a grid of OTHER listings. Taking every
    fbcdn image put 20 strangers' photos (motorbikes, an Apple TV) into one
    CPU listing -- 20 of 25 images on the page belonged to other items."""
    import inspect
    src = inspect.getsource(sources._gallery_fb)
    assert 'a[href*="/marketplace/item/"]' in src, \
        "le filtre qui exclut les annonces voisines a disparu"
    assert "closest(" in src, "l'exclusion doit remonter les ancêtres du <img>"
    assert "naturalWidth" in src, "les vignettes minuscules doivent rester exclues"

def test_sellers_are_linked_from_scan_data():
    """986 listings carried a seller name that went nowhere: records were only
    built when someone opened a facebook listing."""
    db.run("DELETE FROM sellers"); db.run("DELETE FROM listings")
    d = {"url": "https://s/1", "source": "ricardo", "title": "x", "price": 10.0,
         "seller_name": "405151536", "seller_key": "405151536"}
    lid, _ = engine.upsert_listing(d)
    sid = engine.link_seller(lid, d)
    assert sid, "un vendeur doit être créé sans requête supplémentaire"
    assert db.q("SELECT seller_ref FROM listings WHERE id=?", (lid,), one=True)["seller_ref"] == sid
    # a second listing from the same seller reuses the record
    d2 = dict(d, url="https://s/2")
    lid2, _ = engine.upsert_listing(d2)
    assert engine.link_seller(lid2, d2) == sid, "même vendeur = même fiche"
    assert db.q("SELECT COUNT(*) n FROM sellers", one=True)["n"] == 1
    assert sellers.seen_count(sid) == 2

    row = sellers.get("ricardo", "405151536")
    assert sellers.display_name(row) == "vendeur #405151536", \
        "un identifiant numérique n'est pas un nom"
    # our own observations stand in for a join date the site never publishes
    assert sellers.assess(row, observed=1)[0] == "unknown"
    assert sellers.assess(row, observed=9)[0] == "ok"
    assert engine.link_seller(lid, {"source": "x"}) is None, "sans clé: pas de fiche"

def test_reference_rejects_accessories():
    """An accessory naming the product is always cheaper and always wrong:
    a replacement remote made an Apple TV 4K look like 7.50 CHF."""
    acc = reference.is_accessory
    assert acc("Glitfix Ersatzfernbedienung für Apple TV 4K", "Apple TV 4K")
    assert acc("Coque de protection pour Apple TV", "Apple TV 4K")
    assert acc("Câble HDMI pour Apple TV 4K", "Apple TV 4K")
    assert not acc("APPLE TV 4K [2022]", "Apple TV 4K"), "le produit lui-même doit passer"
    assert not acc("HEAD Kore 99 Ski 23/24", "Head Kore 99")
    # a lookup that cannot verify must return nothing, never a wrong number
    assert reference.lookup("") is None

def test_seller_trust_flags_new_accounts():
    """A brand-new account is the common thread in marketplace scams.
    This flags RISK; it never certifies anyone as safe."""
    import datetime
    y = datetime.date.today().year
    db.run("DELETE FROM sellers")
    sellers.upsert("fb_marketplace", "new", name="N", member_since=y, listings_count=1)
    sellers.upsert("fb_marketplace", "old", name="O", member_since=y - 12, listings_count=30)
    sellers.upsert("fb_marketplace", "recent", name="R", member_since=y - 1, listings_count=1)
    lvl = lambda k: sellers.assess(sellers.get("fb_marketplace", k))[0]
    assert lvl("new") == "risk", "compte de l'année = risque"
    assert lvl("recent") == "caution"
    assert lvl("old") == "ok"
    assert sellers.assess(None)[0] == "unknown", "sans info: inconnu, jamais 'ok'"
    # an unreadable profile must not wipe what we already knew
    sellers.upsert("fb_marketplace", "old", name=None, member_since=None, listings_count=None)
    assert sellers.get("fb_marketplace", "old")["member_since"] == y - 12
    assert sellers.stale({"checked_at": 0}) and not sellers.stale(
        {"checked_at": time.time()})

def test_price_history_accumulates():
    """One point per product per day, updated in place, never duplicated."""
    db.run("DELETE FROM matches"); db.run("DELETE FROM listings")
    db.run("DELETE FROM products"); db.run("DELETE FROM price_points")
    pid = db.run("INSERT INTO products(norm_key,canonical_name,created_at) VALUES('h','H',0)")
    for i, pr in enumerate([100, 200, 300]):
        db.run("""INSERT INTO listings(url,source,title,price,product_id,active,first_seen)
                  VALUES(?,'f','x',?,?,1,0)""", (f"https://h/{i}", pr, pid))
    db.recompute_product_stats(pid)
    pts = db.q("SELECT * FROM price_points WHERE product_id=?", (pid,))
    assert len(pts) == 1 and pts[0]["median"] == 200, [dict(x) for x in pts]
    # same day again must update, not append
    db.run("""INSERT INTO listings(url,source,title,price,product_id,active,first_seen)
              VALUES('https://h/4','f','x',500,?,1,0)""", (pid,))
    db.recompute_product_stats(pid)
    pts = db.q("SELECT * FROM price_points WHERE product_id=?", (pid,))
    assert len(pts) == 1, "un seul point par jour"
    assert pts[0]["n"] == 4 and pts[0]["median"] == 300, dict(pts[0])
    # a gone listing still counts toward history only while active=1
    assert pts[0]["lo"] == 100 and pts[0]["hi"] == 500

def test_price_changes_are_logged():
    """A seller dropping the price is data, not something to overwrite."""
    db.run("DELETE FROM listing_prices"); db.run("DELETE FROM listings")
    d = {"url": "https://p/1", "source": "f", "title": "x", "price": 300.0}
    lid, new = engine.upsert_listing(d)
    assert new
    engine.upsert_listing({**d, "price": 250.0})
    engine.upsert_listing({**d, "price": 250.0})        # unchanged: no new row
    hist = db.q("SELECT price FROM listing_prices WHERE listing_id=? ORDER BY ts", (lid,))
    prices = [h["price"] for h in hist]
    assert prices == [300.0, 250.0], prices

def test_browser_sources_rotate_targets():
    """Every model on every cycle would mean hundreds of automated page views
    an hour on facebook -- the surest way to get an account restricted."""
    db.run("DELETE FROM matches"); db.run("DELETE FROM listings")
    db.run("DELETE FROM targets"); db.run("DELETE FROM searches")
    hits = {"light": 0, "browser": 0}
    def fake_light(q, s=None):
        hits["light"] += 1; return []
    def fake_browser(q, s=None):
        hits["browser"] += 1; return []
    sources.ADAPTERS["fake_light"] = fake_light
    sources.ADAPTERS["fake_browser"] = fake_browser
    sources.NEEDS_BROWSER = tuple(sources.NEEDS_BROWSER) + ("fake_browser",)
    try:
        sid = db.run("""INSERT INTO searches(name,query,seller_type,shipping_ok,origins,
                        sources,active,created_at) VALUES('r','x','any',1,'[]',?,1,0)""",
                     (json.dumps(["fake_light", "fake_browser"]),))
        for i in range(8):
            db.run("INSERT INTO targets(search_id,name,query,active,created_at)"
                   " VALUES(?,?,?,1,0)", (sid, f"Model {i}", f"model{i}"))
        real = ai.analyse
        ai.analyse = lambda items, req=None, budget=None: {}
        try:
            engine.run_search(db.q("SELECT * FROM searches WHERE id=?", (sid,), one=True))
        finally:
            ai.analyse = real
        # rotation now applies to every source: 2574 requests/day got us
        # challenged by ricardo, anibis, leboncoin and tutti at once
        assert hits["light"] == config.TARGETS_PER_CYCLE, \
            f"source HTTP: {hits['light']} au lieu de {config.TARGETS_PER_CYCLE}"
        assert hits["browser"] == config.BROWSER_TARGETS_PER_CYCLE, \
            f"source navigateur: {hits['browser']} au lieu de {config.BROWSER_TARGETS_PER_CYCLE}"
        assert hits["browser"] <= hits["light"], "le navigateur doit rester le plus économe"
    finally:
        sources.NEEDS_BROWSER = tuple(x for x in sources.NEEDS_BROWSER if x != "fake_browser")

def test_lifecycle_marks_instead_of_deleting():
    """History is the point: nothing is removed, disappearance is recorded."""
    db.run("DELETE FROM listings")
    now = time.time()
    old = db.run("""INSERT INTO listings(url,source,title,price,first_seen,last_seen,status,active)
                    VALUES('https://x/old','f','vieux',10,?,?,'active',1)""",
                 (now - 200 * 3600, now - 200 * 3600))
    fresh = db.run("""INSERT INTO listings(url,source,title,price,first_seen,last_seen,status,active)
                      VALUES('https://x/new','f','frais',10,?,?,'active',1)""", (now, now))
    auc = db.run("""INSERT INTO listings(url,source,title,price,first_seen,last_seen,
                    status,active,auction_end) VALUES('https://x/a','f','enchère',10,?,?,'active',1,?)""",
                 (now, now, now - 3600))
    engine.sweep_lifecycle()
    st = {r["url"]: r["status"] for r in db.q("SELECT url,status FROM listings")}
    assert st["https://x/old"] == "gone", st
    assert st["https://x/new"] == "active", st
    assert st["https://x/a"] == "ended", "une enchère passée est terminée, pas disparue"
    assert db.q("SELECT COUNT(*) n FROM listings", one=True)["n"] == 3, "rien ne doit être supprimé"
    # seen again -> back
    db.run("UPDATE listings SET last_seen=? WHERE id=?", (time.time(), old))
    engine.sweep_lifecycle()
    assert db.q("SELECT status FROM listings WHERE id=?", (old,), one=True)["status"] == "active"

def test_strict_model_matching():
    """With a concrete model in hand, a partial match is the wrong product.
    The model NUMBER is the whole point: Kore 93 is not Kore 99."""
    m = engine.matches_target
    assert m("Ski Head Kore 99 184cm", "Head Kore 99")
    assert m("SALOMON QST99 180cm", "Salomon QST 99"), "forme collée: courante chez les vendeurs"
    assert m("Völkl Mantra M6 177", "Volkl M6 Mantra"), "accents et ordre libres"
    assert m("Nordica Enforcer 100 - 186cm, 450 CHF", "Nordica Enforcer 100")
    assert not m("Head Kore 93 2023", "Head Kore 99"), "mauvais numéro"
    assert not m("Salomon QST 92", "Salomon QST 99")
    assert not m("Paire de skis Salomon", "Salomon QST 99"), "modèle absent"
    # a price next to the brand must not pass for the model number
    assert not m("Head Kore 93, 99 euros", "Head Kore 99")
    assert not m("Skis Nordica Enforcer, 100 CHF", "Nordica Enforcer 100")
    assert not m("n'importe quoi", "")

def test_targets_drive_the_scan():
    """Each model is searched by name and near misses are dropped."""
    db.run("DELETE FROM matches"); db.run("DELETE FROM listings")
    db.run("DELETE FROM targets"); db.run("DELETE FROM searches")
    rows = [dict(url="https://x/1", source="fake_t", title="Head Kore 99 184cm",
                 price=500.0, postal_code="1000", location_raw="Lausanne", seller_type="private"),
            dict(url="https://x/2", source="fake_t", title="Head Kore 93 177cm",
                 price=450.0, postal_code="1000", location_raw="Lausanne", seller_type="private"),
            dict(url="https://x/3", source="fake_t", title="Nordica Enforcer 100 186",
                 price=600.0, postal_code="1000", location_raw="Lausanne", seller_type="private")]
    sources.ADAPTERS["fake_t"] = lambda q, s=None: rows
    sid = db.run("""INSERT INTO searches(name,query,seller_type,shipping_ok,origins,sources,
                    active,created_at) VALUES('t','ski','any',1,'[]',?,1,0)""",
                 (json.dumps(["fake_t"]),))
    for n in ("Head Kore 99", "Nordica Enforcer 100"):
        db.run("INSERT INTO targets(search_id,name,query,active,created_at) VALUES(?,?,?,1,0)",
               (sid, n, n))
    real = ai.analyse
    ai.analyse = lambda items, req=None, budget=None: {}
    try:
        engine.run_search(db.q("SELECT * FROM searches WHERE id=?", (sid,), one=True))
    finally:
        ai.analyse = real
    got = {r["title"] for r in db.q("""SELECT l.title FROM matches m
                                       JOIN listings l ON l.id=m.listing_id
                                       WHERE m.search_id=?""", (sid,))}
    assert "Head Kore 99 184cm" in got and "Nordica Enforcer 100 186" in got
    assert "Head Kore 93 177cm" not in got, "le Kore 93 ne doit pas passer"
    linked = db.q("""SELECT COUNT(*) n FROM matches WHERE search_id=? AND target_id IS NOT NULL""",
                  (sid,), one=True)["n"]
    assert linked == 2, f"chaque match doit être rattaché à son modèle ({linked})"

def test_dup_key_merges_crossposts_not_lookalikes():
    """anibis and tutti share a platform, so the same ad appears on both.
    But four different "Enceinte Bluetooth" at 30 EUR are NOT one item --
    the postcode is what tells them apart."""
    k = engine.dup_key
    a = {"title": "A vendre JBL FLIP 7, neuf", "price": 99, "postal_code": "1004"}
    b = {"title": "A VENDRE  jbl flip 7 , NEUF", "price": 99.0, "postal_code": "1004"}
    assert k(a) == k(b), "même annonce reformatée: doit fusionner"
    assert k({"title": "Enceinte Bluetooth", "price": 30, "postal_code": "86000"}) != \
           k({"title": "Enceinte Bluetooth", "price": 30, "postal_code": "59240"}), \
        "titres génériques dans deux villes: annonces distinctes"
    assert k({"title": "Skis Head Kore", "price": 450, "postal_code": "1253"}) != \
           k({"title": "Skis Head Kore", "price": 400, "postal_code": "1253"}), \
        "prix différent: annonces distinctes"
    assert k({"title": "", "price": 1}) is None
    # accents and punctuation must not split one ad in two
    assert k({"title": "Vélo de course, carbone", "price": 500, "postal_code": "1000"}) == \
           k({"title": "velo de course carbone", "price": 500, "postal_code": "1000"})

def test_matches_skip_duplicates():
    """A cross-posted ad must produce one match, not one per site."""
    db.run("DELETE FROM matches"); db.run("DELETE FROM listings"); db.run("DELETE FROM searches")
    same = dict(title="JBL Flip 7", price=99.0, postal_code="1004", location_raw="Lausanne",
                seller_type="private")
    fake = [dict(same, url="https://anibis/x", source="anibis"),
            dict(same, url="https://tutti/y", source="tutti"),
            dict(same, url="https://tutti/z", source="tutti", postal_code="8000",
                 location_raw="Zurich")]          # autre ville: annonce distincte
    sources.ADAPTERS["fake_dup"] = lambda q, s=None: fake
    sid = db.run("""INSERT INTO searches(name,query,seller_type,shipping_ok,origins,sources,
                    active,created_at) VALUES('d','jbl','any',1,'[]',?,1,0)""",
                 (json.dumps(["fake_dup"]),))
    real_ai = ai.analyse                     # tests stay offline and free
    ai.analyse = lambda items, req=None, budget=None: {}
    try:
        engine.run_search(db.q("SELECT * FROM searches WHERE id=?", (sid,), one=True))
    finally:
        ai.analyse = real_ai
    assert db.q("SELECT COUNT(*) n FROM listings", one=True)["n"] == 3, "tout reste au catalogue"
    n = db.q("SELECT COUNT(*) n FROM matches WHERE search_id=?", (sid,), one=True)["n"]
    assert n == 2, f"attendu 2 matchs (1 dédupliqué + 1 autre ville), obtenu {n}"

def test_market_leads_are_verified():
    """The interview model has no web access and invents plausible domains:
    2 of 3 suggestions did not resolve in testing. Never show them unchecked."""
    real = sources.probe_market
    sources.probe_market = lambda url: {
        "https://dead.example/x": ("dead", "domaine inexistant"),
        "https://ok.example/x": ("parsable", "12 annonces"),
        "https://meh.example/x": ("manual", "rien de structuré"),
    }[url]
    try:
        out = sources.verify_markets([
            {"name": "Mort", "url": "https://dead.example/x"},
            {"name": "Bon", "url": "https://ok.example/x"},
            {"name": "Moyen", "url": "https://meh.example/x"}])
    finally:
        sources.probe_market = real
    assert [m["name"] for m in out] == ["Bon", "Moyen"], "un domaine mort doit disparaître"
    assert out[0]["status"] == "parsable" and out[1]["status"] == "manual"
    assert sources.verify_markets([]) == []

def test_criteria_filters_unknown_sources():
    """The model must not select an adapter we do not have."""
    real = ai.smart_chat
    ai.smart_chat = lambda sysm, usr, purpose, **kw: (json.dumps({
        "name": "x", "query": "y", "category": "sport",
        "sources": ["anibis", "site_qui_nexiste_pas"],
        "other_markets": [{"name": "A", "url": "notaurl"},
                          {"name": "B", "url": "https://ok.example/s"}],
    }), False)
    try:
        c = ai.build_criteria("ski", {}, {}, known_sources=["anibis", "tutti"])
    finally:
        ai.smart_chat = real
    assert c["sources"] == ["anibis"], c["sources"]
    assert [m["name"] for m in c["other_markets"]] == ["B"], "URL invalide à écarter"

def test_status_endpoint_changes():
    """The client reloads on this fingerprint, so it must move when data does."""
    import app as webapp
    c = webapp.app.test_client()
    v1 = c.get("/api/status").get_json()["v"]
    lid = db.run("INSERT INTO listings(url,source,title,price,first_seen)"
                 " VALUES('https://x/status','fake','t',1,0)")
    v2 = c.get("/api/status").get_json()["v"]
    assert v1 != v2, "une nouvelle annonce doit changer l'empreinte"
    assert c.get("/api/status").get_json()["v"] == v2, "stable sans changement"
    db.run("DELETE FROM listings WHERE id=?", (lid,))

def test_price_parsing():
    """Thousands separators: getting one wrong divides a price by 1000 silently."""
    for raw, want in [("CHF 2'000", 2000.0), ("CHF 2\u2019000", 2000.0),
                      ("CHF 2\u00a0000", 2000.0), ("2\u202f500 CHF", 2500.0),
                      ("2\u00a0100\u00a0CHF", 2100.0),   # the facebook case
                      ("CHF 1'250.-", 1250.0), ("600.-", 600.0), ("CHF 450", 450.0),
                      ("12,50", 12.5), ("1,234,567", 1234567.0),
                      ("Gratuit", None), ("", None), (None, None), (450, 450.0)]:
        got = sources._num(raw)
        assert got == want, f"_num({raw!r}) = {got!r}, expected {want!r}"

def test_star_is_not_a_toggle():
    """Back navigation replays the request; a toggle would un-save the item."""
    import app as webapp
    c = webapp.app.test_client()
    sid = db.run("INSERT INTO searches(name,query,origins,sources,active,created_at)"
                 " VALUES('s','q','[]','[]',1,0)")
    lid = db.run("INSERT INTO listings(url,source,title,price,first_seen)"
                 " VALUES('https://x/star','fake','t',1,0)")
    mid = db.run("INSERT INTO matches(search_id,listing_id,score,created_at) VALUES(?,?,9,0)",
                 (sid, lid))
    star = lambda want: c.post(f"/match/{mid}/star", data={"want": want, "next": "/favoris"})
    starred = lambda: db.q("SELECT starred FROM matches WHERE id=?", (mid,), one=True)["starred"]
    star("1"); assert starred() == 1, "save failed"
    star("1"); assert starred() == 1, "replaying the save must not un-save it"
    star("0"); assert starred() == 0, "explicit removal failed"

def test_image_hires():
    """Ricardo hands out 265x200 thumbnails; the detail view needs the big one."""
    thumb = "https://img.ricardostatic.ch/images/abc/t_265x200/slug"
    assert sources.hires(thumb).endswith("/t_1000x750/slug")
    assert sources.hires("https://c.anibis.ch/big/1.jpg") == "https://c.anibis.ch/big/1.jpg"
    assert sources.hires(None) is None

def test_stale_links_404():
    """A deleted search must 404, not crash the page with a 500."""
    import app as webapp
    webapp.db = db                      # share the scratch database
    c = webapp.app.test_client()
    for path in ("/search/999999", "/product/999999", "/search/999999/edit"):
        code = c.get(path).status_code
        assert code == 404, f"{path} returned {code}, expected 404"
    # /products and /listings are now redirects into /catalogue
    for path in ("/", "/sources", "/catalogue", "/catalogue?tab=annonces",
                 "/products", "/listings"):
        code = c.get(path, follow_redirects=True).status_code
        assert code == 200, f"{path} -> {code}"

def test_browser_serialised():
    """Chromium allows one process per profile: concurrent scans must queue.

    Before this, three searches touching the browser at once all failed with
    "profile already open" -- they killed each other.
    """
    import threading as th
    held = th.Event(); release = th.Event()

    def holder():                     # RLock is re-entrant: hold it elsewhere
        browser._BROWSER_LOCK.acquire()
        held.set(); release.wait(10)
        browser._BROWSER_LOCK.release()

    t = th.Thread(target=holder, daemon=True); t.start(); held.wait(5)
    old = browser.LOCK_TIMEOUT
    browser.LOCK_TIMEOUT = 1
    try:
        start = time.time()
        assert browser.fetch("https://example.com") is None
        assert browser.last_reason() == "busy", browser.last_reason()
        assert time.time() - start < 6, "must give up on the lock, not hang"
    finally:
        browser.LOCK_TIMEOUT = old
        release.set(); t.join(timeout=5)
    assert browser._BROWSER_LOCK.acquire(timeout=2), "lock must be released again"
    browser._BROWSER_LOCK.release()

def test_delete_search():
    """Deleting a search removes it and its matches, but keeps the listings."""
    import app as webapp
    c = webapp.app.test_client()
    sid = db.run("""INSERT INTO searches(name,query,origins,sources,active,created_at)
                    VALUES('jetable','x','[]','[]',1,0)""")
    lid = db.run("""INSERT INTO listings(url,source,title,price,first_seen)
                    VALUES('https://x/del','fake','t',1,0)""")
    db.run("INSERT INTO matches(search_id,listing_id,score,created_at) VALUES(?,?,50,0)",
           (sid, lid))
    assert c.post(f"/search/{sid}/delete", follow_redirects=True).status_code == 200
    assert db.q("SELECT 1 FROM searches WHERE id=?", (sid,), one=True) is None, "search survived"
    assert db.q("SELECT 1 FROM matches WHERE search_id=?", (sid,), one=True) is None, \
        "matches must cascade with the search"
    assert db.q("SELECT 1 FROM listings WHERE id=?", (lid,), one=True) is not None, \
        "listings are shared catalogue data and must survive"
    assert c.get(f"/search/{sid}").status_code == 404

def test_category_normalisation():
    """The model drifts off its own vocabulary; the DB must not."""
    for raw, want in [("bicycle", "bike"), ("Bicycles", "bike"), ("vélo", "bike"),
                      ("motorcycle", "moto"), ("smartphone", "phone"),
                      ("real_estate", "realestate"), ("bike", "bike"),
                      ("wingsuit", "other"), ("", None), (None, None)]:
        got = ai.norm_category(raw)
        assert got == want, f"norm_category({raw!r}) = {got!r}, expected {want!r}"
    assert ai.norm_category("BIKE ") == "bike", "must trim and lowercase"

def test_product_dedupe():
    """The same product described two ways must land in one fiche."""
    db.run("DELETE FROM matches"); db.run("DELETE FROM listings")
    db.run("DELETE FROM products")
    a = ai.upsert_product({"canonical_name": "Android 13 Car Stereo 7 inch",
                           "model": "Android 13 car stereo", "variant": "7 inch"})
    # same thing, fields split differently by the model
    b = ai.upsert_product({"canonical_name": "Android 13 Car Stereo 7 inch",
                           "model": "7 inch Android 13 car stereo"})
    assert a == b, f"identical product split into {a} and {b}"

    c = ai.upsert_product({"canonical_name": "Apple iPhone 13 Pro 256GB",
                           "brand": "Apple", "model": "iPhone 13 Pro", "variant": "256GB"})
    assert c != a, "different products must not be merged"

    # and the merge pass repairs rows that already got split
    d = db.run("INSERT INTO products(norm_key,canonical_name,created_at) VALUES(?,?,0)",
               ("stale-split-key", "Android 13 Car Stereo 7 inch"))
    assert ai.dedupe_products() >= 1
    assert db.q("SELECT id FROM products WHERE id=?", (d,), one=True) is None, \
        "duplicate row should have been merged away"

def test_product_stats():
    # listings reference products, so clear children first (the FK is doing its job)
    db.run("DELETE FROM matches")
    db.run("DELETE FROM listings")
    db.run("DELETE FROM products")
    pid = ai.upsert_product({"canonical_name": "Apple iPhone 13 Pro", "brand": "Apple",
                             "model": "iPhone 13 Pro", "category": "phone"})
    for i, p in enumerate([200, 300, 400, 500, 600]):
        db.run("INSERT INTO listings(url,source,title,price,product_id,active,first_seen)"
               " VALUES(?,?,?,?,?,1,?)", (f"https://p/{i}", "fake", "x", p, pid, time.time()))
    db.recompute_product_stats(pid)
    p = db.q("SELECT * FROM products WHERE id=?", (pid,), one=True)
    assert p["n_listings"] == 5 and p["price_median"] == 400, dict(p)
    assert p["price_p25"] == 300 and p["price_p75"] == 500, dict(p)



# --- translation -----------------------------------------------------------

def _fake_listing(url="https://tr/1", title="Bergschuhe Grösse 43",
                  desc="Wenig getragen, sehr guter Zustand."):
    db.run("DELETE FROM listing_i18n")
    db.run("DELETE FROM listings WHERE url=?", (url,))
    return db.run("INSERT INTO listings(url,source,title,description,first_seen)"
                  " VALUES(?,?,?,?,?)", (url, "fake", title, desc, time.time()))

def _stub_batches(calls, payload):
    """Replace the model with a canned answer and count how often it is asked."""
    def fake(system, listings, brief=None, budget=None, batch=None, job=None):
        calls.append(len(listings))
        return {i: payload for i, _ in enumerate(listings)}
    return fake

def test_translation_is_cached_and_keeps_the_original():
    """A language is produced once. Asking again must not call the model."""
    lid = _fake_listing()
    calls = []
    real_batches, real_avail = ai.run_batches, ai.available
    ai.run_batches = _stub_batches(calls, {
        "src": "de",
        "t": {"fr": {"title": "Chaussures de montagne taille 43", "desc": "Peu portées."},
              "en": {"title": "Hiking boots size 43", "desc": "Barely worn."}}})
    ai.available = lambda: True
    try:
        assert i18n.ensure(lid, "fr")["title"] == "Chaussures de montagne taille 43"
        assert len(calls) == 1, f"first request should call the model once, got {calls}"
        i18n.ensure(lid, "fr")
        i18n.ensure(lid, "en")          # written by the same call
        assert len(calls) == 1, f"cached languages must not re-call the model: {calls}"
        assert i18n.have(lid) == {"fr", "en"}
    finally:
        ai.run_batches, ai.available = real_batches, real_avail

    # the original is never overwritten
    l = db.q("SELECT * FROM listings WHERE id=?", (lid,), one=True)
    assert l["title"] == "Bergschuhe Grösse 43", "original title was clobbered"
    assert l["lang"] == "de", "source language not recorded"
    assert i18n.view(l, "orig") == (l["title"], l["description"], False)
    assert i18n.view(l, "fr")[0] == "Chaussures de montagne taille 43"
    assert i18n.view(l, "fr")[2] is True, "translated text must be flagged as such"
    assert i18n.view(l, "de")[2] is False, "the source language is not a translation"

def test_translation_falls_back_to_the_original():
    """No key, or a failed call: show the ad as published rather than nothing."""
    lid = _fake_listing(url="https://tr/2")
    l = db.q("SELECT * FROM listings WHERE id=?", (lid,), one=True)
    real = ai.available
    ai.available = lambda: False
    try:
        assert i18n.translate([l]) == 0
        assert i18n.ensure(lid, "fr") is None
    finally:
        ai.available = real
    assert i18n.view(l, "fr") == (l["title"], l["description"], False)
    assert i18n.ensure(lid, "klingon") is None, "unknown language must be refused"

def test_translation_backlog_skips_finished_listings():
    lid = _fake_listing(url="https://tr/3")
    assert lid in [r["id"] for r in i18n.pending(50)], "new listing should be queued"
    for c in i18n.AUTO:
        i18n._store(lid, c, "t", "d")
    assert lid not in [r["id"] for r in i18n.pending(50)], \
        "a fully translated listing must not be queued again"

def test_language_bar_is_on_the_page():
    import app
    lid = _fake_listing(url="https://tr/4")
    i18n._store(lid, "fr", "Chaussures de montagne", "Peu portées.")
    c = app.app.test_client()
    html = c.get(f"/listing/{lid}?lang=fr").get_data(as_text=True)
    assert "Chaussures de montagne" in html, "translated title not rendered"
    assert "Peu portées." in html, "translated description not rendered"
    assert "lang=orig" in html, "no way back to the original"
    assert "+ autre langue" in html, "no way to request another language"


# --- your own provider / key / models --------------------------------------

def _restore_ai(saved):
    for k, v in saved.items():
        setattr(config, k, v)
    config.AI_FALLBACKS = config.fallback_chain()

def test_reading_language_applies_to_lists():
    """The choice is global: lists must show it too, not just the detail page."""
    import app
    lid = _fake_listing(url="https://tr/5", title="Bergschuhe Grösse 43")
    i18n._store(lid, "fr", "Chaussures de montagne taille 43", "Peu portées.")
    c = app.app.test_client()

    html = c.get("/catalogue?tab=annonces").get_data(as_text=True)
    assert "Bergschuhe" in html, "original should show before any choice is made"

    r = c.post("/langue", data={"lang": "fr", "next": "/catalogue?tab=annonces"})
    assert r.status_code == 302
    html = c.get("/catalogue?tab=annonces").get_data(as_text=True)
    assert "Chaussures de montagne taille 43" in html, "list title not translated"

    # a listing with no translation must still appear, in its own language
    plain = _keep_listing("https://tr/6", "Vélo de course Cannondale")
    html = c.get("/catalogue?tab=annonces").get_data(as_text=True)
    assert "Vélo de course Cannondale" in html, "untranslated listing vanished"

    # and going back to the original is one click
    c.post("/langue", data={"lang": "orig", "next": "/catalogue?tab=annonces"})
    html = c.get("/catalogue?tab=annonces").get_data(as_text=True)
    assert "Bergschuhe" in html, "could not get back to the original"

def _keep_listing(url, title):
    """Like _fake_listing but without clearing what is already there."""
    db.run("DELETE FROM listings WHERE url=?", (url,))
    return db.run("INSERT INTO listings(url,source,title,first_seen) VALUES(?,?,?,?)",
                  (url, "fake", title, time.time()))

def test_own_provider_and_models():
    """Switching provider must retarget the endpoint, the chain and the key."""
    saved = {k: getattr(config, k) for k in settings.FIELDS}
    saved["AI_FALLBACKS"] = config.AI_FALLBACKS
    db.run("DELETE FROM settings")
    try:
        settings.save({"AI_PROVIDER": "anthropic",
                       "AI_BASE_URL": "https://api.anthropic.com/v1",
                       "AI_MODEL": "claude-haiku-4-5-20251001",
                       "SMART_MODEL": "claude-sonnet-5",
                       "AI_API_KEY": "sk-ant-secret-value-1234",
                       "SMART_BUDGET_USD": "25"})
        assert config.AI_BASE_URL == "https://api.anthropic.com/v1"
        assert config.SMART_BUDGET_USD == 25.0
        # openrouter-only spares must not be offered to another provider
        assert config.AI_FALLBACKS == ["claude-haiku-4-5-20251001"], config.AI_FALLBACKS

        # a blank key means "keep the one I have", not "erase it"
        settings.save({"AI_API_KEY": "", "AI_MODEL": "claude-opus-5"})
        assert config.AI_API_KEY == "sk-ant-secret-value-1234", "blank field wiped the key"
        assert config.AI_MODEL == "claude-opus-5"

        # and it survives a restart: load() replays the db over .env
        config.AI_MODEL = "something-else"
        settings.load()
        assert config.AI_MODEL == "claude-opus-5", "settings not persisted"

        # an unpriced model still counts against the cap
        assert config.SMART_PRICES.get("no-such-model") is None
        db.run("DELETE FROM ai_spend")
        cost = ai.record_spend("no-such-model", {"prompt_tokens": 1000,
                                                 "completion_tokens": 1000}, "test")
        assert cost > 0, "unknown model billed as free"

        settings.clear("AI_API_KEY")
        assert "AI_API_KEY" not in settings.stored()
    finally:
        db.run("DELETE FROM settings")
        _restore_ai(saved)

def test_smart_model_steps_down_when_the_account_is_empty():
    """The ledger only sees its own calls; an empty account must still demote."""
    saved = {k: getattr(config, k) for k in settings.FIELDS}
    real_chat, real_credit = ai.chat, ai.credit
    seen = {}
    def spy(system, user, temperature=0.0, max_tokens=8000, models=None,
            on_usage=None, cli=False, job=None):
        seen["chain"] = list(models or [])
        return None
    try:
        ai.chat = spy
        db.run("DELETE FROM ai_spend")          # well under the yearly cap

        ai.credit = lambda max_age=300: 4.20    # money left: use the good model
        ai.smart_chat("s", "u", "test")
        assert seen["chain"][0] == config.SMART_MODEL, seen["chain"]

        ai.credit = lambda max_age=300: 0.0     # account dry: step down
        ai.smart_chat("s", "u", "test")
        assert config.SMART_MODEL not in seen["chain"], \
            f"empty account still billed the expensive model: {seen['chain']}"
        assert seen["chain"][0] == config.AI_MODEL

        ai.credit = lambda max_age=300: None    # provider says nothing: carry on
        ai.smart_chat("s", "u", "test")
        assert seen["chain"][0] == config.SMART_MODEL, \
            "an unknown balance must not be read as an empty one"
    finally:
        ai.chat, ai.credit = real_chat, real_credit
        _restore_ai(saved)

def test_settings_page_never_echoes_the_key():
    import app
    saved = {k: getattr(config, k) for k in settings.FIELDS}
    try:
        config.AI_API_KEY = "sk-or-v1-thisisasecretkeyvalue"
        html = app.app.test_client().get("/reglages").get_data(as_text=True)
        assert "thisisasecretkeyvalue" not in html, "the key was rendered into the page"
        assert "sk-or-v1" in html, "no hint of which key is in use"
        assert "anthropic" in html and "openai" in html, "own-account providers missing"
    finally:
        _restore_ai(saved)


# --- login -----------------------------------------------------------------

def test_login_locks_the_app_only_once_an_account_exists():
    import app
    db.run("DELETE FROM users")
    c = app.app.test_client()
    assert c.get("/catalogue").status_code == 200, "no account should mean no lock"

    ok, _ = auth.create("timi", "hunter2")
    assert ok
    c = app.app.test_client()
    r = c.get("/catalogue")
    assert r.status_code == 302 and "/login" in r.headers["Location"], \
        f"app still open after an account was created: {r.status_code}"
    # a poller must get a status code, not a login page it cannot parse
    assert c.get("/api/status").status_code == 401

    assert c.post("/login", data={"username": "timi", "password": "wrong"}).status_code == 302
    assert c.get("/catalogue").status_code == 302, "wrong password let us in"

    c.post("/login", data={"username": "timi", "password": "hunter2"})
    assert c.get("/catalogue").status_code == 200, "correct password rejected"

    c.post("/logout")
    assert c.get("/catalogue").status_code == 302, "logout did not take effect"
    db.run("DELETE FROM users")

def test_password_is_never_stored_in_the_clear():
    db.run("DELETE FROM users")
    auth.create("timi", "correct horse battery staple")
    row = db.q("SELECT pw, salt FROM users WHERE username='timi'", one=True)
    assert "correct horse" not in row["pw"], "password readable in the database"
    assert len(row["salt"]) == 32, "no per-account salt"
    # two accounts, same password, different hashes
    auth.create("autre", "correct horse battery staple")
    other = db.q("SELECT pw FROM users WHERE username='autre'", one=True)
    assert other["pw"] != row["pw"], "same password hashed identically"
    db.run("DELETE FROM users")

def test_subscription_tier_is_assistant_only_and_degrades_cleanly():
    """The subscription must never carry bulk work, and must not break anything."""
    saved = {k: getattr(config, k) for k in settings.FIELDS}
    real_cli, real_which = ai._cli_chat, ai.shutil.which
    ai._tier_down.clear()
    try:
        config.CLAUDE_CLI = True
        ai.shutil.which = lambda n: "/fake/claude"

        # bulk work must not see it, however it is configured
        assert "abonnement" not in [t["name"] for t in ai.tiers()], \
            "bulk calls would go through the subscription and hit its rate limits"
        assert "abonnement" == ai.tiers(cli_ok=True)[0]["name"], \
            "the assistant should prefer the subscription: it costs no credit"

        # switched off, it disappears entirely
        config.CLAUDE_CLI = False
        assert "abonnement" not in [t["name"] for t in ai.tiers(cli_ok=True)]
        config.CLAUDE_CLI = True

        # an expired session parks it and falls through to the API key
        ai._cli_chat = lambda *a, **k: (None, "OAuth session expired")
        db.run("DELETE FROM ai_cache")
        calls = []
        real_post = ai.net.post_json
        class R:
            status_code = 200
            def json(self):
                return {"choices": [{"message": {"content": '{"ok":1}'}}], "usage": {}}
        ai.net.post_json = lambda u, b, headers=None, timeout=None: (calls.append(u), R())[1]
        try:
            assert ai.chat("s", "u", cli=True) == '{"ok":1}', "no fallback after expiry"
            assert calls, "the API key was never tried"
            assert ai._tier_down.get("abonnement", 0) > time.time(), \
                "expired subscription retried on every call"
        finally:
            ai.net.post_json = real_post
    finally:
        ai._cli_chat, ai.shutil.which = real_cli, real_which
        ai._tier_down.clear()
        db.run("DELETE FROM ai_cache")
        _restore_ai(saved)
        config.CLAUDE_CLI = saved.get("CLAUDE_CLI", False)

def test_one_click_connect():
    saved = {k: getattr(config, k) for k in settings.FIELDS}
    real_probe = settings.probe
    try:
        db.run("DELETE FROM settings")
        settings.probe = lambda: (True, "ok")

        ok, msg = settings.connect("groq", "")
        assert not ok and "clé" in msg, "a keyed provider was accepted with no key"

        ok, _ = settings.connect("groq", "gsk-test")
        assert ok and config.AI_PROVIDER == "groq"
        assert config.AI_BASE_URL == config.PROVIDERS["groq"][0]
        assert config.AI_API_KEY == "gsk-test"
        assert config.AI_FALLBACKS == [config.PROVIDERS["groq"][1]], \
            "openrouter spares leaked onto another provider"

        # a local runtime needs no key at all
        ok, _ = settings.connect("ollama", "")
        assert ok and config.AI_PROVIDER == "ollama"

        assert settings.connect("nonesuch", "k")[0] is False
    finally:
        settings.probe = real_probe
        db.run("DELETE FROM settings")
        _restore_ai(saved)

def test_settings_page_lists_every_provider():
    import app
    html = app.app.test_client().get("/reglages").get_data(as_text=True)
    for p, meta in config.PROVIDER_INFO.items():
        assert meta["label"] in html, f"{p} missing from the connect panel"
        if not meta.get("local"):
            assert meta["key_url"] in html, f"no link to get a {p} key"

def test_connections_dashboard_offers_a_fix():
    """An expired session must be visible and repairable without a terminal."""
    import app
    real_auth, real_which = ai.cli_auth, ai.shutil.which
    saved = {k: getattr(config, k) for k in settings.FIELDS}
    try:
        ai.shutil.which = lambda n: "/fake/claude"
        ai.cli_auth = lambda: {"loggedIn": False, "authMethod": "none"}
        html = app.app.test_client().get("/connexions").get_data(as_text=True)
        assert "session expirée" in html, "expired session not surfaced"
        assert url_for_login(app) in html, "no button to repair it"

        ai.cli_auth = lambda: {"loggedIn": True, "authMethod": "claudeai"}
        config.CLAUDE_CLI = True
        html = app.app.test_client().get("/connexions").get_data(as_text=True)
        assert "session expirée" not in html and "actif" in html, \
            "still shown as broken once logged in"

        st = app.app.test_client().get("/api/connexions").get_json()
        assert st["cli_logged"] is True, "poller cannot see the session came back"
    finally:
        ai.cli_auth, ai.shutil.which = real_auth, real_which
        _restore_ai(saved)

def url_for_login(app):
    with app.app.test_request_context():
        from flask import url_for
        return url_for("connexions_claude")

def test_cli_login_never_touches_credentials():
    """The app opens the door; it must not read or carry any secret."""
    import inspect
    src = inspect.getsource(ai.cli_login)
    for forbidden in ("keychain", "security find", ".credentials.json", "password"):
        assert forbidden not in src.lower(), f"cli_login reaches for {forbidden}"
    assert "auth login" in src, "login must go through the CLI's own flow"

def test_layout_holds_on_a_phone():
    """Regressions here are invisible on a laptop and ruin the app on a phone."""
    import app, re
    css = (pathlib.Path("static") / "app.css").read_text()
    # sticky <th> silently stops working in Safari with border-collapse:collapse
    assert "border-collapse:separate" in css, "sticky headers will break in Safari"
    # under 16px, iOS Safari zooms into the field on tap and never zooms back
    assert "font-size:16px" in css, "form fields will trigger iOS zoom"
    assert "@media(hover:hover)" in css, "hover styles stick after a tap on touch"
    assert css.count("@media(min-width") >= 2, "breakpoints are not mobile-first"
    assert "max-width:639px" in css, "no stacked layout for narrow screens"

    c = app.app.test_client()
    html = c.get("/").get_data(as_text=True)
    # the nav must not depend on <details>: closed, it hides its own content
    # via content-visibility, which no portable author CSS can undo
    assert "<details" not in html, "nav menu is back on <details>"
    assert 'id="navtoggle"' in html and 'for="navtoggle"' in html, \
        "no CSS-only way to open the menu"
    # every table needs a real thead, or the header row becomes a stray card
    for page in ("/catalogue?tab=annonces", "/catalogue?tab=produits", "/favoris"):
        h = c.get(page).get_data(as_text=True)
        assert h.count("<table") <= h.count("<thead>"), f"{page}: table without thead"

def test_crawler_page_never_calls_a_refused_site_active():
    """The audit page must not claim to crawl a site we refuse on principle."""
    import app
    html = app.app.test_client().get("/crawler").get_data(as_text=True)
    top = html[:html.index("Sites qui refusent")] if "Sites qui refusent" in html else html
    for dom in ("www.leboncoin.fr",):
        i = top.find(dom)
        assert i > 0, f"{dom} missing from the domain table"
        row = top[i:i + 900]
        assert "refusé" in row, f"{dom} is shown as crawlable on the audit page"
        assert ">actif<" not in row, f"{dom} still reads as actif"
    # Nothing is asserted about a permitted site here: with no network the
    # crawler cannot read robots.txt and fails closed, so tutti legitimately
    # reads "refusé" in the suite. The regression being pinned is the opposite
    # one -- a site refused on principle must never read as active.


# --- eBay ------------------------------------------------------------------

def test_interrupted_scan_does_not_leave_matches_provisional():
    """Phase 1 rows must not survive as "analyse en cours…" for ever.

    Restarting mid-scan killed phase 2 and left keyword-scored rows on the
    results page as though a verdict were still coming. 139 of 271 matches in
    the real database were stuck that way -- including a hydrofoil in a
    backpack search, matched on the single token "V2".
    """
    db.run("DELETE FROM matches")
    db.run("DELETE FROM listings")
    db.run("DELETE FROM searches")
    sid = db.run("""INSERT INTO searches(name,query,origins,sources,shipping_ok,created_at)
                    VALUES('sac à dos','Peak design 30l V2','[]','[]',1,?)""", (time.time(),))
    junk = db.run("INSERT INTO listings(url,source,title,first_seen) VALUES(?,?,?,?)",
                  ("https://x/foil", "fake", "Armstrong Foil CF2400 V2", time.time()))
    good = db.run("INSERT INTO listings(url,source,title,first_seen) VALUES(?,?,?,?)",
                  ("https://x/bag", "fake", "Peak Design Everyday Backpack 30L V2", time.time()))
    old = time.time() - 3600
    for lid in (junk, good):
        db.run("""INSERT INTO matches(search_id,listing_id,score,reason,created_at)
                  VALUES(?,?,?,?,?)""",
               (sid, lid, engine.keyword_score(
                   dict(db.q("SELECT * FROM listings WHERE id=?", (lid,), one=True)),
                   db.q("SELECT * FROM searches WHERE id=?", (sid,), one=True)),
                engine.PENDING, old))

    assert db.q("SELECT score FROM matches WHERE listing_id=?", (junk,),
                one=True)["score"] < 40, "the foil should score badly on keywords"

    real = ai.analyse
    ai.analyse = lambda *a, **k: {}          # no AI: the keyword cut-off applies
    try:
        seen, dropped = engine.finish_pending(older_than=60)
    finally:
        ai.analyse = real
    assert seen == 2, seen
    assert dropped == 1, f"the foil should have been dropped, dropped={dropped}"
    assert db.q("SELECT 1 FROM matches WHERE listing_id=?", (junk,), one=True) is None, \
        "hydrofoil still matched to a backpack search"
    kept = db.q("SELECT reason FROM matches WHERE listing_id=?", (good,), one=True)
    assert kept and kept["reason"] != engine.PENDING, "real match left provisional"

    # a scan still running must not be judged out from under itself
    db.run("UPDATE matches SET reason=? , created_at=? WHERE listing_id=?",
           (engine.PENDING, time.time(), good))
    assert engine.finish_pending(older_than=600) == (0, 0), \
        "a fresh phase-1 row was judged while its scan was still going"


# --- email alerts: the legitimate route into sites that refuse the crawler ---

def test_refused_sites_are_never_crawled_even_with_an_adapter():
    """leboncoin now has an adapter. It must not reach leboncoin."""
    import ast, inspect
    for site in sources.DENIED_BY_OPERATOR:
        assert site in mailbox.SITES, \
            f"{site} refuses crawling but has a non-mail adapter"
    # the mail reader must not import the crawl layer at all
    tree = ast.parse(inspect.getsource(mailbox))
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            imported.add(n.module.split(".")[0])
    assert not ({"crawler", "browser", "net"} & imported), \
        f"the mail reader reaches the network on its own: {imported}"
    # and asking leboncoin with no mailbox configured must stay silent, not crawl
    saved = config.IMAP_HOST
    try:
        config.IMAP_HOST = ""
        assert sources.ADAPTERS["leboncoin"]("velo") == []
        assert sources.LAST_STATUS["leboncoin"][0] == "login"
    finally:
        config.IMAP_HOST = saved

def test_alert_email_is_parsed_into_listings():
    html = """<table>
      <tr><td><a href="https://www.leboncoin.fr/ad/velos/2891234567?utm_source=alerte">
        Vélo de course Cannondale CAAD13</a><span>1\u00a0250 €</span> Annemasse</td></tr>
      <tr><td><a href="https://www.leboncoin.fr/ad/motos/2891999888">Yamaha Tracer 900</a>
        <span>6\u202f900 €</span></td></tr>
      <tr><td><a href="https://tracking.example.com/click">Se désabonner</a></td></tr>
    </table>"""
    rows = mailbox.parse("leboncoin", html)
    assert len(rows) == 2, [r["title"] for r in rows]
    assert rows[0]["price"] == 1250.0, rows[0]["price"]
    assert rows[1]["price"] == 6900.0, rows[1]["price"]
    assert rows[0]["currency"] == "EUR" and rows[0]["country"] == "FR"
    assert "?" not in rows[0]["url"], "tracking parameters kept in the url"
    # a title carrying digits must not be swallowed into the price
    solo = mailbox.parse("leboncoin",
        '<a href="https://www.leboncoin.fr/ad/x/2891234567">CAAD13</a><span>1 250 €</span>')
    assert solo[0]["price"] == 1250.0, solo[0]["price"]
    # no price shown means no price invented
    bare = mailbox.parse("leboncoin",
        '<a href="https://www.leboncoin.fr/ad/x/2891234500">Titre seul</a>')
    assert bare[0]["price"] is None

def test_mail_listings_go_through_the_normal_pipeline():
    rows = mailbox.parse("leboncoin",
        '<a href="https://www.leboncoin.fr/ad/velos/2891234567">Vélo Cannondale</a>'
        '<span>1 250 €</span>')
    db.run("DELETE FROM listings WHERE url=?", (rows[0]["url"],))
    lid, new = engine.upsert_listing(rows[0])
    assert new
    got = db.q("SELECT price, source, currency FROM listings WHERE id=?", (lid,), one=True)
    assert got["price"] == 1250.0 and got["source"] == "leboncoin"
    assert got["currency"] == "EUR"
    db.run("DELETE FROM listings WHERE id=?", (lid,))


# --- per-job routing -------------------------------------------------------

def test_bulk_work_can_never_be_put_on_the_subscription():
    """Measured: each `claude -p` call drags ~24k tokens of Claude Code context.

    Two per search is nothing; hundreds a day would exhaust the subscription's
    rate limits in minutes. Choosing it for bulk must be refused in code, not
    merely hidden in the form.
    """
    saved = getattr(config, "JOB_ROUTES", {})
    try:
        config.JOB_ROUTES = {j: {"account": "abonnement", "model": "m"}
                             for j in config.JOBS}
        for job in ("tri", "traduction"):
            acct, _ = ai.route(job)
            assert acct != "abonnement", f"{job} was routed to the subscription"
        assert ai.route("entretien")[0] == "abonnement", "the interview may use it"
    finally:
        config.JOB_ROUTES = saved

def test_job_route_picks_the_model():
    """A job pinned to a model must use that one, not the account default."""
    saved = (getattr(config, "JOB_ROUTES", {}), config.AI_FALLBACKS)
    seen = {}
    real_post = ai.net.post_json
    class R:
        status_code = 200
        def json(self):
            return {"choices": [{"message": {"content": "{}"}}], "usage": {}}
    def fake(url, body, headers=None, timeout=None):
        seen["model"] = body.get("model")
        return R()
    try:
        config.JOB_ROUTES = {"traduction": {"account": "principal", "model": "mon-modele"}}
        ai.net.post_json = fake
        ai._tier_down.clear()
        db.run("DELETE FROM ai_cache")
        ai.chat("s", "u", job="traduction")
        assert seen["model"] == "mon-modele", seen["model"]
        # and with no model pinned, the account default is used
        config.JOB_ROUTES = {}
        db.run("DELETE FROM ai_cache")
        ai.chat("s", "u", job="traduction")
        assert seen["model"] == config.AI_FALLBACKS[0], seen["model"]
    finally:
        ai.net.post_json = real_post
        db.run("DELETE FROM ai_cache")
        config.JOB_ROUTES, config.AI_FALLBACKS = saved

def test_subscription_calls_do_not_eat_the_api_budget():
    """The point of the subscription is that it costs nothing here."""
    db.run("DELETE FROM ai_spend")
    before = ai.budget_left()
    ai.record_spend("claude-code/claude-sonnet-5",
                    {"prompt_tokens": 9, "completion_tokens": 191, "cost": 0.0},
                    "entretien")
    row = db.q("SELECT cost_usd, tokens_in, tokens_out FROM ai_spend", one=True)
    assert row["cost_usd"] == 0.0, "a subscription call was billed to the API budget"
    assert row["tokens_in"] == 9 and row["tokens_out"] == 191, "tokens lost"
    assert ai.budget_left() == before
    db.run("DELETE FROM ai_spend")

def test_settings_page_shows_what_each_job_really_uses():
    import app
    html = app.app.test_client().get("/reglages").get_data(as_text=True)
    from markupsafe import escape as _esc
    for job, meta in config.JOBS.items():
        # Jinja escapes quotes too: "Entretien de l'assistant" -> &#39;
        assert str(_esc(meta["label"])) in html, f"{job} missing from the panel"
        assert f'name="acct_{job}"' in html and f'name="model_{job}"' in html
    # the subscription must not even be offered for bulk work
    i = html.index('name="acct_tri"')
    assert "abonnement" not in html[i:i + 400].lower(), \
        "the subscription is offered for bulk work"

def test_module_selfchecks_do_not_touch_live_settings():
    """`python3 settings.py` once wiped the user's CLAUDE_CLI row."""
    db.run("INSERT OR REPLACE INTO settings(k,v,updated_at) VALUES('CLAUDE_CLI','1',0)")
    before = dict(db.q("SELECT k,v FROM settings WHERE k='CLAUDE_CLI'", one=True))
    settings.demo()
    after = db.q("SELECT k,v FROM settings WHERE k='CLAUDE_CLI'", one=True)
    assert after and dict(after) == before, \
        "the self-check changed a stored setting instead of restoring it"
    db.run("DELETE FROM settings WHERE k='CLAUDE_CLI'")
    settings.demo()
    assert db.q("SELECT 1 FROM settings WHERE k='CLAUDE_CLI'", one=True) is None, \
        "the self-check left a row behind where there was none"

if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print(f"  ok  {name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    print(f"all green (scratch db, {_REAL_DB.name} untouched)")
